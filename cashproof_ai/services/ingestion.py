# """
# cashproof_ai/services/ingestion.py

# File ingestion pipeline (§2 + §7).
#   1. Dispatch file to the appropriate parser.
#   2. Deduplicate transactions via §7 fingerprinting.
#   3. Write ParsedStatement + LedgerTransaction rows atomically.
# """

# import hashlib
# import logging
# import traceback
# from decimal import Decimal

# from django.db import transaction as db_transaction

# from ..models import CashProofSession, ParsedStatement, LedgerTransaction, ClientAccount
# from ..parsers.dispatcher import dispatch

# logger = logging.getLogger(__name__)


# # ── Public API ────────────────────────────────────────────────────────────────

# def ingest_statement(session: CashProofSession, statement: ParsedStatement) -> ParsedStatement:
#     """
#     Parse *statement.file*, deduplicate, and persist transactions.
#     Updates statement.parse_status on success or failure.
#     """
#     statement.parse_status = 'PROCESSING'
#     statement.save(update_fields=['parse_status'])

#     try:
#         file_path = statement.file.path
#         account_meta, raw_transactions = dispatch(file_path)

#         # Populate extracted header fields on the statement
#         _populate_statement_meta(statement, account_meta, file_path)
#         statement.save()

#         # Get existing fingerprints for this session (§7 duplicate prevention)
#         session_tx_ids = ParsedStatement.objects.filter(session=session).values_list('id', flat=True)
#         existing_fingerprints = set(
#             LedgerTransaction.objects.filter(
#                 statement_id__in=session_tx_ids
#             ).values_list('fingerprint', flat=True)
#         )

#         # Build and persist LedgerTransaction rows
#         tx_instances = []
#         for tx_data in raw_transactions:
#             fp = _make_fingerprint(session, statement, tx_data)
#             if fp in existing_fingerprints:
#                 continue  # §7: skip exact duplicate
#             existing_fingerprints.add(fp)

#             tx = _build_ledger_tx(statement, tx_data, fp)
#             tx_instances.append(tx)

#         with db_transaction.atomic():
#             LedgerTransaction.objects.bulk_create(tx_instances, batch_size=500)

#         statement.parse_status = 'COMPLETED'
#         statement.save(update_fields=['parse_status'])
#         logger.info(f'Ingested {len(tx_instances)} transactions from {statement.filename()}')

#     except Exception as exc:
#         statement.parse_status = 'FAILED'
#         statement.error_message = f'{exc}\n\n{traceback.format_exc()}'
#         statement.save(update_fields=['parse_status', 'error_message'])
#         logger.error(f'Ingestion failed for statement #{statement.pk}: {exc}')

#     return statement


# # ── Helpers ───────────────────────────────────────────────────────────────────

# def _populate_statement_meta(stmt: ParsedStatement, meta: dict, file_path: str) -> None:
#     import os
#     stmt.client_name_extracted   = meta.get('client_name', '') or ''
#     stmt.bank_name_extracted     = meta.get('bank_name', '') or ''
#     stmt.account_number_extracted = meta.get('account_number', '') or ''
#     stmt.last4_extracted         = meta.get('last4', '') or ''
#     stmt.account_title_extracted = meta.get('account_title', '') or ''
#     stmt.account_type_extracted  = meta.get('account_type', '') or ''
#     stmt.currency_extracted      = meta.get('currency', 'USD') or 'USD'
#     stmt.period_start            = meta.get('period_start')
#     stmt.period_end              = meta.get('period_end')

#     beg = meta.get('beginning_balance')
#     end = meta.get('ending_balance')
#     stmt.beginning_balance       = Decimal(str(beg)) if beg is not None else None
#     stmt.ending_balance          = Decimal(str(end)) if end is not None else None

#     sc = meta.get('statement_total_credits')
#     sd = meta.get('statement_total_debits')
#     stmt.statement_total_credits = Decimal(str(sc)) if sc is not None else None
#     stmt.statement_total_debits  = Decimal(str(sd)) if sd is not None else None


# def _make_fingerprint(session: CashProofSession,
#                        stmt: ParsedStatement,
#                        tx: dict) -> str:
#     """
#     §7 — Build a deterministic fingerprint:
#     Client + Account + Date + Amount + Description + Reference + Direction
#     """
#     client  = session.client_name or ''
#     acct    = stmt.account_number_extracted or ''
#     d       = str(tx.get('transaction_date', ''))
#     amt     = str(tx.get('net_amount', ''))
#     desc    = (tx.get('description') or '')[:100]
#     ref     = tx.get('reference_number', '') or ''
#     dirn    = 'CR' if (tx.get('credit_amount') or 0) > 0 else 'DR'
#     raw     = f'{client}|{acct}|{d}|{amt}|{desc}|{ref}|{dirn}'
#     return hashlib.sha256(raw.encode()).hexdigest()[:64]


