# from decimal import Decimal
# import re
# from datetime import timedelta
# from django.db.models import Q
# from .models import BankAccount, Transaction


# def detect_interbank_transactions(user=None):
#     """
#     Scans the database to identify and flag interbank (self-transfer) transactions
#     based on rigorous client matching rules.

#     Detection priority (in order):
#       1. Reset existing flags
#       2. Negative Filtering (Exclude Interest, Fees, Customer ACH unless strong match)
#       3. Description/Reference Matching (Known client account numbers)
#       4. Amount/Date Cross-Account Pairing (Exact amount, ±2 days)
#       5. Possible/Unmatched Tagging (Says 'transfer' but no destination found)
#       6. Recalculate aggregates for all affected accounts
#     """

#     if user is not None:
#         user_upload_ids = user.uploads.values_list('id', flat=True)
#         user_accounts = list(BankAccount.objects.filter(upload_id__in=user_upload_ids))
#         user_account_ids = [acc.id for acc in user_accounts]
#         base_qs = Transaction.objects.filter(account_id__in=user_account_ids)
#     else:
#         user_accounts = list(BankAccount.objects.all())
#         base_qs = Transaction.objects.all()

#     # 1. Reset all interbank flags
#     base_qs.update(is_interbank=False)
    
#     # Reload all transactions into memory for logic
#     all_txs = list(base_qs.order_by('date', 'id'))
#     updated_txs = []
    
#     # Extract client account mappings (Account Number, Bank Name, Holder)
#     acct_patterns = []
#     for acc in user_accounts:
#         raw_num = acc.account_number.strip()
#         cleaned_num = re.sub(r'[\s\-]', '', raw_num)
        
#         patterns = []
#         if cleaned_num and cleaned_num.lower() != 'unknownaccount':
#             patterns.append(cleaned_num)
#             if len(cleaned_num) >= 4:
#                 patterns.append(cleaned_num[-4:])
                
#         acct_patterns.append({
#             'account_id': acc.id,
#             'bank_name': acc.bank_name,
#             'account_holder': acc.account_holder,
#             'account_number': cleaned_num,
#             'patterns': patterns
#         })

#     # Negative Match Keywords
#     not_interbank_keywords = re.compile(
#         r'\b(?:interest|bank fee|service fee|customer ach deposit|maintenance fee|overdraft fee)\b', 
#         re.IGNORECASE
#     )
    
#     # Generic Transfer Keywords (for Possible/Unmatched marking)
#     generic_transfer_keywords = re.compile(
#         r'\b(?:transfer to|transfer from|internal transfer|own account transfer|online banking transfer)\b', 
#         re.IGNORECASE
#     )

#     # Dictionary to keep track of matched IDs to prevent double counting during pairing
#     matched_inflow_ids = set()

#     for tx in all_txs:
#         desc_lower = tx.description.lower()
#         desc_cleaned = re.sub(r'[\s\-]', '', tx.description)
        
#         # 2. Negative Filtering (Interest / Fee)
#         if not_interbank_keywords.search(desc_lower):
#             # NOT INTERBANK unless a strong account match overrides it later
#             tx.is_interbank = False
#             continue

#         is_strong_match = False
        
#         # 3. Description / Reference Matching (Account Numbers)
#         for ap in acct_patterns:
#             # Don't match against its own account
#             if ap['account_id'] == tx.account_id:
#                 continue
                
#             for pat in ap['patterns']:
#                 if len(pat) > 4 and pat in desc_cleaned:
#                     is_strong_match = True
#                     break
#                 # Last 4 digits match requires word boundaries
#                 elif len(pat) == 4 and re.search(rf'(?<!\d){re.escape(pat)}(?!\d)', tx.description):
#                     is_strong_match = True
#                     break
                    
#             if is_strong_match:
#                 break
                
#         if is_strong_match:
#             # High confidence / Confirmed Interbank based on description
#             tx.is_interbank = True
#             tx.category = 'Interbank Self-Transfer'
#             updated_txs.append(tx)
#             continue
            
#         # 5. Generic Transfer check (Possible/Unmatched)
#         if generic_transfer_keywords.search(desc_lower):
#             tx.is_interbank = False # Not automatically interbank
#             tx.category = 'Possible Interbank (Unmatched)'
#             updated_txs.append(tx)
#             continue

