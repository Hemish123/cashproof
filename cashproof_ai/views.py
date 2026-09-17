"""
cashproof_ai/views.py
"""

import os
import tempfile
import threading
import logging

from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connections

from .models import CashProofSession, ClientAccount, ParsedStatement, LedgerTransaction, InterbankMatch
from .services.ingestion import ingest_statement
from .services.account_master import build_account_master
from .services.interbank import run_interbank_matching
from .services.calculations import recalculate_session
from .reports import generate_report

logger = logging.getLogger(__name__)

LOGIN_URL = 'login'


# ── Background pipeline ───────────────────────────────────────────────────────

def _run_pipeline(session_id: int) -> None:
    """
    Background thread: ingest all statements → build master → match interbank → calculate.
    """
    try:
        from django.db import connection
        session = CashProofSession.objects.get(id=session_id)
        session.status = 'PROCESSING'
        session.save(update_fields=['status'])

        # 1. Ingest each uploaded statement
        for stmt in session.statements.filter(parse_status='PENDING'):
            ingest_statement(session, stmt)

        # 2. Build / update client account master
        build_account_master(session)

        # 3. Run §6 interbank matching
        run_interbank_matching(session)

        # 4. §8 recalculate net totals
        recalculate_session(session)

        # 5. Generate §11 final text report
        session.final_text_report = _build_text_report(session)

        session.status = 'COMPLETED'
        session.save(update_fields=['status', 'final_text_report'])
        logger.info(f'CashProof AI session #{session_id} completed successfully.')

    except Exception as exc:
        import traceback
        try:
            session = CashProofSession.objects.get(id=session_id)
            session.status = 'FAILED'
            session.error_message = f'{exc}\n\n{traceback.format_exc()}'
            session.save(update_fields=['status', 'error_message'])
        except Exception:
            pass
        logger.exception(f'CashProof AI session #{session_id} failed.')
    finally:
        connections.close_all()


def _build_text_report(session: CashProofSession) -> str:
    """§11 — Final text report."""
    from decimal import Decimal
    accounts = list(session.client_accounts.all())
    banks    = list({a.bank_name for a in accounts if a.bank_name})
    stmt_count = session.statements.count()

    total_deposits = sum(a.total_deposits for a in accounts)
    total_payments = sum(a.total_payments for a in accounts)
    net_deposits   = sum(a.net_deposits   for a in accounts)
    net_payments   = sum(a.net_payments   for a in accounts)

    confirmed  = session.interbank_matches.filter(status='CONFIRMED_INTERBANK')
    confirmed_count = confirmed.count()
    confirmed_total = sum(m.amount for m in confirmed) if confirmed_count else Decimal('0')

    from .models import LedgerTransaction
    stmt_ids = list(session.statements.values_list('id', flat=True))
    possible_qs = LedgerTransaction.objects.filter(
        statement_id__in=stmt_ids,
        interbank_status__in=['POSSIBLE_UNMATCHED', 'POSSIBLE_INTERBANK', 'AMBIGUOUS'],
        interbank_match__isnull=True,
    )
    possible_count = possible_qs.count()
    possible_total = sum(
        (t.debit_amount or t.credit_amount or Decimal('0')) for t in possible_qs
    )

    # Period
    start_dates = [s.period_start for s in session.statements.all() if s.period_start]
    end_dates   = [s.period_end   for s in session.statements.all() if s.period_end]
    period_str  = ''
    if start_dates and end_dates:
        period_str = (f'{min(start_dates).strftime("%B %d, %Y")} '
                      f'to {max(end_dates).strftime("%B %d, %Y")}')

    lines = [
        '═' * 72,
        'CASHPROOF AI — ANALYSIS REPORT',
        '═' * 72,
        '',
        f'  Client            : {session.client_name or "(not specified)"}',
        f'  Accounts analyzed : {len(accounts)}',
        f'  Banks analyzed    : {", ".join(banks) or "(none)"}',
        f'  Statements        : {stmt_count} uploaded file(s)',
        f'  Statement period  : {period_str or "varies"}',
        '',
        '── Financial Summary ────────────────────────────────────────────────',
        '',
        f'  Total Deposits (before interbank exclusion) : ${total_deposits:>15,.2f}',
        f'  Total Payments (before interbank exclusion) : ${total_payments:>15,.2f}',
        '',
        f'  Confirmed Interbank Transfers  : {confirmed_count} match pair(s), ${confirmed_total:,.2f}',
        f'  Possible / Unmatched Transfers : {possible_count} transaction(s), ${possible_total:,.2f}',
        '',
        f'  Net External Deposits : ${net_deposits:>15,.2f}',
        f'  Net External Payments : ${net_payments:>15,.2f}',
        '',
        '── Interbank Activity ───────────────────────────────────────────────',
        '',
    ]

    for m in confirmed:
        lines.append(
            f'  {m.match_id}  {m.match_date}  '
            f'{m.from_account} → {m.to_account}  '
            f'${m.amount:,.2f}  [{m.status}]'
        )
        lines.append(f'       Reason: {m.reason[:120]}')
        lines.append('')

    if possible_count:
        lines.append('── Possible / Unmatched — Action Required ───────────────────────────')
        lines.append('')
        for tx in possible_qs[:20]:
            lines.append(
                f'  [{tx.transaction_date}] {tx.bank_name} {tx.account_number} '
                f'${tx.debit_amount or tx.credit_amount or 0:,.2f}'
            )
            lines.append(f'       {tx.reason_for_classification[:120]}')
            lines.append('')

    unmapped = [a for a in accounts if a.mapping_status != 'MAPPED']
    if unmapped:
        lines.append('── Accounts Requiring Manual Mapping ────────────────────────────────')
        lines.append('')
        for a in unmapped:
            lines.append(f'  {a.bank_name} — {a.account_name} ({a.account_number})')
        lines.append('')

    lines.append('═' * 72)
    return '\n'.join(lines)