# def _build_ledger_tx(stmt: ParsedStatement, tx: dict, fingerprint: str) -> LedgerTransaction:
#     """Construct an unsaved LedgerTransaction from a parser output dict."""
#     session = stmt.session
#     period_str = ''
#     if stmt.period_start and stmt.period_end:
#         period_str = f"{stmt.period_start.strftime('%b %d, %Y')} – {stmt.period_end.strftime('%b %d, %Y')}"

#     credit_amount = tx.get('credit_amount')
#     debit_amount  = tx.get('debit_amount')
#     net_amount    = tx.get('net_amount') or Decimal('0')

#     return LedgerTransaction(
#         statement=stmt,
#         client_account=stmt.client_account,  # may be None until account_master runs

#         # §3 header fields
#         client_name      = session.client_name or stmt.client_name_extracted or '',
#         bank_name        = stmt.bank_name_extracted or '',
#         account_number   = stmt.account_number_extracted or '',
#         last4            = stmt.last4_extracted or '',
#         account_title    = stmt.account_title_extracted or 'Operating Account',
#         statement_period = period_str,

#         # Transaction fields
#         transaction_date  = tx['transaction_date'],
#         value_date        = tx.get('value_date'),
#         description       = tx.get('description', '') or '',
#         reference_number  = tx.get('reference_number', '') or '',
#         check_number      = tx.get('check_number', '') or '',

#         debit_amount  = Decimal(str(debit_amount))  if debit_amount  is not None else None,
#         credit_amount = Decimal(str(credit_amount)) if credit_amount is not None else None,
#         net_amount    = Decimal(str(net_amount)),

#         transaction_type  = tx.get('transaction_type', 'I') or 'I',
#         counterparty_info = tx.get('counterparty_info', '') or '',
#         source_file       = stmt.filename(),
#         source_page       = tx.get('source_page', '') or '',

#         # Interbank defaults
#         interbank_status           = 'NOT_INTERBANK',
#         interbank_confidence_score = None,
#         interbank_matched_account  = '',
#         interbank_match            = None,
#         reason_for_classification  = '',

#         fingerprint = fingerprint,
#     )



"""
cashproof_ai/services/ingestion.py

File ingestion pipeline (§2 + §7).
  1. Dispatch file to the appropriate parser.
  2. Deduplicate transactions via §7 fingerprinting.
  3. Write ParsedStatement + LedgerTransaction rows atomically.
"""

import hashlib
import logging
import traceback
from decimal import Decimal

from django.db import transaction as db_transaction

from ..models import CashProofSession, ParsedStatement, LedgerTransaction, ClientAccount
from ..parsers.dispatcher import dispatch

logger = logging.getLogger(__name__)


# ── Public API ────────────────────────────────────────────────────────────────

def ingest_statement(session: CashProofSession, statement: ParsedStatement) -> ParsedStatement:
    """
    Parse *statement.file*, deduplicate, and persist transactions.
    Updates statement.parse_status on success or failure.
    """
    statement.parse_status = 'PROCESSING'
    statement.save(update_fields=['parse_status'])

    try:
        file_path = statement.file.path
        account_meta, raw_transactions = dispatch(file_path)

        # Populate extracted header fields on the statement
        _populate_statement_meta(statement, account_meta, file_path)
        statement.save()

        # Get fingerprints belonging to OTHER, already-ingested statements in
        # this session (§7 — catches the same transaction re-appearing across
        # two uploaded documents, e.g. an overlapping statement period or a
        # reconciliation statement duplicating a raw one).
        session_tx_ids = ParsedStatement.objects.filter(session=session).exclude(
            pk=statement.pk
        ).values_list('id', flat=True)
        cross_statement_fingerprints = set(
            LedgerTransaction.objects.filter(
                statement_id__in=session_tx_ids
            ).values_list('fingerprint', flat=True)
        )

        # Build and persist LedgerTransaction rows.
        # IMPORTANT: never check a transaction against fingerprints produced
        # by THIS SAME statement. Two rows on one printed statement that
        # share date+description+amount are common (e.g. several identical
        # $15.50 payroll entries posted the same day under the same batch
        # tracer) and are each genuine — only cross-statement repeats are
        # duplicates.
        tx_instances = []
        for tx_data in raw_transactions:
            fp = _make_fingerprint(session, statement, tx_data)
            if fp in cross_statement_fingerprints:
                continue  # §7: skip — already ingested from a different statement

            tx = _build_ledger_tx(statement, tx_data, fp)
            tx_instances.append(tx)

        with db_transaction.atomic():
            LedgerTransaction.objects.bulk_create(tx_instances, batch_size=500)

        statement.parse_status = 'COMPLETED'
        statement.save(update_fields=['parse_status'])
        logger.info(f'Ingested {len(tx_instances)} transactions from {statement.filename()}')

    except Exception as exc:
        statement.parse_status = 'FAILED'
        statement.error_message = f'{exc}\n\n{traceback.format_exc()}'
        statement.save(update_fields=['parse_status', 'error_message'])
        logger.error(f'Ingestion failed for statement #{statement.pk}: {exc}')

    return statement


