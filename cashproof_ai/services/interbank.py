"""
cashproof_ai/services/interbank.py

Full §6 Interbank Matching Algorithm — 7 steps.

Step 1  — Candidate filter (pattern / description scan)
Step 2  — Find opposite-side candidate
Step 3  — Score confidence (point table)
Step 4  — Classify by score → CONFIRMED / HIGH_PROBABILITY / POSSIBLE / NOT_INTERBANK
Step 5  — One-sided transfers (candidate found, but no counterpart)
Step 6  — Record confirmed matches (assign IB-xxx Match IDs)
Step 7  — Remove from external totals (handled by calculations.py)

Hard rules (§12):
  - Never classify on keyword match alone.
  - Never exclude one-sided candidates from net totals.
  - Never guess an account mapping.
"""

import re
import logging
from decimal import Decimal
from datetime import timedelta, date

from ..models import CashProofSession, LedgerTransaction, InterbankMatch

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

# Keywords that, if present in the description, make the tx a CANDIDATE (Step 1b)
INTERNAL_TRANSFER_PATTERNS = re.compile(
    r'\b(?:'
    r'internal\s+transfer|online\s+banking\s+transfer|transfer\s+to\s+(?:checking|savings|money\s+market|payroll)|'
    r'transfer\s+from\s+(?:checking|savings|money\s+market|payroll)|'
    r'int(?:ernal)?\s*txfr|book\s+transfer|acct\s+transfer|account\s+transfer'
    r')\b',
    re.IGNORECASE,
)

# Permanent exclusions — these can NEVER be interbank regardless of anything else (§5 / §12)
HARD_EXCLUSION_PATTERNS = re.compile(
    r'\b(?:interest|bank\s+fee|service\s+fee|service\s+charge|maintenance\s+fee|'
    r'overdraft\s+fee|nsf\s+fee|monthly\s+fee)\b',
    re.IGNORECASE,
)

# Scoring constants (§6 Step 3)
SCORE_SAME_CLIENT_BOTH_IN_MASTER = 30
SCORE_EXACT_AMOUNT               = 25
SCORE_SAME_DATE                  = 20
SCORE_NEXT_DAY                   = 15
SCORE_WITHIN_2BD                 = 10
SCORE_ACCT_NUMBER_IN_DESC        = 15
SCORE_INTERNAL_TRANSFER_WORDING  = 10
SCORE_BANK_TRANSFER_TYPE         = 5


# ── Public API ────────────────────────────────────────────────────────────────

