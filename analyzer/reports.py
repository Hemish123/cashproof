import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from decimal import Decimal
import re
from datetime import datetime

def format_period(start_date, end_date):
    if not start_date or not end_date:
        return "Unknown"
    start_str = start_date.strftime("%b %d")
    if start_date.year == end_date.year:
        if start_date.month == end_date.month:
            return f"{start_str} - {end_date.strftime('%b %d')}, {start_date.year}"
        else:
            return f"{start_str} - {end_date.strftime('%b %d')}, {start_date.year}"
    else:
        return f"{start_date.strftime('%b %d, %Y')} - {end_date.strftime('%b %d, %Y')}"

def format_statement_dates(start_date, end_date):
    if not start_date or not end_date:
        return "Statement Dates Unknown"
    return f"Statement Dates {start_date.strftime('%m/%d/%y')} thru {end_date.strftime('%m/%d/%y')}"

def generate_excel_report(bank_accounts, output_path):
    """
    Generates a standardized Excel report matching QNB_KwikGoal format:
    1. Executive Summary
    2. Transaction Ledger
    3. Individual Account Tabs
    """
    wb = openpyxl.Workbook()
    # Remove default sheet
    default_sheet = wb.active
    wb.remove(default_sheet)

    # 1. Create Executive Summary Sheet
    ws_summary = wb.create_sheet(title="Executive Summary")
    ws_summary.views.sheetView[0].showGridLines = True

    # 2. Create Transaction Ledger Sheet
    ws_ledger = wb.create_sheet(title="Transaction Ledger")
    ws_ledger.views.sheetView[0].showGridLines = True

    # ---- Style definitions ----
    font_title = Font(name="Segoe UI", size=16, bold=True, color="1F4E79")
    font_subtitle = Font(name="Segoe UI", size=10, italic=True, color="595959")
    font_header = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
    font_body = Font(name="Segoe UI", size=11)
    font_total = Font(name="Segoe UI", size=11, bold=True)
    font_dimmed = Font(name="Segoe UI", size=10, italic=True, color="7F7F7F")

    fill_header_summary = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
    fill_header_ledger = PatternFill(start_color="2F5597", end_color="2F5597", fill_type="solid")
    fill_zebra = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
    fill_total = PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid")
    fill_interbank = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid") # light yellow

    thin_border_side = Side(style='thin', color='D9D9D9')
    border_all = Border(left=thin_border_side, right=thin_border_side, top=thin_border_side, bottom=thin_border_side)
    
    double_bottom = Border(top=Side(style='thin', color='000000'), bottom=Side(style='double', color='000000'))

    align_center = Alignment(horizontal="center", vertical="center")
    align_left = Alignment(horizontal="left", vertical="center")
    align_right = Alignment(horizontal="right", vertical="center")

    num_format_currency = '#,##0.00;(#,##0.00);"-";@'

    company_name = "Kwik Goal LTD"
    if bank_accounts and bank_accounts[0].account_holder:
        company_name = bank_accounts[0].account_holder

    primary_bank = bank_accounts[0].bank_name if bank_accounts else "Unknown Bank"

    # ==================== BUILD EXECUTIVE SUMMARY ====================
    ws_summary["A1"] = "Bank Statement Cash Flow Analysis Report"
    ws_summary["A1"].font = font_title
    ws_summary["A2"] = f"Executive Aggregates & Standardization Summary — {company_name} ({primary_bank})"
    ws_summary["A2"].font = font_subtitle

    # Table Headers starting at row 4
    summary_headers = [
        "Account", "Bank Name", "Account Number", "Total Deposits", "Total Payments",
        "Total Interbank Deposits", "Total Interbank Payments",
        "Net Deposits", "Net Payments"
    ]
    
    for col_idx, header in enumerate(summary_headers, start=1):
        cell = ws_summary.cell(row=4, column=col_idx, value=header)
        cell.font = font_header
        cell.fill = fill_header_summary
        cell.alignment = align_center
        cell.border = border_all
    ws_summary.row_dimensions[4].height = 28

    current_row = 5
    totals = {
        'deposits': Decimal('0.00'), 'payments': Decimal('0.00'),
        'ib_deposits': Decimal('0.00'), 'ib_payments': Decimal('0.00'),
        'net_deposits': Decimal('0.00'), 'net_payments': Decimal('0.00')
    }

    # Populating summary rows
    for idx, acc in enumerate(bank_accounts):
        row_data = [
            acc.account_title or "Operating Account",
            acc.bank_name,
            acc.account_number,
            acc.total_deposits,
            acc.total_payments,
            acc.total_interbank_deposits,
            acc.total_interbank_payments,
            acc.net_deposits,
            acc.net_payments
        ]

        # Add to combined totals
        totals['deposits'] += acc.total_deposits
        totals['payments'] += acc.total_payments
        totals['ib_deposits'] += acc.total_interbank_deposits
        totals['ib_payments'] += acc.total_interbank_payments
        totals['net_deposits'] += acc.net_deposits
        totals['net_payments'] += acc.net_payments

        for col_idx, val in enumerate(row_data, start=1):
            cell = ws_summary.cell(row=current_row, column=col_idx, value=val)
            cell.font = font_body
            cell.border = border_all
            
            # Format numbers vs text
            if col_idx in (1, 2, 3):
                cell.alignment = align_left
            else:
                cell.alignment = align_right
                cell.number_format = num_format_currency

            # Zebra stripe
            if idx % 2 == 1:
                cell.fill = fill_zebra
        
        ws_summary.row_dimensions[current_row].height = 20
        current_row += 1

    # Add Totals Row
    ws_summary.cell(row=current_row, column=1, value="Combined Total").font = font_total
    ws_summary.cell(row=current_row, column=1).alignment = align_left
    ws_summary.cell(row=current_row, column=1).border = double_bottom
    ws_summary.cell(row=current_row, column=1).fill = fill_total
    
    ws_summary.cell(row=current_row, column=2, value="").border = double_bottom
    ws_summary.cell(row=current_row, column=2).fill = fill_total

    ws_summary.cell(row=current_row, column=3, value="").border = double_bottom
    ws_summary.cell(row=current_row, column=3).fill = fill_total

    total_keys = ['deposits', 'payments', 'ib_deposits', 'ib_payments', 'net_deposits', 'net_payments']
    for idx, key in enumerate(total_keys, start=4):
        cell = ws_summary.cell(row=current_row, column=idx, value=totals[key])
        cell.font = font_total
        cell.alignment = align_right
        cell.number_format = num_format_currency
        cell.border = double_bottom
        cell.fill = fill_total

    ws_summary.row_dimensions[current_row].height = 22

    # Add Source Statement Reconciliation under totals
    current_row += 2 # Leave 2 empty rows after Combined Totals

    for acc in bank_accounts:
        txs = list(acc.transactions.all().order_by('date', 'id'))
        beginning_balance = acc.beginning_balance
        ending_balance = acc.ending_balance

        calculated_ending = beginning_balance + acc.total_deposits - acc.total_payments
        diff = abs(calculated_ending - ending_balance)
        recon_status = "PASS" if diff <= 0.05 else "FAIL"
        
        period_str = format_period(acc.start_date, acc.end_date)
        
        # Write reconciliation metrics block
        ws_summary.cell(row=current_row, column=1, value="Source Statement").font = font_total
        ws_summary.cell(row=current_row, column=2, value=f"{acc.bank_name} — {period_str}").font = font_body
        current_row += 1
        
        ws_summary.cell(row=current_row, column=1, value="Beginning Balance").font = font_total
        cell_beg = ws_summary.cell(row=current_row, column=2, value=beginning_balance)
        cell_beg.font = font_body
        cell_beg.number_format = num_format_currency
        cell_beg.alignment = align_right
        current_row += 1
        
        ws_summary.cell(row=current_row, column=1, value="Ending Balance").font = font_total
        cell_end = ws_summary.cell(row=current_row, column=2, value=ending_balance)
        cell_end.font = font_body
        cell_end.number_format = num_format_currency
        cell_end.alignment = align_right
        current_row += 1
        
        ws_summary.cell(row=current_row, column=1, value="Statement Debits").font = font_total
        cell_deb = ws_summary.cell(row=current_row, column=2, value=acc.total_payments)
        cell_deb.font = font_body
        cell_deb.number_format = num_format_currency
        cell_deb.alignment = align_right
        current_row += 1
        
        ws_summary.cell(row=current_row, column=1, value="Statement Credits").font = font_total
        cell_cred = ws_summary.cell(row=current_row, column=2, value=acc.total_deposits)
        cell_cred.font = font_body
        cell_cred.number_format = num_format_currency
        cell_cred.alignment = align_right
        current_row += 1
        
        recon_text = f"{recon_status} — Beginning + Credits - Debits = Ending"
        ws_summary.cell(row=current_row, column=1, value="Reconciliation Check").font = font_total
        ws_summary.cell(row=current_row, column=2, value=recon_text).font = font_total
        if recon_status == "PASS":
            ws_summary.cell(row=current_row, column=2).font = Font(name="Segoe UI", size=11, bold=True, color="2E7D32")
        else:
            ws_summary.cell(row=current_row, column=2).font = Font(name="Segoe UI", size=11, bold=True, color="C62828")
            
        current_row += 2 # gap

    # ==================== BUILD TRANSACTION LEDGER ====================
    
    account_summaries = []
    for acc in bank_accounts:
        title = acc.account_title or "Operating"
        # Simplify title for the ledger summary (e.g., just first word or remove "Account")
        title_simple = title.replace(" Account", "").strip()
        account_summaries.append(f"{acc.account_number} {title_simple}")
        
    accounts_str = ", ".join(account_summaries)
    
    ws_ledger["A1"] = "Standardized Transactions Ledger"
    ws_ledger["A1"].font = font_title
    ws_ledger["A2"] = f"Combined granular transaction logs across all {primary_bank} accounts ({accounts_str}) — {company_name}"
    ws_ledger["A2"].font = font_subtitle

    ledger_headers = [
        "Date", "Bank Name", "Account Number", "Account Title", "Statement Ref", "Description",
        "Amount", "Category", "Interbank Self-Transfer?"
    ]
    
    for col_idx, header in enumerate(ledger_headers, start=1):
        cell = ws_ledger.cell(row=4, column=col_idx, value=header)
        cell.font = font_header
        cell.fill = fill_header_ledger
        cell.alignment = align_center
        cell.border = border_all
    ws_ledger.row_dimensions[4].height = 28

    current_ledger_row = 5
    
    all_txs = []
    for acc in bank_accounts:
        for tx in acc.transactions.all():
            all_txs.append(tx)
    
    all_txs.sort(key=lambda x: x.date, reverse=True)

    for idx, tx in enumerate(all_txs):
        # Format statement ref dynamically, e.g. "9625 - Jan 2026"
        month_abbr = tx.date.strftime('%b')
        stmt_ref = f"{tx.account.account_number} - {month_abbr} {tx.date.year}"
        
        row_data = [
            tx.date.strftime("%Y-%m-%d"),
            tx.account.bank_name,
            tx.account.account_number,
            tx.account.account_title or "Operating Account",
            stmt_ref,
            tx.description,
            tx.amount,
            tx.category,
            "Yes" if tx.is_interbank else "No"
        ]

        for col_idx, val in enumerate(row_data, start=1):
            cell = ws_ledger.cell(row=current_ledger_row, column=col_idx, value=val)
            cell.font = font_body
            cell.border = border_all

            if col_idx in (1, 9):
                cell.alignment = align_center
            elif col_idx in (2, 3, 4, 5, 6, 8):
                cell.alignment = align_left
            elif col_idx == 7:
                cell.alignment = align_right
                cell.number_format = num_format_currency

            if tx.is_interbank:
                cell.fill = fill_interbank
                if col_idx == 9:
                    cell.font = font_total
            else:
                if idx % 2 == 1:
                    cell.fill = fill_zebra
                    
        ws_ledger.row_dimensions[current_ledger_row].height = 20
        current_ledger_row += 1

    # ==================== BUILD INDIVIDUAL ACCOUNT SHEETS ====================
    for acc in bank_accounts:
        short_bank = "".join(c for c in acc.bank_name if c.isalnum() or c.isspace()).strip()
        short_bank = short_bank.split()[0] if short_bank else "Bank"
        sheet_title = f"{short_bank}_{acc.account_number}"
        sheet_title = re.sub(r'[\\/*?:\[\]]', '', sheet_title)[:30]
        
        ws_acc = wb.create_sheet(title=sheet_title)
        ws_acc.views.sheetView[0].showGridLines = True
        
        txs = list(acc.transactions.all().order_by('date', 'id'))
        beginning_balance = acc.beginning_balance
        ending_balance = acc.ending_balance

        calculated_ending = beginning_balance + acc.total_deposits - acc.total_payments
        diff = abs(calculated_ending - ending_balance)
        recon_status = "PASS" if diff <= 0.05 else "FAIL"
        
        period_str = format_statement_dates(acc.start_date, acc.end_date)
        
        card_data = [
            ("Account", acc.account_number),
            ("Account Title", acc.account_title or "Operating Account"),
            ("Statement Period", period_str),
            ("Transactions in Ledger", len(txs)),
            ("Statement Total Credits", acc.total_deposits),
            ("FDD Ledger Total Deposits", acc.total_deposits),
            ("Statement Total Debits", acc.total_payments),
            ("FDD Ledger Total Payments", acc.total_payments),
            ("Statement Beginning Balance", beginning_balance),
            ("Statement Ending Balance", ending_balance),
            ("Calculated Ending Balance", calculated_ending),
            ("Reconciliation", recon_status)
        ]
        
        # Headers for Individual Sheet
        cell_h1 = ws_acc.cell(row=1, column=1, value="Bank Statement")
        cell_h1.font = font_header
        cell_h1.fill = fill_header_summary
        cell_h1.alignment = align_left
        cell_h1.border = border_all
        
        cell_h2 = ws_acc.cell(row=1, column=2, value=acc.bank_name)
        cell_h2.font = font_header
        cell_h2.fill = fill_header_summary
        cell_h2.alignment = align_left
        cell_h2.border = border_all
        
        for idx, (label, val) in enumerate(card_data, start=2):
            cell_lbl = ws_acc.cell(row=idx, column=1, value=label)
            cell_lbl.font = font_total
            cell_lbl.alignment = align_left
            cell_lbl.border = border_all
            
            cell_val = ws_acc.cell(row=idx, column=2, value=val)
            cell_val.font = font_body
            cell_val.border = border_all
            
            if isinstance(val, (int, Decimal, float)):
                if label == "Transactions in Ledger":
                    cell_val.alignment = align_right
                    cell_val.number_format = '#,##0'
                else:
                    cell_val.alignment = align_right
                    cell_val.number_format = num_format_currency
            else:
                cell_val.alignment = align_left
                if label == "Reconciliation":
                    cell_val.font = font_total
                    if val == "PASS":
                        cell_val.font = Font(name="Segoe UI", size=11, bold=True, color="2E7D32")
                    else:
                        cell_val.font = Font(name="Segoe UI", size=11, bold=True, color="C62828")

        # Autofit columns for individual account sheet
        for col in ws_acc.columns:
            max_len = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                if cell.value is not None:
                    val_str = str(cell.value)
                    if isinstance(cell.value, (float, int, Decimal)):
                        val_str = f"{cell.value:,.2f}"
                    max_len = max(max_len, len(val_str))
            ws_acc.column_dimensions[col_letter].width = max(max_len + 3, 15)

    # ---- Autofit Column Widths (Summary and Ledger) ----
    for ws in (ws_summary, ws_ledger):
        for col in ws.columns:
            max_len = 0
            col_letter = get_column_letter(col[0].column)
            
            # Skip Title Block evaluation (first 3 rows) to prevent oversized columns
            for cell in col[3:]:
                if cell.value is not None:
                    val_str = str(cell.value)
                    if isinstance(cell.value, (float, int, Decimal)):
                        val_str = f"{cell.value:,.2f}"
                    max_len = max(max_len, len(val_str))
            
            header_val = col[3].value
            max_len = max(max_len, len(str(header_val or '')) + 3)
            min_w = 28 if col_letter == 'A' else 14
            ws.column_dimensions[col_letter].width = min(max(max_len, min_w), 50)

    wb.save(output_path)
