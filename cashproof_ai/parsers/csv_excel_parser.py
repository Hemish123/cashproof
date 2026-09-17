"""
cashproof_ai/parsers/csv_excel_parser.py

Fresh CSV / Excel bank-statement parser for CashProof AI.
Supports .csv, .xlsx, .xls files in common bank export formats.

Returns: (account_meta dict, [transaction dict, ...])
Same schema as pdf_parser.py.
"""

import re
import logging
from datetime import datetime, date
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

# ── Amount / date helpers ─────────────────────────────────────────────────────

def _parse_amount(val) -> Decimal | None:
    if val is None:
        return None
    text = str(val).strip()
    if not text or text in ('-', '–', '—', 'n/a', 'N/A'):
        return None
    negative = text.startswith('(') and text.endswith(')')
    text = text.strip('()').replace(',', '').replace('$', '').replace('£', '').replace('€', '').strip()
    if not text:
        return None
    try:
        d = Decimal(text)
        return -d if negative else d
    except InvalidOperation:
        return None


def _parse_date(val) -> date | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    text = str(val).strip()
    formats = [
        '%m/%d/%Y', '%m/%d/%y', '%m-%d-%Y', '%m-%d-%y',
        '%Y-%m-%d', '%Y/%m/%d',
        '%d/%m/%Y', '%d-%m-%Y',
        '%B %d, %Y', '%B %d %Y', '%b %d, %Y', '%b %d %Y',
        '%d %B %Y', '%d %b %Y',
        '%Y%m%d',
    ]
    text = text.rstrip(',')
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _classify_transaction_type(description: str, is_debit: bool) -> str:
    desc = (description or '').lower()
    if re.search(r'\b(?:fee|charge|service charge|maintenance|overdraft)\b', desc):
        return 'E'
    if re.search(r'\b(?:interest|int\.?\s*pd)\b', desc):
        return 'D'
    if re.search(r'\b(?:check|cheque|chk)\b', desc):
        return 'F'
    if re.search(r'\b(?:transfer|internal transfer|online transfer|wire transfer)\b', desc):
        return 'C'
    if re.search(r'\bach\b', desc):
        return 'G'
    if re.search(r'\b(?:card|merchant|settlement)\b', desc):
        return 'H'
    return 'B' if is_debit else 'A'


# ── Column-name normaliser ────────────────────────────────────────────────────

_COL_ALIASES = {
    # Date
    'date': 'date', 'transaction date': 'date', 'trans date': 'date',
    'posting date': 'date', 'post date': 'date', 'value date': 'value_date',
    # Description
    'description': 'description', 'memo': 'description', 'payee': 'description',
    'details': 'description', 'particulars': 'description', 'narrative': 'description',
    'transaction description': 'description',
    # Debit
    'debit': 'debit', 'debit amount': 'debit', 'withdrawals': 'debit',
    'withdrawal': 'debit', 'payment': 'debit', 'payments': 'debit',
    'dr': 'debit', 'amount (dr)': 'debit',
    # Credit
    'credit': 'credit', 'credit amount': 'credit', 'deposits': 'credit',
    'deposit': 'credit', 'cr': 'credit', 'amount (cr)': 'credit',
    # Amount (signed single column)
    'amount': 'amount', 'transaction amount': 'amount', 'net amount': 'amount',
    # Balance
    'balance': 'balance', 'running balance': 'balance', 'ledger balance': 'balance',
    'available balance': 'balance',
    # Reference / Check
    'reference': 'reference', 'reference number': 'reference', 'ref': 'reference',
    'ref number': 'reference', 'trace': 'reference', 'trace number': 'reference',
    'check number': 'check', 'check #': 'check', 'chk': 'check', 'check no': 'check',
    # Type
    'type': 'type', 'transaction type': 'type', 'category': 'type',
}

def _normalise_col(name: str) -> str:
    return _COL_ALIASES.get(str(name).strip().lower(), str(name).strip().lower())


# ── Main parser class ─────────────────────────────────────────────────────────

