"""
cashproof_ai/services/validation.py

§10 — Validation checks. Run before delivering the Excel file.
Returns a list of (check_name, expected, actual, pass_fail) tuples.
"""

import logging
from decimal import Decimal

from ..models import CashProofSession, ClientAccount, LedgerTransaction, InterbankMatch

logger = logging.getLogger(__name__)

CONFIRMED = 'CONFIRMED_INTERBANK'
TOLERANCE = Decimal('0.05')  # §10: balance reconciliation tolerance


def run_all_checks(session: CashProofSession) -> list[dict]:
    """
    Run every §10 check and return a list of result dicts:
      { 'check': str, 'expected': str, 'actual': str, 'status': 'PASS'|'FAIL'|'INFO' }
    """
    checks = []
    accounts = list(session.client_accounts.all())

    # ── Global checks ─────────────────────────────────────────────────────────

    # §10 #1 — Total transactions
    stmt_ids = list(session.statements.values_list('id', flat=True))
    total_txs = LedgerTransaction.objects.filter(statement_id__in=stmt_ids).count()
    stmt_counts = sum(
        s.transactions.count() for s in session.statements.all()
    )
    checks.append(_check(
        '1. Total transactions extracted',
        f'Sum of per-statement counts = {stmt_counts}',
        str(total_txs),
        total_txs == stmt_counts,
    ))

    # §10 #2 — Total credits vs. statement printed credits
    total_credits = Decimal('0')
    stmt_credits  = Decimal('0')
    for stmt in session.statements.all():
        if stmt.statement_total_credits is not None:
            stmt_credits += stmt.statement_total_credits
        total_credits += (
            stmt.transactions.filter(credit_amount__isnull=False)
                             .aggregate(s=_django_sum('credit_amount'))['s'] or Decimal('0')
        )
    if stmt_credits:
        checks.append(_check(
            '2. Total credits vs. statement printed total',
            f'${stmt_credits:,.2f}',
            f'${total_credits:,.2f}',
            abs(total_credits - stmt_credits) <= TOLERANCE,
        ))
    else:
        checks.append(_info('2. Total credits', f'Printed total not available; ledger total = ${total_credits:,.2f}'))

    # §10 #3 — Total debits vs. statement printed debits
    total_debits = Decimal('0')
    stmt_debits  = Decimal('0')
    for stmt in session.statements.all():
        if stmt.statement_total_debits is not None:
            stmt_debits += stmt.statement_total_debits
        total_debits += (
            stmt.transactions.filter(debit_amount__isnull=False)
                             .aggregate(s=_django_sum('debit_amount'))['s'] or Decimal('0')
        )
    if stmt_debits:
        checks.append(_check(
            '3. Total debits vs. statement printed total',
            f'${stmt_debits:,.2f}',
            f'${total_debits:,.2f}',
            abs(total_debits - stmt_debits) <= TOLERANCE,
        ))
    else:
        checks.append(_info('3. Total debits', f'Printed total not available; ledger total = ${total_debits:,.2f}'))

    # §10 #4 — Confirmed interbank credits
    ib_credits = sum(
        LedgerTransaction.objects.filter(
            statement_id__in=stmt_ids,
            interbank_status=CONFIRMED,
            credit_amount__isnull=False,
        ).aggregate(s=_django_sum('credit_amount'))['s'] or Decimal('0')
        for _ in [1]
    )
    ib_credits_val = (
        LedgerTransaction.objects.filter(
            statement_id__in=stmt_ids,
            interbank_status=CONFIRMED,
            credit_amount__isnull=False,
        ).aggregate(s=_django_sum('credit_amount'))['s'] or Decimal('0')
    )
    checks.append(_info('4. Confirmed interbank credits', f'${ib_credits_val:,.2f}'))

    # §10 #5 — Confirmed interbank debits must equal #4
    ib_debits_val = (
        LedgerTransaction.objects.filter(
            statement_id__in=stmt_ids,
            interbank_status=CONFIRMED,
            debit_amount__isnull=False,
        ).aggregate(s=_django_sum('debit_amount'))['s'] or Decimal('0')
    )
    checks.append(_check(
        '5. Confirmed IB debits == IB credits (balanced pairs)',
        f'${ib_credits_val:,.2f}',
        f'${ib_debits_val:,.2f}',
        abs(ib_credits_val - ib_debits_val) <= TOLERANCE,
    ))

    # §10 #6 — Net deposits
    net_deposits = sum(a.net_deposits for a in accounts)
    checks.append(_info('6. Net deposits (all accounts)', f'${net_deposits:,.2f}'))

    # §10 #7 — Net payments
    net_payments = sum(a.net_payments for a in accounts)
    checks.append(_info('7. Net payments (all accounts)', f'${net_payments:,.2f}'))

    # §10 #8 — Number of confirmed interbank matches
    confirmed_count = InterbankMatch.objects.filter(session=session, status=CONFIRMED).count()
    checks.append(_info('8. Confirmed interbank match pairs', str(confirmed_count)))

    # §10 #9 — Possible/unmatched
    possible_count = LedgerTransaction.objects.filter(
        statement_id__in=stmt_ids,
        interbank_status__in=['POSSIBLE_UNMATCHED', 'POSSIBLE_INTERBANK', 'AMBIGUOUS'],
    ).count()
    checks.append(_info('9. Possible / unmatched transfer flags', str(possible_count)))

    # §10 #10 — Accounts successfully mapped
    mapped   = sum(1 for a in accounts if a.mapping_status == 'MAPPED')
    unmapped = sum(1 for a in accounts if a.mapping_status != 'MAPPED')
    checks.append(_check(
        '10. Accounts successfully mapped to client master',
        f'0 unmapped',
        f'{unmapped} unmapped',
        unmapped == 0,
    ))
    if unmapped:
        unmapped_names = [a.account_name or a.account_number for a in accounts if a.mapping_status != 'MAPPED']
        checks.append(_info(
            '11. Accounts requiring manual mapping',
            ', '.join(unmapped_names) or 'None',
        ))

    # ── Per-account balance reconciliation ───────────────────────────────────
    for stmt in session.statements.all():
        beg = stmt.beginning_balance
        end = stmt.ending_balance
        if beg is None or end is None:
            checks.append(_info(
                f'Balance check: {stmt.filename()}',
                'Beginning or ending balance not available — cannot reconcile',
            ))
            continue

        ledger_credits = (
            stmt.transactions.filter(credit_amount__isnull=False)
                             .aggregate(s=_django_sum('credit_amount'))['s'] or Decimal('0')
        )
        ledger_debits = (
            stmt.transactions.filter(debit_amount__isnull=False)
                             .aggregate(s=_django_sum('debit_amount'))['s'] or Decimal('0')
        )
        calculated_end = beg + ledger_credits - ledger_debits
        diff = abs(calculated_end - end)
        checks.append(_check(
            f'Balance reconciliation: {stmt.filename()}',
            f'Beg {beg:,.2f} + Credits {ledger_credits:,.2f} − Debits {ledger_debits:,.2f} = {calculated_end:,.2f}',
            f'Statement ending balance = {end:,.2f}',
            diff <= TOLERANCE,
        ))

    return checks


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check(name: str, expected: str, actual: str, passed: bool) -> dict:
    return {
        'check':    name,
        'expected': expected,
        'actual':   actual,
        'status':   'PASS' if passed else 'FAIL',
    }


def _info(name: str, value: str) -> dict:
    return {
        'check':    name,
        'expected': '',
        'actual':   value,
        'status':   'INFO',
    }


def _django_sum(field):
    from django.db.models import Sum
    return Sum(field)
