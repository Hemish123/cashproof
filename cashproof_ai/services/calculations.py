"""
cashproof_ai/services/calculations.py

§8 — Net calculations.
Computes per-account, per-month, and rolled-up totals.
Updates ClientAccount aggregates after interbank matching completes.
"""

import logging
from decimal import Decimal
from collections import defaultdict

from django.db.models import Sum

from ..models import CashProofSession, ClientAccount, LedgerTransaction

logger = logging.getLogger(__name__)

CONFIRMED_STATUS = 'CONFIRMED_INTERBANK'


# ── Public API ────────────────────────────────────────────────────────────────

def recalculate_session(session: CashProofSession) -> None:
    """
    Recalculate §8 totals for every ClientAccount in *session*.
    """
    for account in session.client_accounts.all():
        recalculate_account(account)


def recalculate_account(account: ClientAccount) -> None:
    """
    Recalculate §8 totals for a single ClientAccount and save.

    §8 Definitions:
      Total Deposits       = all credits (before interbank exclusion)
      Total Payments       = all debits  (before interbank exclusion)
      Total IB Deposits    = confirmed interbank credits
      Total IB Payments    = confirmed interbank debits
      Net Deposits         = Total Deposits  − Total IB Deposits
      Net Payments         = Total Payments  − Total IB Payments
    """
    txs = LedgerTransaction.objects.filter(client_account=account)

    total_deposits = (
        txs.filter(credit_amount__isnull=False, credit_amount__gt=0)
           .aggregate(s=Sum('credit_amount'))['s'] or Decimal('0')
    )
    total_payments = (
        txs.filter(debit_amount__isnull=False, debit_amount__gt=0)
           .aggregate(s=Sum('debit_amount'))['s'] or Decimal('0')
    )
    total_ib_deposits = (
        txs.filter(
            credit_amount__isnull=False,
            credit_amount__gt=0,
            interbank_status=CONFIRMED_STATUS,
        ).aggregate(s=Sum('credit_amount'))['s'] or Decimal('0')
    )
    total_ib_payments = (
        txs.filter(
            debit_amount__isnull=False,
            debit_amount__gt=0,
            interbank_status=CONFIRMED_STATUS,
        ).aggregate(s=Sum('debit_amount'))['s'] or Decimal('0')
    )

    account.total_deposits           = total_deposits
    account.total_payments           = total_payments
    account.total_interbank_deposits  = total_ib_deposits
    account.total_interbank_payments  = total_ib_payments
    account.net_deposits             = total_deposits  - total_ib_deposits
    account.net_payments             = total_payments  - total_ib_payments
    account.save(update_fields=[
        'total_deposits', 'total_payments',
        'total_interbank_deposits', 'total_interbank_payments',
        'net_deposits', 'net_payments',
    ])


def monthly_summary(session: CashProofSession) -> list[dict]:
    """
    Return a list of dicts, one per (account, year, month), sorted by date.
    Each dict has the §8 column set plus year/month/account info.
    """
    rows = []
    for account in session.client_accounts.all():
        monthly = _monthly_for_account(account)
        for (year, month), data in sorted(monthly.items()):
            rows.append({
                'client_name':               session.client_name,
                'bank_name':                 account.bank_name,
                'account_number':            account.account_number,
                'last4':                     account.last4,
                'account_name':              account.account_name,
                'year':                      year,
                'month':                     month,
                'total_deposits':            data['total_deposits'],
                'total_payments':            data['total_payments'],
                'total_interbank_deposits':  data['total_ib_deposits'],
                'total_interbank_payments':  data['total_ib_payments'],
                'net_deposits':              data['total_deposits'] - data['total_ib_deposits'],
                'net_payments':              data['total_payments'] - data['total_ib_payments'],
            })
    return rows


# ── Internal ──────────────────────────────────────────────────────────────────

def _monthly_for_account(account: ClientAccount) -> dict:
    """
    Group LedgerTransactions for *account* by (year, month).
    Returns { (year, month): {total_deposits, total_payments, ...} }
    """
    txs = LedgerTransaction.objects.filter(client_account=account)
    monthly: dict[tuple, dict] = defaultdict(lambda: {
        'total_deposits':   Decimal('0'),
        'total_payments':   Decimal('0'),
        'total_ib_deposits': Decimal('0'),
        'total_ib_payments': Decimal('0'),
    })

    for tx in txs:
        key = (tx.transaction_date.year, tx.transaction_date.month)
        if tx.credit_amount and tx.credit_amount > 0:
            monthly[key]['total_deposits'] += tx.credit_amount
            if tx.interbank_status == CONFIRMED_STATUS:
                monthly[key]['total_ib_deposits'] += tx.credit_amount
        if tx.debit_amount and tx.debit_amount > 0:
            monthly[key]['total_payments'] += tx.debit_amount
            if tx.interbank_status == CONFIRMED_STATUS:
                monthly[key]['total_ib_payments'] += tx.debit_amount

    return monthly