class CSVExcelParser:
    """
    Fresh CSV / Excel parser for CashProof AI.
    """

    def __init__(self, file_path: str):
        self.file_path = file_path

    def parse(self) -> tuple[dict, list[dict]]:
        import os
        ext = os.path.splitext(self.file_path)[1].lower()
        if ext == '.csv':
            raw_rows, raw_header = self._read_csv()
        elif ext in ('.xlsx', '.xls'):
            raw_rows, raw_header = self._read_excel()
        else:
            raise ValueError(f'Unsupported extension: {ext}')

        col_map = {i: _normalise_col(h) for i, h in enumerate(raw_header)}
        transactions = self._parse_rows(raw_rows, col_map)
        account_meta = self._infer_meta(transactions)
        return account_meta, transactions

    # ── Readers ───────────────────────────────────────────────────────────────

    def _read_csv(self) -> tuple[list[list], list]:
        import csv
        # Try to detect encoding
        for encoding in ('utf-8-sig', 'utf-8', 'latin-1', 'cp1252'):
            try:
                with open(self.file_path, newline='', encoding=encoding) as f:
                    raw = f.read()
                break
            except UnicodeDecodeError:
                continue
        else:
            with open(self.file_path, newline='', encoding='latin-1') as f:
                raw = f.read()

        # Find the header row (the one containing 'date' and at least one amount word)
        lines = raw.splitlines()
        header_idx = 0
        for idx, line in enumerate(lines[:30]):
            lower = line.lower()
            if 'date' in lower and any(w in lower for w in ['amount', 'debit', 'credit', 'description', 'memo']):
                header_idx = idx
                break

        import io
        reader = csv.reader(io.StringIO('\n'.join(lines[header_idx:])))
        rows = list(reader)
        if not rows:
            return [], []
        header = rows[0]
        data = [r for r in rows[1:] if any(c.strip() for c in r)]
        return data, header

    def _read_excel(self) -> tuple[list[list], list]:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(self.file_path, data_only=True)
            ws = wb.active
            all_rows = list(ws.iter_rows(values_only=True))
        except Exception:
            try:
                import xlrd
                wb = xlrd.open_workbook(self.file_path)
                ws = wb.sheet_by_index(0)
                all_rows = [ws.row_values(i) for i in range(ws.nrows)]
            except Exception as e:
                raise ValueError(f'Cannot read Excel file: {e}')

        # Find header row
        header_idx = 0
        for idx, row in enumerate(all_rows[:30]):
            row_str = [str(c or '').lower() for c in row]
            if 'date' in row_str and any(
                w in row_str for w in ['amount', 'debit', 'credit', 'description', 'memo']
            ):
                header_idx = idx
                break

        header = [str(c or '') for c in all_rows[header_idx]]
        data = []
        for row in all_rows[header_idx + 1:]:
            if any(c is not None and str(c).strip() for c in row):
                data.append(list(row))
        return data, header

    # ── Row parsing ───────────────────────────────────────────────────────────

    def _parse_rows(self, rows: list[list], col_map: dict[int, str]) -> list[dict]:
        transactions = []
        seen_cols = set(col_map.values())

        has_debit_credit = 'debit' in seen_cols or 'credit' in seen_cols
        has_amount       = 'amount' in seen_cols

        def get(row, name):
            for idx, col_name in col_map.items():
                if col_name == name and idx < len(row):
                    return row[idx]
            return None

        for row in rows:
            if not any(str(c or '').strip() for c in row):
                continue

            # Date
            tx_date = _parse_date(get(row, 'date'))
            if tx_date is None:
                continue  # skip non-transaction rows

            # Description
            desc = str(get(row, 'description') or '').strip()
            if not desc:
                # Combine all non-numeric text cells as fallback description
                desc = ' '.join(
                    str(c) for c in row
                    if c and not re.match(r'^[\d\.,\-\(\)]+$', str(c).strip())
                    and not _parse_date(c)
                )

            # Amounts
            debit_amount  = None
            credit_amount = None
            net_amount    = Decimal('0')

            if has_debit_credit:
                d_raw = get(row, 'debit')
                c_raw = get(row, 'credit')
                d_val = _parse_amount(d_raw)
                c_val = _parse_amount(c_raw)
                if d_val and abs(d_val) > 0:
                    debit_amount = abs(d_val)
                    net_amount   = -debit_amount
                elif c_val and abs(c_val) > 0:
                    credit_amount = abs(c_val)
                    net_amount    = credit_amount

            elif has_amount:
                a_raw = get(row, 'amount')
                a_val = _parse_amount(a_raw)
                if a_val is not None:
                    if a_val < 0:
                        debit_amount = abs(a_val)
                        net_amount   = a_val
                    elif a_val > 0:
                        credit_amount = a_val
                        net_amount    = a_val

            if debit_amount is None and credit_amount is None:
                continue  # no amounts — skip

            is_debit = debit_amount is not None
            tx_type  = _classify_transaction_type(desc, is_debit)

            # Balance
            balance = _parse_amount(get(row, 'balance'))

            # Reference / check
            ref = str(get(row, 'reference') or '').strip()
            chk = str(get(row, 'check') or '').strip()

            # Value date
            v_date = _parse_date(get(row, 'value_date'))

            transactions.append({
                'transaction_date': tx_date,
                'value_date': v_date,
                'description': desc[:500],
                'reference_number': ref,
                'check_number': chk,
                'debit_amount': debit_amount,
                'credit_amount': credit_amount,
                'net_amount': net_amount,
                'transaction_type': tx_type,
                'counterparty_info': '',
                'source_page': '1',
                'balance': balance,
            })

        return transactions

    # ── Meta inference ────────────────────────────────────────────────────────

    def _infer_meta(self, transactions: list[dict]) -> dict:
        """Infer account meta from transaction set (CSV/Excel rarely embeds header metadata)."""
        dates = [t['transaction_date'] for t in transactions if t['transaction_date']]
        total_credits = sum(t['credit_amount'] for t in transactions if t['credit_amount'])
        total_debits  = sum(t['debit_amount']  for t in transactions if t['debit_amount'])

        return {
            'client_name': '',
            'bank_name': '',
            'account_number': '',
            'last4': '',
            'account_title': 'Operating Account',
            'account_type': 'checking',
            'currency': 'USD',
            'period_start': min(dates) if dates else None,
            'period_end': max(dates) if dates else None,
            'beginning_balance': None,
            'ending_balance': None,
            'statement_total_credits': total_credits,
            'statement_total_debits': total_debits,
        }