# ── Views ─────────────────────────────────────────────────────────────────────

@login_required(login_url=LOGIN_URL)
def session_list(request):
    """List all CashProof AI sessions for the logged-in user."""
    sessions = CashProofSession.objects.filter(user=request.user)
    return render(request, 'cashproof_ai/session_list.html', {'sessions': sessions})


@login_required(login_url=LOGIN_URL)
def new_session(request):
    """Create a new session and trigger background processing."""
    if request.method == 'POST':
        client_name = request.POST.get('client_name', '').strip()
        files = request.FILES.getlist('files')

        if not files:
            messages.error(request, 'Please upload at least one bank statement.')
            return redirect('cashproof_ai:new_session')

        if len(files) > 10:
            messages.error(request, 'Maximum 10 files per session.')
            return redirect('cashproof_ai:new_session')

        # Create session
        session = CashProofSession.objects.create(
            user=request.user,
            client_name=client_name,
            status='PENDING',
        )

        # Create ParsedStatement records
        for f in files:
            ParsedStatement.objects.create(session=session, file=f, parse_status='PENDING')

        # Fire background thread
        threading.Thread(target=_run_pipeline, args=(session.id,), daemon=True).start()

        messages.success(
            request,
            f'Session created with {len(files)} file(s). Processing in the background — '
            f'refresh the page to check status.'
        )
        return redirect('cashproof_ai:session_detail', session_id=session.id)

    return render(request, 'cashproof_ai/new_session.html')


@login_required(login_url=LOGIN_URL)
def session_detail(request, session_id: int):
    """Dashboard for a single session."""
    session = get_object_or_404(CashProofSession, id=session_id, user=request.user)
    accounts  = session.client_accounts.all().order_by('bank_name', 'account_number')
    ib_matches = session.interbank_matches.all().order_by('match_id')
    statements = session.statements.all().order_by('uploaded_at')

    stmt_ids = list(statements.values_list('id', flat=True))
    recent_txs = LedgerTransaction.objects.filter(
        statement_id__in=stmt_ids
    ).select_related('client_account', 'interbank_match').order_by('-transaction_date', '-id')[:200]

    from decimal import Decimal
    total_deposits = sum(a.total_deposits for a in accounts) if accounts else Decimal('0')
    total_payments = sum(a.total_payments for a in accounts) if accounts else Decimal('0')
    net_deposits   = sum(a.net_deposits   for a in accounts) if accounts else Decimal('0')
    net_payments   = sum(a.net_payments   for a in accounts) if accounts else Decimal('0')
    confirmed_ib   = ib_matches.filter(status='CONFIRMED_INTERBANK').count()

    context = {
        'session': session,
        'accounts': accounts,
        'ib_matches': ib_matches,
        'statements': statements,
        'recent_txs': recent_txs,
        'total_deposits': total_deposits,
        'total_payments': total_payments,
        'net_deposits':   net_deposits,
        'net_payments':   net_payments,
        'confirmed_ib':   confirmed_ib,
    }
    return render(request, 'cashproof_ai/session_detail.html', context)


@login_required(login_url=LOGIN_URL)
def session_status_api(request, session_id: int):
    """JSON polling endpoint."""
    session = get_object_or_404(CashProofSession, id=session_id, user=request.user)
    return JsonResponse({
        'status': session.status,
        'is_processing': session.status in ('PENDING', 'PROCESSING'),
        'error_message': session.error_message,
    })


@login_required(login_url=LOGIN_URL)
def download_report(request, session_id: int):
    """Generate and stream the §9 Excel workbook."""
    session = get_object_or_404(CashProofSession, id=session_id, user=request.user)

    if session.status != 'COMPLETED':
        messages.warning(request, 'The session has not completed processing yet.')
        return redirect('cashproof_ai:session_detail', session_id=session_id)

    with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp:
        tmp_path = tmp.name

    try:
        generate_report(session, tmp_path)
        with open(tmp_path, 'rb') as f:
            data = f.read()
        response = HttpResponse(
            data,
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        # Build filename matching QNB_KwikGoal_Cashflow_Report.xlsx pattern
        accounts    = session.client_accounts.all().order_by('bank_name')
        first_acct  = accounts.first()
        raw_bank    = (first_acct.bank_name if first_acct else '') or ''
        bank_part   = (raw_bank.split() or ['Bank'])[0].replace(' ', '')
        client_part = (session.client_name or 'Client').replace(' ', '')
        filename    = f'{bank_part}_{client_part}_Cashflow_Report.xlsx'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    except Exception as exc:
        logger.exception(f'Report generation failed for session #{session_id}')
        messages.error(request, f'Error generating report: {exc}')
        return redirect('cashproof_ai:session_detail', session_id=session_id)
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


@login_required(login_url=LOGIN_URL)
def delete_session(request, session_id: int):
    """Delete a session and all its data."""
    session = get_object_or_404(CashProofSession, id=session_id, user=request.user)

    if request.method == 'POST':
        # Delete uploaded files
        for stmt in session.statements.all():
            try:
                if stmt.file and os.path.exists(stmt.file.path):
                    os.remove(stmt.file.path)
            except Exception:
                pass
        session.delete()
        messages.success(request, 'Session deleted successfully.')
        return redirect('cashproof_ai:session_list')

    return render(request, 'cashproof_ai/delete_confirm.html', {'session': session})
