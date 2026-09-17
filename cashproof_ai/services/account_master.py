"""
cashproof_ai/services/account_master.py

Builds / updates the Client Account Master (§1).
Maps ParsedStatement records to ClientAccount records.
"""

import re
import logging

from ..models import CashProofSession, ClientAccount, ParsedStatement

logger = logging.getLogger(__name__)


def _clean(s: str) -> str:
    return re.sub(r'[\s\-]', '', s or '').lower()


def _last4(account_number: str) -> str:
    cleaned = _clean(account_number)
    return cleaned[-4:] if len(cleaned) >= 4 else cleaned


# ── Public API ────────────────────────────────────────────────────────────────

def build_account_master(session: CashProofSession) -> list[ClientAccount]:
    """
    Scan all ParsedStatement objects for *session*, create or update
    ClientAccount records, then link each statement back to its account.

    Rules (§1):
      - Primary match key: last4 + bank_name (normalised).
      - Secondary key: bank_name + account_title + period (for masked statements).
      - If confidence insufficient: mark statement mapping_status = "Account Mapping Required".
      - Never guess — never fail the whole run over one unmapped document.
    """
    statements = list(session.statements.all())
    accounts_map: dict[str, ClientAccount] = {}  # key → ClientAccount

    for stmt in statements:
        match_key, confidence = _compute_match_key(stmt)
        if confidence == 'low':
            stmt_account = _get_or_create_account(session, stmt, mapping_status='Account Mapping Required')
        else:
            if match_key in accounts_map:
                stmt_account = accounts_map[match_key]
                # Widen period if needed
                _widen_period(stmt_account, stmt)
                stmt_account.save(update_fields=['period_start', 'period_end'])
            else:
                stmt_account = _get_or_create_account(session, stmt, mapping_status='MAPPED')
                accounts_map[match_key] = stmt_account

        stmt.client_account = stmt_account
        stmt.save(update_fields=['client_account'])

        # Back-fill LedgerTransaction FK
        stmt.transactions.filter(client_account__isnull=True).update(client_account=stmt_account)

    return list(session.client_accounts.all())


def _compute_match_key(stmt: ParsedStatement) -> tuple[str, str]:
    """
    Return (key, confidence).
    confidence: 'high' | 'medium' | 'low'
    """
    bank  = _clean(stmt.bank_name_extracted)
    last4 = _last4(stmt.account_number_extracted)
    title = _clean(stmt.account_title_extracted)

    if bank and last4:
        return f'{bank}::{last4}', 'high'
    if bank and title and stmt.period_start:
        period = stmt.period_start.strftime('%Y%m') if stmt.period_start else ''
        return f'{bank}::{title}::{period}', 'medium'
    return f'unknown::{stmt.pk}', 'low'


def _get_or_create_account(session: CashProofSession,
                            stmt: ParsedStatement,
                            mapping_status: str) -> ClientAccount:
    """
    Find an existing ClientAccount in *session* that matches *stmt*, or create one.
    """
    bank  = stmt.bank_name_extracted or ''
    acct  = stmt.account_number_extracted or ''
    l4    = _last4(acct) or stmt.last4_extracted or ''
    title = stmt.account_title_extracted or 'Operating Account'
    atype = stmt.account_type_extracted or 'checking'
    # Normalise account_type to one of the model choices
    atype = _normalise_account_type(atype)

    # Try to find existing account
    existing_qs = ClientAccount.objects.filter(
        session=session,
        bank_name__iexact=bank,
    )
    if l4:
        existing_qs = existing_qs.filter(last4=l4)

    existing = existing_qs.first()
    if existing:
        _widen_period(existing, stmt)
        existing.save(update_fields=['period_start', 'period_end'])
        return existing

    # Create new
    account = ClientAccount.objects.create(
        session=session,
        client_name=stmt.client_name_extracted or session.client_name or '',
        bank_name=bank,
        account_name=title,
        account_number=acct,
        last4=l4,
        account_type=atype,
        currency=stmt.currency_extracted or 'USD',
        period_start=stmt.period_start,
        period_end=stmt.period_end,
        mapping_status=mapping_status,
    )
    return account


def _widen_period(account: ClientAccount, stmt: ParsedStatement) -> None:
    if stmt.period_start and (account.period_start is None or stmt.period_start < account.period_start):
        account.period_start = stmt.period_start
    if stmt.period_end and (account.period_end is None or stmt.period_end > account.period_end):
        account.period_end = stmt.period_end


def _normalise_account_type(raw: str) -> str:
    raw_lower = raw.lower()
    if 'saving' in raw_lower:
        return 'savings'
    if 'money' in raw_lower or 'market' in raw_lower or 'mm' in raw_lower:
        return 'money_market'
    if 'payroll' in raw_lower:
        return 'payroll'
    if 'escrow' in raw_lower:
        return 'escrow'
    if 'checking' in raw_lower or 'current' in raw_lower:
        return 'checking'
    return 'other'