def run_interbank_matching(session: CashProofSession) -> dict:
    """
    Execute the full §6 algorithm for *session*.
    Returns a summary dict with counts.
    """
    # Reset all interbank fields for this session (fresh run)
    tx_ids = list(
        LedgerTransaction.objects.filter(statement__session=session).values_list('id', flat=True)
    )
    LedgerTransaction.objects.filter(id__in=tx_ids).update(
        interbank_status='NOT_INTERBANK',
        interbank_confidence_score=None,
        interbank_matched_account='',
        interbank_match=None,
        reason_for_classification='',
    )
    InterbankMatch.objects.filter(session=session).delete()

    all_txs = list(LedgerTransaction.objects.filter(id__in=tx_ids).select_related('client_account'))
    accounts = list(session.client_accounts.all())
    acct_by_id = {a.id: a for a in accounts}

    # Step 1 — Build candidate set and permanently exclude fee/interest
    candidates, permanently_excluded = _step1_filter(all_txs, acct_by_id)

    # Bulk-update permanently excluded
    if permanently_excluded:
        excluded_ids = [t.id for t in permanently_excluded]
        LedgerTransaction.objects.filter(id__in=excluded_ids).update(
            interbank_status='EXCLUDED_FEE_INTEREST',
            reason_for_classification='Hard-excluded: fee or interest — never interbank (§5 / §12)',
        )

    # Steps 2–6 — Match candidates
    match_counter      = _get_next_match_counter(session)
    confirmed_pairs    = []
    one_sided          = []
    high_prob          = []
    possible_unmatched = []
    to_update          = []

    debit_candidates  = [t for t in candidates if (t.debit_amount or 0) > 0]
    credit_candidates = [t for t in candidates if (t.credit_amount or 0) > 0]

    matched_credit_ids = set()
    matched_debit_ids  = set()

    for debit_tx in debit_candidates:
        if debit_tx.id in matched_debit_ids:
            continue

        # Step 2 — find opposite-side candidates
        opposite_candidates = _step2_find_candidates(
            debit_tx, credit_candidates, matched_credit_ids, acct_by_id
        )

        if not opposite_candidates:
            # Step 5 — one-sided
            debit_tx.interbank_status          = 'POSSIBLE_UNMATCHED'
            debit_tx.interbank_confidence_score = 0
            debit_tx.reason_for_classification  = (
                'Internal transfer indicator found, but corresponding client-owned account '
                'transaction was not available in the uploaded statements.'
            )
            one_sided.append(debit_tx)
            to_update.append(debit_tx)
            continue

        # Step 3 — score each opposite candidate
        scored = []
        for credit_tx in opposite_candidates:
            score, reasons = _step3_score(debit_tx, credit_tx, acct_by_id)
            scored.append((score, reasons, credit_tx))

        scored.sort(key=lambda x: -x[0])
        best_score, best_reasons, best_credit = scored[0]

        # Tie check — multiple candidates with the same best score
        tied = [s for s in scored if s[0] == best_score]
        if len(tied) > 1:
            debit_tx.interbank_status          = 'AMBIGUOUS'
            debit_tx.interbank_confidence_score = best_score
            debit_tx.reason_for_classification  = (
                f'Score {best_score}: {len(tied)} equally-scored candidates — flagged for manual review.'
            )
            to_update.append(debit_tx)
            possible_unmatched.append(debit_tx)
            continue

        # Step 4 — classify by score
        status = _step4_classify(best_score)

        debit_tx.interbank_status          = status
        debit_tx.interbank_confidence_score = best_score
        debit_tx.reason_for_classification  = '; '.join(best_reasons)
        debit_tx.interbank_matched_account  = best_credit.account_number

        best_credit.interbank_status          = status
        best_credit.interbank_confidence_score = best_score
        best_credit.reason_for_classification  = '; '.join(best_reasons)
        best_credit.interbank_matched_account  = debit_tx.account_number

        # Step 6 — assign Match ID for confirmed pairs
        if status == 'CONFIRMED_INTERBANK':
            match_id_str = f'IB-{match_counter:03d}'
            match_counter += 1

            ib_match = InterbankMatch.objects.create(
                session=session,
                match_id=match_id_str,
                match_date=debit_tx.transaction_date,
                client_name=session.client_name,
                from_bank=debit_tx.bank_name,
                from_account=debit_tx.account_number,
                to_bank=best_credit.bank_name,
                to_account=best_credit.account_number,
                amount=debit_tx.debit_amount or Decimal('0'),
                debit_description=debit_tx.description,
                credit_description=best_credit.description,
                match_type='amount_date_cross_account',
                confidence_score=best_score,
                status='CONFIRMED_INTERBANK',
                reason='; '.join(best_reasons),
            )
            debit_tx.interbank_match  = ib_match
            best_credit.interbank_match = ib_match
            confirmed_pairs.append((debit_tx, best_credit))
            matched_debit_ids.add(debit_tx.id)
            matched_credit_ids.add(best_credit.id)
        elif status == 'HIGH_PROBABILITY_INTERBANK':
            high_prob.append(debit_tx)
        else:
            possible_unmatched.append(debit_tx)

        to_update.append(debit_tx)
        to_update.append(best_credit)

    # Bulk-update all modified transactions
    if to_update:
        # Deduplicate
        seen = set()
        unique_updates = []
        for t in to_update:
            if t.id not in seen:
                seen.add(t.id)
                unique_updates.append(t)
        LedgerTransaction.objects.bulk_update(
            unique_updates,
            [
                'interbank_status', 'interbank_confidence_score',
                'interbank_matched_account', 'interbank_match',
                'reason_for_classification',
            ]
        )

    logger.info(
        f'Session #{session.pk}: {len(confirmed_pairs)} confirmed pairs, '
        f'{len(one_sided)} one-sided, {len(high_prob)} high-prob, '
        f'{len(possible_unmatched)} possible/unmatched.'
    )

    return {
        'confirmed_pairs': len(confirmed_pairs),
        'one_sided': len(one_sided),
        'high_probability': len(high_prob),
        'possible_unmatched': len(possible_unmatched),
        'permanently_excluded': len(permanently_excluded),
    }