#     # Commit the description-based updates first
#     if updated_txs:
#         Transaction.objects.bulk_update(updated_txs, ['is_interbank', 'category'])
        
#     # Reload for Pairing
#     outflows = [t for t in all_txs if t.amount < 0]
#     inflows = {t.id: t for t in all_txs if t.amount > 0}
    
#     # Helper to determine if two accounts belong to the same client
#     def is_same_client(acc1_id, acc2_id):
#         if user is not None:
#             return True # All accounts in qs belong to the same user
        
#         # If no user, rely on account_holder name matching exactly
#         acc1 = next((a for a in user_accounts if a.id == acc1_id), None)
#         acc2 = next((a for a in user_accounts if a.id == acc2_id), None)
#         if not acc1 or not acc2:
#             return False
#         if not acc1.account_holder or not acc2.account_holder:
#             return False
#         return acc1.account_holder.lower() == acc2.account_holder.lower()

#     # 4. Cross-Account Amount & Date Pairing (0% Tolerance, ±2 days)
#     paired_txs = []
    
#     for outflow in outflows:
#         # If it was already confirmed via description, we still try to pair it to mark the counterpart
#         target_amount = abs(outflow.amount)
#         min_date = outflow.date - timedelta(days=2)
#         max_date = outflow.date + timedelta(days=2)
        
#         # Find matching inflow
#         best_match = None
#         for inf_id, inflow in inflows.items():
#             if inf_id in matched_inflow_ids:
#                 continue
                
#             if inflow.account_id == outflow.account_id:
#                 continue # Must be different account
                
#             if not is_same_client(outflow.account_id, inflow.account_id):
#                 continue # Must be same client
                
#             if inflow.amount == target_amount and min_date <= inflow.date <= max_date:
#                 # Prioritize exact date match
#                 if best_match is None or abs((inflow.date - outflow.date).days) < abs((best_match.date - outflow.date).days):
#                     best_match = inflow
                    
#         if best_match:
#             matched_inflow_ids.add(best_match.id)
            
#             # CONFIRMED INTERBANK
#             outflow.is_interbank = True
#             outflow.category = 'Interbank Self-Transfer'
#             best_match.is_interbank = True
#             best_match.category = 'Interbank Self-Transfer'
            
#             paired_txs.append(outflow)
#             paired_txs.append(best_match)
            
#     if paired_txs:
#         Transaction.objects.bulk_update(paired_txs, ['is_interbank', 'category'])

#     # 6. Recalculate Aggregates for all affected accounts
#     from .parsers.manager import update_account_aggregates

#     for account in user_accounts:
#         update_account_aggregates(account)


"""
detect_interbank_transactions — revised

Fixes vs. the original:
  1. Fees/interest are PERMANENTLY excluded from the pairing candidate pool
     (not just skipped in the first pass) — they can never become interbank.
  2. "Customer ACH deposit" style descriptions are excluded by default but
     still get a chance at a Rule 5 strong-match override, per the rule table.
  3. Any transaction already confirmed via Rule 5 (description match) is
     removed from the Rule 3/4 candidate pool, so it can't be double-matched
     to a second, unrelated counterpart.
  4. Rule 4's date window is business-day aware, and a confidence tier
     (very_high / high / medium) is computed and stored.
  5. Ambiguous ties (multiple equally-good candidates for the same amount
     and window) are NOT force-matched — they're left flagged for manual
     review instead of guessed.
  6. Consistent normalization for description/account-number matching.
  7. `category` (set by the statement parser's derive_category) is NEVER
     read or written here. This function only ever touches is_interbank /
     interbank_confidence / interbank_match_status — a prior version reset
     `category` to '' for every transaction up front, which wiped out every
     non-interbank transaction's category in the ledger. Keep these two
     concerns fully separate.

Requires two new nullable fields on Transaction:
    is_interbank            BooleanField (already existed)
    interbank_confidence    CharField(null=True)   # very_high/high/medium
    interbank_match_status  CharField(null=True)   # see values used below
"""

from decimal import Decimal
import re
from datetime import timedelta
import numpy as np
from .models import BankAccount, Transaction


