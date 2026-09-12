from decimal import Decimal
import re
from datetime import timedelta
from django.db.models import Q
from .models import BankAccount, Transaction


def detect_interbank_transactions(user=None):


    """
    Scans the database to identify and flag interbank (self-transfer) transactions,
    then updates the calculated summary statistics for all accounts.

    Detection priority (in order):
      1. Reset existing flags (clean slate for this user's transactions)
      2. Keyword heuristics on description text
      3. Registered account number matching — transactions mentioning any of the
         user's registered UserBankAccount account numbers are flagged as self-transfers
      4. Cross-account amount & date pairing (same abs amount, ±2 days, different account)
      5. Recalculate aggregates for all affected accounts
    """

    # ── Scope to user's transactions if user is provided ──────────────────────
    if user is not None:
        user_upload_ids = user.uploads.values_list('id', flat=True)
        user_account_ids = BankAccount.objects.filter(
            upload_id__in=user_upload_ids
        ).values_list('id', flat=True)
        base_qs = Transaction.objects.filter(account_id__in=user_account_ids)
    else:
        base_qs = Transaction.objects.all()

    # 1. Reset all interbank flags for a clean recalculation
    base_qs.update(is_interbank=False)

    # 2. Text Keyword Heuristics
    keywords = [
        r'\btransfer\s+to\s+checking\b',
        r'\btransfer\s+from\s+checking\b',
        r'\btransfer\s+to\s+savings\b',
        r'\btransfer\s+from\s+savings\b',
        r'\btransfer\s+to\s+acct\b',
        r'\btransfer\s+from\s+acct\b',
        r'\btransfer\s+debit\b',
        r'\btransfer\s+credit\b',
        r'\bcbusol\s+transfer\b',
        r'\btransfer\s+to\b',
        r'\btransfer\s+from\b',
        r'\binternal\s+transfer\b',
        r'\binter-bank\s+transfer\b',
        r'\bown\s+account\s+transfer\b',
        r'\bself\s+transfer\b',
        r'\bzelle\s+to\s+self\b',
        r'\bzelle\s+from\s+self\b',
        r'\bcredit\s+card\s+payment\b',
        r'\bpayment\s+to\s+credit\s+card\b',
        r'\bautopay\s+credit\s+card\b',
        r'\btransfer\b',
        r'\bxfer\b',
        r'\bonline\s+transfer\b',
    ]
    keyword_pattern = re.compile('|'.join(keywords), re.IGNORECASE)

    all_txs = list(base_qs)
    keyword_flagged = []
    for tx in all_txs:
        if keyword_pattern.search(tx.description):
            tx.is_interbank = True
            tx.category = 'Interbank Self-Transfer'
            keyword_flagged.append(tx)

    if keyword_flagged:
        Transaction.objects.bulk_update(keyword_flagged, ['is_interbank', 'category'])

    # 3. Registered Account Number Matching ───────────────────────────────────
    # If a user has registered their own bank accounts, flag any transaction
    # whose description contains one of those account numbers as a self-transfer.
    if user is not None:
        from user_auth.models import UserBankAccount

        registered_accounts = list(
            UserBankAccount.objects.filter(user=user).values('account_number', 'bank_name')
        )

        # Build a list of cleaned account number patterns to search for
        acct_patterns = []
        for reg in registered_accounts:
            raw = reg['account_number'].strip()
            # Normalised (no spaces/dashes)
            cleaned = re.sub(r'[\s\-]', '', raw)
            if cleaned:
                acct_patterns.append(cleaned)
            # Also add last-4 digits as a word-boundary pattern (e.g. "...1234")
            if len(cleaned) >= 4:
                acct_patterns.append(cleaned[-4:])

        if acct_patterns:
            # Reload fresh from DB to get accurate is_interbank state
            acct_flagged = []
            for tx in list(base_qs.all()):
                desc_cleaned = re.sub(r'[\s\-]', '', tx.description)
                for pat in acct_patterns:
                    # Full account number: substring match on cleaned string
                    if len(pat) > 4 and pat in desc_cleaned:
                        tx.is_interbank = True
                        tx.category = 'Interbank Self-Transfer'
                        acct_flagged.append(tx)
                        break
                    # Last-4 digits: word-boundary match on original description
                    if len(pat) == 4 and re.search(rf'(?<!\d){re.escape(pat)}(?!\d)', tx.description):
                        tx.is_interbank = True
                        tx.category = 'Interbank Self-Transfer'
                        acct_flagged.append(tx)
                        break

            if acct_flagged:
                Transaction.objects.bulk_update(acct_flagged, ['is_interbank', 'category'])

    # 4. Cross-Account Amount & Date Pairing ──────────────────────────────────
    # Matches an outflow on one account with an equal-amount inflow on a
    # different account within ±2 days — scoped to the user's uploads.
    outflows = base_qs.filter(amount__lt=0).order_by('date')
    inflows  = base_qs.filter(amount__gt=0).order_by('date')

    matched_inflow_ids = set()
    paired_txs = []

    for outflow in outflows:
        target_amount = abs(outflow.amount)
        min_date = outflow.date - timedelta(days=2)
        max_date = outflow.date + timedelta(days=2)

        matching_inflows = inflows.filter(
            amount=target_amount,
            date__range=(min_date, max_date),
        ).exclude(
            account=outflow.account
        ).exclude(
            id__in=matched_inflow_ids
        )

        if matching_inflows.exists():
            best_match = matching_inflows.first()
            matched_inflow_ids.add(best_match.id)

            outflow.is_interbank = True
            outflow.category = 'Interbank Self-Transfer'
            best_match.is_interbank = True
            best_match.category = 'Interbank Self-Transfer'

            paired_txs.append(outflow)
            paired_txs.append(best_match)

    if paired_txs:
        Transaction.objects.bulk_update(paired_txs, ['is_interbank', 'category'])

    # 5. Recalculate Aggregates for all affected accounts ─────────────────────
    from .parsers.manager import update_account_aggregates

    if user is not None:
        affected_accounts = BankAccount.objects.filter(upload_id__in=user_upload_ids)
    else:
        affected_accounts = BankAccount.objects.all()

    for account in affected_accounts:
        update_account_aggregates(account)