# ── Helpers ───────────────────────────────────────────────────────────────────

def _populate_statement_meta(stmt: ParsedStatement, meta: dict, file_path: str) -> None:
    import os
    stmt.client_name_extracted   = meta.get('client_name', '') or ''
    stmt.bank_name_extracted     = meta.get('bank_name', '') or ''
    stmt.account_number_extracted = meta.get('account_number', '') or ''
    stmt.last4_extracted         = meta.get('last4', '') or ''
    stmt.account_title_extracted = meta.get('account_title', '') or ''
    stmt.account_type_extracted  = meta.get('account_type', '') or ''
    stmt.currency_extracted      = meta.get('currency', 'USD') or 'USD'
    stmt.period_start            = meta.get('period_start')
    stmt.period_end              = meta.get('period_end')

    beg = meta.get('beginning_balance')
    end = meta.get('ending_balance')
    stmt.beginning_balance       = Decimal(str(beg)) if beg is not None else None
    stmt.ending_balance          = Decimal(str(end)) if end is not None else None

    sc = meta.get('statement_total_credits')
    sd = meta.get('statement_total_debits')
    stmt.statement_total_credits = Decimal(str(sc)) if sc is not None else None
    stmt.statement_total_debits  = Decimal(str(sd)) if sd is not None else None


def _make_fingerprint(session: CashProofSession,
                       stmt: ParsedStatement,
                       tx: dict) -> str:
    """
    §7 — Build a deterministic fingerprint:
    Client + Account + Date + Amount + Description + Reference + Direction
    """
    client  = session.client_name or ''
    acct    = stmt.account_number_extracted or ''
    d       = str(tx.get('transaction_date', ''))
    amt     = str(tx.get('net_amount', ''))
    desc    = (tx.get('description') or '')[:100]
    ref     = tx.get('reference_number', '') or ''
    dirn    = 'CR' if (tx.get('credit_amount') or 0) > 0 else 'DR'
    raw     = f'{client}|{acct}|{d}|{amt}|{desc}|{ref}|{dirn}'
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


def _build_ledger_tx(stmt: ParsedStatement, tx: dict, fingerprint: str) -> LedgerTransaction:
    """Construct an unsaved LedgerTransaction from a parser output dict."""
    session = stmt.session
    period_str = ''
    if stmt.period_start and stmt.period_end:
        period_str = f"{stmt.period_start.strftime('%b %d, %Y')} – {stmt.period_end.strftime('%b %d, %Y')}"

    credit_amount = tx.get('credit_amount')
    debit_amount  = tx.get('debit_amount')
    net_amount    = tx.get('net_amount') or Decimal('0')

    return LedgerTransaction(
        statement=stmt,
        client_account=stmt.client_account,  # may be None until account_master runs

        # §3 header fields
        client_name      = session.client_name or stmt.client_name_extracted or '',
        bank_name        = stmt.bank_name_extracted or '',
        account_number   = stmt.account_number_extracted or '',
        last4            = stmt.last4_extracted or '',
        account_title    = stmt.account_title_extracted or 'Operating Account',
        statement_period = period_str,

        # Transaction fields
        transaction_date  = tx['transaction_date'],
        value_date        = tx.get('value_date'),
        description       = tx.get('description', '') or '',
        reference_number  = tx.get('reference_number', '') or '',
        check_number      = tx.get('check_number', '') or '',

        debit_amount  = Decimal(str(debit_amount))  if debit_amount  is not None else None,
        credit_amount = Decimal(str(credit_amount)) if credit_amount is not None else None,
        net_amount    = Decimal(str(net_amount)),

        transaction_type  = tx.get('transaction_type', 'I') or 'I',
        counterparty_info = tx.get('counterparty_info', '') or '',
        source_file       = stmt.filename(),
        source_page       = tx.get('source_page', '') or '',

        # Interbank defaults
        interbank_status           = 'NOT_INTERBANK',
        interbank_confidence_score = None,
        interbank_matched_account  = '',
        interbank_match            = None,
        reason_for_classification  = '',

        fingerprint = fingerprint,
    )