NOT_INTERBANK_HARD_KEYWORDS = re.compile(
    r'\b(?:interest|bank fee|service fee|maintenance fee|overdraft fee)\b',
    re.IGNORECASE
)

# Conditional exclusion: excluded by default, but overridable by Rule 5.
CONDITIONAL_KEYWORDS = re.compile(
    r'\b(?:customer ach deposit|ach deposit)\b',
    re.IGNORECASE
)

GENERIC_TRANSFER_KEYWORDS = re.compile(
    r'\b(?:transfer to|transfer from|internal transfer|own account transfer|'
    r'online banking transfer|money market transfer|payroll account transfer)\b',
    re.IGNORECASE
)


def _clean(s: str) -> str:
    return re.sub(r'[\s\-]', '', s or '')


def _normalize_name(s: str) -> str:
    """Lowercase and collapse to alphanumeric+space, so 'Kwik Goal LTD.',
    'kwik goal ltd', 'KWIK GOAL LTD' all compare equal, and punctuation in
    a statement description doesn't break a holder-name match."""
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


def _business_day_window(date, max_business_days=2):
    """Return (min_date, max_date) expanded to cover N business days on
    either side, skipping weekends (extend to Mon/Fri when the edge lands
    on a weekend)."""
    lo = np.busday_offset(date, -max_business_days, roll='forward')
    hi = np.busday_offset(date, max_business_days, roll='backward')
    return lo.astype('datetime64[D]').tolist(), hi.astype('datetime64[D]').tolist()


def _date_confidence(outflow_date, inflow_date):
    diff = abs((inflow_date - outflow_date).days)
    if diff == 0:
        return 'very_high'
    if diff == 1:
        return 'high'
    return 'medium'