# ── Step implementations ──────────────────────────────────────────────────────

def _step1_filter(
    all_txs: list[LedgerTransaction],
    acct_by_id: dict,
) -> tuple[list[LedgerTransaction], list[LedgerTransaction]]:
    """
    Step 1 — Candidate filter.
    A tx is a candidate only if:
      (a) Its description contains an account number / last4 / nickname from the
          client account master (for a DIFFERENT account than its own), OR
      (b) Its description matches an internal-transfer pattern.
    Everything else short-circuits to NOT_INTERBANK immediately.
    """
    candidates          = []
    permanently_excluded = []

    for tx in all_txs:
        desc = tx.description or ''

        # Permanent exclusions first
        if HARD_EXCLUSION_PATTERNS.search(desc):
            permanently_excluded.append(tx)
            continue

        # Check (a): account number / last4 in description
        if acct_by_id and tx.client_account_id:
            own_acct = acct_by_id.get(tx.client_account_id)
            for acct in acct_by_id.values():
                if acct.id == (own_acct.id if own_acct else None):
                    continue
                # Full account number
                if acct.account_number and len(acct.account_number) >= 6:
                    cleaned_acct = re.sub(r'[\s\-]', '', acct.account_number)
                    if cleaned_acct.lower() in re.sub(r'[\s\-]', '', desc).lower():
                        candidates.append(tx)
                        break
                # Last4
                if acct.last4 and len(acct.last4) == 4:
                    if re.search(rf'(?<!\d){re.escape(acct.last4)}(?!\d)', desc):
                        candidates.append(tx)
                        break
            else:
                # Check (b): internal transfer pattern
                if INTERNAL_TRANSFER_PATTERNS.search(desc):
                    candidates.append(tx)
        else:
            # No account master mapping; still check (b)
            if INTERNAL_TRANSFER_PATTERNS.search(desc):
                candidates.append(tx)

    return candidates, permanently_excluded


def _step2_find_candidates(
    debit_tx: LedgerTransaction,
    credit_candidates: list[LedgerTransaction],
    matched_credit_ids: set,
    acct_by_id: dict,
) -> list[LedgerTransaction]:
    """
    Step 2 — For a debit candidate, find all credit candidates that:
      - Are in a DIFFERENT account than the debit.
      - Have the EXACT same amount (0% tolerance — §6 Step 2).
      - Date within a 2-business-day window.
      - If description named a specific counterparty account, restrict to that account.
    """
    target_amount = debit_tx.debit_amount or Decimal('0')
    debit_date    = debit_tx.transaction_date
    min_date, max_date = _business_day_window(debit_date, 2)

    # Did the description name a specific target account?
    named_target = _extract_named_account(debit_tx.description, acct_by_id, debit_tx.client_account_id)

    result = []
    for credit_tx in credit_candidates:
        if credit_tx.id in matched_credit_ids:
            continue
        if credit_tx.client_account_id == debit_tx.client_account_id:
            continue
        if (credit_tx.credit_amount or Decimal('0')) != target_amount:
            continue
        if not (min_date <= credit_tx.transaction_date <= max_date):
            continue
        if named_target and credit_tx.client_account_id != named_target:
            continue
        result.append(credit_tx)

    # Sort by date proximity
    result.sort(key=lambda c: abs((c.transaction_date - debit_date).days))
    return result


