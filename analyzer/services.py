from decimal import Decimal
import re
from datetime import timedelta
from django.db.models import Q
from .models import BankAccount, Transaction

def detect_interbank_transactions():
    """
    Scans the database to identify and flag interbank (self-transfer) transactions,
    then updates the calculated summary statistics for all accounts.
    """
    # 1. Reset all transaction interbank flags to re-calculate clean state
    Transaction.objects.all().update(is_interbank=False)

    # 2. Text Keyword Heuristics
    # Compile regex pattern of self-transfer/internal keywords
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
    pattern = re.compile('|'.join(keywords), re.IGNORECASE)

    # Flag transactions matching text patterns
    all_txs = list(Transaction.objects.all())
    for tx in all_txs:
        if pattern.search(tx.description):
            tx.is_interbank = True
            tx.category = 'Interbank Self-Transfer'

    # Save initial keyword-flagged transactions
    Transaction.objects.bulk_update([t for t in all_txs if t.is_interbank], ['is_interbank', 'category'])

    # 3. Cross-Account Amount & Date Pairing Algorithm
    # Refetch fresh transactions to proceed with pairing
    outflows = Transaction.objects.filter(amount__lt=0).order_by('date')
    inflows = Transaction.objects.filter(amount__gt=0).order_by('date')

    matched_inflow_ids = set()
    updated_txs = []

    for outflow in outflows:
        # Find potential matching inflows
        # Criteria: different bank account, absolute amount matches, date within +/- 2 days
        target_amount = abs(outflow.amount)
        min_date = outflow.date - timedelta(days=2)
        max_date = outflow.date + timedelta(days=2)

        matching_inflows = inflows.filter(
            amount=target_amount,
            date__range=(min_date, max_date)
        ).exclude(
            account=outflow.account
        ).exclude(
            id__in=matched_inflow_ids
        )

        if matching_inflows.exists():
            # Pair them up
            best_match = matching_inflows.first()
            matched_inflow_ids.add(best_match.id)
            
            # Flag both as interbank transfers
            outflow.is_interbank = True
            outflow.category = 'Interbank Self-Transfer'
            best_match.is_interbank = True
            best_match.category = 'Interbank Self-Transfer'
            
            updated_txs.append(outflow)
            updated_txs.append(best_match)

    if updated_txs:
        # Bulk save paired transactions in the database
        Transaction.objects.bulk_update(updated_txs, ['is_interbank', 'category'])

    # 4. Recalculate Aggregates for all accounts
    from .parsers.manager import update_account_aggregates
    for account in BankAccount.objects.all():
        update_account_aggregates(account)