def detect_interbank_transactions(user=None):
    if user is not None:
        user_upload_ids = user.uploads.values_list('id', flat=True)
        user_accounts = list(BankAccount.objects.filter(upload_id__in=user_upload_ids))
        user_account_ids = [acc.id for acc in user_accounts]
        base_qs = Transaction.objects.filter(account_id__in=user_account_ids)
    else:
        user_accounts = list(BankAccount.objects.all())
        base_qs = Transaction.objects.all()

    # 1. Reset flags — NEVER touch `category` here. Category is owned by the
    # statement parser (derive_category); this function only ever writes
    # is_interbank / interbank_confidence / interbank_match_status.
    base_qs.update(is_interbank=False, interbank_confidence=None, interbank_match_status=None)

    all_txs = list(base_qs.order_by('date', 'id'))

    # Known client account patterns (from linked BankAccount records, NOT
    # from statement parsing — these are the accounts the user entered).
    # Each pattern is tagged 'full' (the complete account number — safe to
    # match as a plain substring) or 'partial' (last-4 / last-5 digits —
    # statements commonly show only "...9625" or "...79625", so these need
    # boundary-aware matching to avoid matching inside a longer number).
    acc_by_id = {acc.id: acc for acc in user_accounts}
    acct_patterns = []
    for acc in user_accounts:
        cleaned_num = _clean(acc.account_number)
        patterns = []
        if cleaned_num and cleaned_num.lower() != 'unknownaccount':
            patterns.append((cleaned_num, 'full'))
            if len(cleaned_num) >= 4:
                patterns.append((cleaned_num[-4:], 'partial'))
            if len(cleaned_num) >= 5:
                patterns.append((cleaned_num[-5:], 'partial'))
        holder_norm = _normalize_name(acc.account_holder) if acc.account_holder else None
        acct_patterns.append({
            'account_id': acc.id,
            'patterns': patterns,
            'holder_norm': holder_norm,
        })

    def is_same_client(acc1_id, acc2_id):
        if user is not None:
            return True
        acc1 = next((a for a in user_accounts if a.id == acc1_id), None)
        acc2 = next((a for a in user_accounts if a.id == acc2_id), None)
        if not acc1 or not acc2 or not acc1.account_holder or not acc2.account_holder:
            return False
        return acc1.account_holder.lower() == acc2.account_holder.lower()

    def strong_description_match(tx):
        desc_cleaned = _clean(tx.description)
        desc_norm = _normalize_name(tx.description)
        own_acc = acc_by_id.get(tx.account_id)
        own_holder_norm = _normalize_name(own_acc.account_holder) if own_acc and own_acc.account_holder else None

        for ap in acct_patterns:
            if ap['account_id'] == tx.account_id:
                continue

            # Account-number match: full number (plain substring) or
            # last-4 / last-5 digits (must sit on a digit boundary).
            for pat, kind in ap['patterns']:
                if kind == 'full' and pat in desc_cleaned:
                    return True
                if kind == 'partial' and re.search(rf'(?<!\d){re.escape(pat)}(?!\d)', desc_cleaned):
                    return True

            # Account-holder-name match: another of the client's OWN
            # accounts has a holder name that shows up in this
            # transaction's description — e.g. a "Kwik Goal LTD" account's
            # transaction mentions "Tulsi", another account the same
            # client holds. This is only checked when the other holder's
            # name is DIFFERENT from this transaction's own account holder
            # name. Sibling accounts that share the exact same holder name
            # (e.g. three accounts all titled "Kwik Goal LTD") would
            # otherwise match on every ordinary ACH/payroll/tax line that
            # simply references the company's own name as originator —
            # that's not evidence of a transfer between accounts, so it's
            # deliberately excluded.
            other_holder_norm = ap.get('holder_norm')
            if (other_holder_norm and len(other_holder_norm) >= 3
                    and re.search(rf'(?<![a-z0-9]){re.escape(other_holder_norm)}(?![a-z0-9])', desc_norm)):
                return True

        return False

    permanently_excluded_ids = set()   # hard-excluded: never eligible, ever
    rule5_confirmed_ids = set()        # confirmed via description: remove from pairing pool
    to_save = []

    for tx in all_txs:
        desc_lower = (tx.description or '').lower()

        # --- Hard exclusion: interest / fees can NEVER be interbank ---
        # (category is left exactly as the parser set it — not touched here)
        if NOT_INTERBANK_HARD_KEYWORDS.search(desc_lower):
            tx.is_interbank = False
            tx.interbank_match_status = 'excluded_fee_interest'
            permanently_excluded_ids.add(tx.id)
            to_save.append(tx)
            continue

        # --- Conditional exclusion: ACH deposit, unless Rule 5 overrides ---
        if CONDITIONAL_KEYWORDS.search(desc_lower):
            if strong_description_match(tx):
                tx.is_interbank = True
                tx.interbank_confidence = 'very_high'  # description match = strongest signal
                tx.interbank_match_status = 'confirmed_description'
                rule5_confirmed_ids.add(tx.id)
                to_save.append(tx)
            else:
                tx.is_interbank = False
                tx.interbank_match_status = 'excluded_customer_ach'
                permanently_excluded_ids.add(tx.id)
                to_save.append(tx)
            continue

        # --- Rule 5: strong account-number-in-description match ---
        if strong_description_match(tx):
            tx.is_interbank = True
            tx.interbank_confidence = 'very_high'
            tx.interbank_match_status = 'confirmed_description'
            rule5_confirmed_ids.add(tx.id)
            to_save.append(tx)
            continue

        # --- Generic "transfer"-flavored description, no destination found yet ---
        if GENERIC_TRANSFER_KEYWORDS.search(desc_lower):
            tx.is_interbank = False
            tx.interbank_match_status = 'possible_unmatched'
            to_save.append(tx)
            # NOTE: no `continue` — it still needs a chance at Rule 3/4 pairing.

    if to_save:
        Transaction.objects.bulk_update(
            to_save, ['is_interbank', 'interbank_confidence', 'interbank_match_status']
        )

    # --- Rule 5b: find and flag the counterpart of every Rule 5 match ---
    # Rule 5 only proves ONE leg of a transfer is interbank (the leg whose
    # description references the other account). Without this step the
    # opposite leg — on the other account — is never flagged, so it stays
    # in total_deposits/total_payments while only one side is subtracted
    # out in total_interbank_deposits/total_interbank_payments. That
    # imbalance is exactly what breaks Net Deposits / Net Payments (and
    # the global totals) when a Rule 5 match happens. We look for the
    # opposite-sign, matching-amount, nearby-date transaction on another
    # of the client's accounts, the same way Rule 3/4 does below, and
    # flag it too.
    counterpart_txs = []
    tx_by_id = {t.id: t for t in all_txs}
    for tx_id in list(rule5_confirmed_ids):
        tx = tx_by_id[tx_id]
        target_amount = abs(tx.amount)
        min_date, max_date = _business_day_window(tx.date, max_business_days=2)

        candidates = []
        for other in all_txs:
            if other.id == tx.id or other.id in rule5_confirmed_ids or other.id in permanently_excluded_ids:
                continue
            if other.account_id == tx.account_id:
                continue
            if not is_same_client(tx.account_id, other.account_id):
                continue
            if abs(other.amount) != target_amount:
                continue
            if (other.amount > 0) == (tx.amount > 0):
                continue  # must be the opposite side (inflow vs outflow)
            if not (min_date <= other.date <= max_date):
                continue
            candidates.append(other)

        if not candidates:
            continue

        candidates.sort(key=lambda c: abs((c.date - tx.date).days))
        best_gap = abs((candidates[0].date - tx.date).days)
        tied = [c for c in candidates if abs((c.date - tx.date).days) == best_gap]
        if len(tied) > 1:
            continue  # ambiguous — don't guess, leave for manual review

        counterpart = tied[0]
        counterpart.is_interbank = True
        counterpart.interbank_confidence = 'high'  # inferred from the matched leg, not its own description
        counterpart.interbank_match_status = 'confirmed_description_counterpart'
        rule5_confirmed_ids.add(counterpart.id)
        counterpart_txs.append(counterpart)

    if counterpart_txs:
        Transaction.objects.bulk_update(
            counterpart_txs, ['is_interbank', 'interbank_confidence', 'interbank_match_status']
        )

    # --- Rule 3/4: amount + business-day-window pairing ---
    # Candidate pool excludes anything permanently excluded or already
    # confirmed via Rule 5 (prevents double-matching / re-use).
    excluded_ids = permanently_excluded_ids | rule5_confirmed_ids
    outflows = [t for t in all_txs if t.amount < 0 and t.id not in excluded_ids]
    inflows = [t for t in all_txs if t.amount > 0 and t.id not in excluded_ids]

    matched_inflow_ids = set()
    matched_outflow_ids = set()
    paired_txs = []
    unmatched_ambiguous = []

    for outflow in outflows:
        if outflow.id in matched_outflow_ids:
            continue

        target_amount = abs(outflow.amount)
        min_date, max_date = _business_day_window(outflow.date, max_business_days=2)

        candidates = []
        for inflow in inflows:
            if inflow.id in matched_inflow_ids:
                continue
            if inflow.account_id == outflow.account_id:
                continue
            if not is_same_client(outflow.account_id, inflow.account_id):
                continue
            if inflow.amount != target_amount:
                continue
            if not (min_date <= inflow.date <= max_date):
                continue
            candidates.append(inflow)

        if not candidates:
            continue

        # Rank by date proximity; only auto-match if there's a single best
        # candidate. If multiple candidates tie on the closest date, don't
        # guess — leave for manual review instead of silently picking one.
        candidates.sort(key=lambda c: abs((c.date - outflow.date).days))
        best_gap = abs((candidates[0].date - outflow.date).days)
        tied = [c for c in candidates if abs((c.date - outflow.date).days) == best_gap]

        if len(tied) > 1:
            unmatched_ambiguous.append(outflow)
            outflow.interbank_match_status = 'ambiguous_multiple_candidates'
            paired_txs.append(outflow)
            continue

        best_match = tied[0]
        matched_inflow_ids.add(best_match.id)
        matched_outflow_ids.add(outflow.id)

        confidence = _date_confidence(outflow.date, best_match.date)
        for t in (outflow, best_match):
            t.is_interbank = True
            t.interbank_confidence = confidence
            t.interbank_match_status = 'confirmed_amount_date'
            paired_txs.append(t)

    if paired_txs:
        Transaction.objects.bulk_update(
            paired_txs, ['is_interbank', 'interbank_confidence', 'interbank_match_status']
        )

    from .parsers.manager import update_account_aggregates
    for account in user_accounts:
        update_account_aggregates(account)

    return {
        'rule5_confirmed': len(rule5_confirmed_ids),
        'rule3_4_confirmed_pairs': len(matched_outflow_ids),
        'ambiguous_unmatched': len(unmatched_ambiguous),
        'permanently_excluded': len(permanently_excluded_ids),
    }