def _step3_score(
    debit_tx: LedgerTransaction,
    credit_tx: LedgerTransaction,
    acct_by_id: dict,
) -> tuple[int, list[str]]:
    """
    Step 3 — Compute confidence score (0–100) and collect reason strings.
    """
    score   = 0
    reasons = []

    # +30: both accounts in the client account master
    both_in_master = (
        debit_tx.client_account_id in acct_by_id and
        credit_tx.client_account_id in acct_by_id
    )
    if both_in_master:
        score += SCORE_SAME_CLIENT_BOTH_IN_MASTER
        reasons.append('+30 both accounts in client master')

    # +25: exact amount match
    if (debit_tx.debit_amount or 0) == (credit_tx.credit_amount or 0):
        score += SCORE_EXACT_AMOUNT
        reasons.append('+25 exact amount match')

    # Date proximity
    day_diff = abs((credit_tx.transaction_date - debit_tx.transaction_date).days)
    if day_diff == 0:
        score += SCORE_SAME_DATE
        reasons.append('+20 same transaction date')
    elif day_diff == 1:
        score += SCORE_NEXT_DAY
        reasons.append('+15 next business day')
    else:
        score += SCORE_WITHIN_2BD
        reasons.append('+10 within 2-business-day window')

    # +15: same-client account number / last4 in description
    for acct in acct_by_id.values():
        if acct.id == debit_tx.client_account_id:
            continue
        cleaned_acct = re.sub(r'[\s\-]', '', acct.account_number or '')
        if (cleaned_acct and len(cleaned_acct) >= 4 and
                cleaned_acct.lower() in re.sub(r'[\s\-]', '', debit_tx.description or '').lower()):
            score += SCORE_ACCT_NUMBER_IN_DESC
            reasons.append(f'+15 account number {cleaned_acct[-4:]} in description')
            break
        if acct.last4 and re.search(rf'(?<!\d){re.escape(acct.last4)}(?!\d)', debit_tx.description or ''):
            score += SCORE_ACCT_NUMBER_IN_DESC
            reasons.append(f'+15 last4 {acct.last4} in description')
            break

    # +10: description strongly indicates internal transfer
    if INTERNAL_TRANSFER_PATTERNS.search(debit_tx.description or ''):
        score += SCORE_INTERNAL_TRANSFER_WORDING
        reasons.append('+10 description indicates internal transfer')

    # +5: transaction type consistent with bank-to-bank transfer (type C or G)
    if debit_tx.transaction_type in ('C', 'G'):
        score += SCORE_BANK_TRANSFER_TYPE
        reasons.append('+5 transaction type consistent with transfer')

    score = min(score, 100)
    return score, reasons


def _step4_classify(score: int) -> str:
    if score >= 90:
        return 'CONFIRMED_INTERBANK'
    if score >= 70:
        return 'HIGH_PROBABILITY_INTERBANK'
    if score >= 50:
        return 'POSSIBLE_INTERBANK'
    return 'NOT_INTERBANK'


# ── Utilities ─────────────────────────────────────────────────────────────────

def _business_day_window(d: date, n: int) -> tuple[date, date]:
    """Return (min_date, max_date) expanded by *n* business days on each side."""
    try:
        import numpy as np
        lo = np.busday_offset(d.isoformat(), -n, roll='forward')
        hi = np.busday_offset(d.isoformat(), n, roll='backward')
        lo_date = date.fromisoformat(str(lo))
        hi_date = date.fromisoformat(str(hi))
        return lo_date, hi_date
    except Exception:
        # Fallback: calendar days
        return d - timedelta(days=n), d + timedelta(days=n)


def _extract_named_account(
    description: str,
    acct_by_id: dict,
    own_acct_id,
) -> int | None:
    """
    If the description explicitly names one of the client's OTHER accounts
    (by account number or last4), return that account's id. Otherwise None.
    """
    desc_clean = re.sub(r'[\s\-]', '', description or '').lower()
    for acct in acct_by_id.values():
        if acct.id == own_acct_id:
            continue
        cleaned = re.sub(r'[\s\-]', '', acct.account_number or '').lower()
        if cleaned and len(cleaned) >= 4 and cleaned in desc_clean:
            return acct.id
        if acct.last4 and re.search(rf'(?<!\d){re.escape(acct.last4)}(?!\d)', description or ''):
            return acct.id
    return None


def _get_next_match_counter(session: CashProofSession) -> int:
    last = InterbankMatch.objects.filter(session=session).order_by('match_id').last()
    if not last or not last.match_id:
        return 1
    m = re.search(r'(\d+)$', last.match_id)
    return (int(m.group(1)) + 1) if m else 1
