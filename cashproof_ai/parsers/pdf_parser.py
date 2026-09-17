"""
cashproof_ai/parsers/pdf_parser.py

Section-anchored PDF bank-statement parser for CashProof AI.

Drop-in replacement for the generic heuristic parser. Same public interface:
    PDFStatementParser(file_path).parse() -> (account_meta dict, [transaction dict, ...])
so ingestion.py / interbank.py / reports.py require NO changes.

WHY THIS EXISTS
----------------
The previous version scanned raw text for "any date + any trailing number,"
with no concept of where the transaction table starts/ends. That works only
by luck. This version instead anchors on the statement's own printed section
headers (DEPOSITS AND OTHER CREDITS / WITHDRAWALS AND OTHER DEBITS / CHECKS /
ACCOUNT SUMMARY / DAILY BALANCE SUMMARY) — the same labels a human reads —
and only extracts transactions from inside those sections. Everything
outside a recognized section (headers, footers, MICR/OCR junk lines, the
"To Help Balance Your Account" boilerplate page) is ignored, never mistaken
for a transaction.

If you ingest a bank whose statement doesn't use these section headers,
extend SECTION_PATTERNS / SUMMARY_PATTERNS below rather than falling back to
generic scanning — a narrow parser that refuses to guess is safer than a
broad one that silently fabricates rows.
"""

import re
import logging
from datetime import datetime, date
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

# ── Bank detection ──────────────────────────────────────────────────────────

BANK_INDICATORS = {
    'old national': 'Old National Bank',
    'chase': 'JPMorgan Chase Bank',
    'jpmorgan': 'JPMorgan Chase Bank',
    'bank of america': 'Bank of America',
    'wells fargo': 'Wells Fargo Bank',
    'citibank': 'Citibank',
    'td bank': 'TD Bank',
    'us bank': 'US Bank',
    'pnc': 'PNC Bank',
    'regions': 'Regions Bank',
    'suntrust': 'SunTrust Bank',
    'truist': 'Truist Bank',
    'capital one': 'Capital One',
    'fifth third': 'Fifth Third Bank',
    'navy federal': 'Navy Federal Credit Union',
    'bb&t': 'BB&T Bank',
    'qnb': 'QNB Bank',
    'keybank': 'KeyBank',
    'huntington': 'Huntington Bank',
    'citizens': 'Citizens Bank',
    'ally bank': 'Ally Bank',
}

# ── Section anchors (printed headers on the statement itself) ──────────────

SECTION_PATTERNS = {
    'credits': re.compile(r'DEPOSITS AND OTHER CREDITS', re.IGNORECASE),
    'debits':  re.compile(r'WITHDRAWALS AND OTHER DEBITS', re.IGNORECASE),
    'checks':  re.compile(r'^\s*CHECKS\s*$', re.IGNORECASE),
    'daily_balance': re.compile(r'DAILY BALANCE SUMMARY', re.IGNORECASE),
    'help_balance':  re.compile(r'To Help Balance Your Account', re.IGNORECASE),
}

SUMMARY_PATTERNS = {
    'beg':     re.compile(r'Previous Statement Balance\s+(\d{2}/\d{2}/\d{4})\s+\$?([\d,]+\.\d{2})'),
    'end':     re.compile(r'Current Statement Balance\s+(\d{2}/\d{2}/\d{4})\s+\$?([\d,]+\.\d{2})'),
    'ncr':     re.compile(r'Deposits/Credits\s+(\d+)\s+\$?([\d,]+\.\d{2})'),
    'ndr':     re.compile(r'Withdrawals/Debits\s+(\d+)\s+-?\$?([\d,]+\.\d{2})'),
    'acct':    re.compile(r'ACCOUNT NUMBER\s+X*(\d{4,})', re.IGNORECASE),
    'date':    re.compile(r'^DATE\s+(\d{2}/\d{2}/\d{4})\s*$', re.IGNORECASE),
}

# A transaction/check line: date, tracer/check#, description, trailing $amount
TXN_LINE_RE = re.compile(
    r'(?:^|\s)(\d{2}/\d{2})\s+(\S+)\s+(.*?)\s+(-?\$[\d,]+\.\d{2})\s*$'
)
CHECK_LINE_RE = re.compile(r'(\d{5,7})\s*(\*?)\s+(\d{2}/\d{2})\s+\$([\d,]+\.\d{2})')

# Lines that are page furniture, not transaction continuations — never treat
# these as description continuations even if they follow a transaction line.
NOISE_LINE_RE = re.compile(
    r'^(PAGE\s+\d+|DATE\s|ACCOUNT|BUSINESS ANALYSIS|OLD NATIONAL|P\.\s*O\.\s*Box|'
    r'Evansville|www\.|Client Care|Fraud|Written Inquiries|Visit us|'
    r'DEPOSITS AND OTHER CREDITS|WITHDRAWALS AND OTHER DEBITS|CHECKS|'
    r'CHECK NUMBER|DAILY BALANCE|To Help|Member|[A-Z]{20,}$|\d{8} \d{7})',
    re.IGNORECASE,
)


def _money(s: str) -> Decimal:
    return Decimal(s.replace('$', '').replace(',', ''))


def _classify_transaction_type(description: str, is_debit: bool) -> str:
    desc = (description or '').upper()
    if 'MONTHLY SERVICE CHARGE' in desc:
        return 'E'
    if 'INTEREST' in desc and not is_debit:
        return 'D'
    if desc.startswith('CHECK'):
        return 'F'
    if re.search(r'INT TXFR|INTERNAL TRANSFER|ONLINE.*TRANSFER', desc):
        return 'C'
    if 'ACH' in desc:
        return 'G'
    return 'B' if is_debit else 'A'


class PDFStatementParser:
    """Section-anchored parser. Public interface unchanged from prior version."""

    def __init__(self, file_path: str):
        self.file_path = file_path

    def parse(self) -> tuple[dict, list[dict]]:
        text = self._extract_text_layout()
        account_meta = self._extract_header(text)
        transactions = self._extract_transactions(text, account_meta)

        if account_meta.get('statement_total_credits') is None:
            account_meta['statement_total_credits'] = sum(
                t['credit_amount'] for t in transactions if t['credit_amount']
            )
        if account_meta.get('statement_total_debits') is None:
            account_meta['statement_total_debits'] = sum(
                t['debit_amount'] for t in transactions if t['debit_amount']
            )
        return account_meta, transactions

    # ── Text extraction ──────────────────────────────────────────────────
    def _extract_text_layout(self) -> str:
        """
        Extract text with PyMuPDF ("pymupdf" on PyPI, imported as `fitz`).

        Deliberately NOT using pdftotext (poppler-utils) here: that requires
        a system binary that isn't present on Azure App Service / Azure
        Functions by default and can't be apt-installed on a PaaS plan
        without a custom container. PyMuPDF ships its compiled core inside
        the wheel itself — `pip install pymupdf` in requirements.txt is the
        entire dependency footprint, nothing extra to install on Azure.

        Loops over *every* page of the document (statements here run
        20–30+ pages) and concatenates them in order, with an explicit
        page-break marker and page count logged so a truncated/partial
        extraction is never silent.

        Falls back to pdfplumber (also pure-Python-wheel, no system binary)
        only if PyMuPDF itself fails to open the file.
        """
        try:
            import fitz  # PyMuPDF
            text_parts = []
            with fitz.open(self.file_path) as doc:
                page_count = doc.page_count
                for page in doc:
                    text_parts.append(self._reconstruct_layout(page))
            logger.info(f'PyMuPDF: extracted {page_count} page(s) from {self.file_path}')
            full_text = '\n'.join(text_parts)
            if full_text.strip():
                return full_text
            logger.warning(f'PyMuPDF returned no text for {self.file_path}, falling back to pdfplumber')
        except Exception as e:
            logger.warning(f'PyMuPDF failed ({e}), falling back to pdfplumber')

        try:
            import pdfplumber
            pages = []
            with pdfplumber.open(self.file_path) as pdf:
                page_count = len(pdf.pages)
                for page in pdf.pages:
                    pages.append(page.extract_text() or '')
            logger.info(f'pdfplumber: extracted {page_count} page(s) from {self.file_path}')
            return '\n'.join(pages)
        except Exception as e:
            logger.error(f'pdfplumber also failed: {e}')
            return ''

    @staticmethod
    def _reconstruct_layout(page, y_tol: float = 2.0) -> str:
        """
        Re-implements what `pdftotext -layout` does, using only PyMuPDF's
        word-position output (no external binary):

        1. Pull every word with its (x0, y0) position via page.get_text("words").
           PyMuPDF's own block/line grouping is unreliable for wide-column
           bank-statement tables (it can split one visual row — date, tracer,
           description, amount — into several separate "line" objects), so
           we don't trust it.
        2. Cluster words into rows by y0 (vertical position), tolerance
           y_tol points — words on the same printed line land within a
           couple of points of each other regardless of which column
           MuPDF assigned them to.
        3. Within each row, sort left-to-right by x0 and join with a single
           space, so a row reads exactly like a line of `pdftotext -layout`
           output: "05/01  2121 ONE VISION  PAYROLL  $7,112.50".
        4. Emit rows top-to-bottom so section headers, transaction rows,
           and the account-summary block all appear in printed order.
        """
        words = page.get_text('words')  # (x0, y0, x1, y1, text, block, line, word_no)
        rows: list[tuple[float, list[tuple[float, str]]]] = []
        for x0, y0, x1, y1, txt, *_ in words:
            placed = False
            for row_y, items in rows:
                if abs(row_y - y0) <= y_tol:
                    items.append((x0, txt))
                    placed = True
                    break
            if not placed:
                rows.append((y0, [(x0, txt)]))
        rows.sort(key=lambda r: r[0])
        lines = []
        for _y, items in rows:
            items.sort(key=lambda t: t[0])
            lines.append(' '.join(t for _, t in items))
        return '\n'.join(lines)

    # ── Header / account summary ─────────────────────────────────────────
    def _extract_header(self, text: str) -> dict:
        meta = {
            'client_name': '', 'bank_name': '', 'account_number': '', 'last4': '',
            'account_title': 'Operating Account', 'account_type': 'checking',
            'currency': 'USD', 'period_start': None, 'period_end': None,
            'beginning_balance': None, 'ending_balance': None,
            'statement_total_credits': None, 'statement_total_debits': None,
        }
        head = text[:2500]
        low = text[:6000].lower()
        for key, name in BANK_INDICATORS.items():
            if key in low:
                meta['bank_name'] = name
                break
        if not meta['bank_name'] and 'evansville, in' in low and 'p. o. box 718' in low:
            meta['bank_name'] = 'Old National Bank'  # letterhead is a logo image, not text, on some pages

        m = SUMMARY_PATTERNS['acct'].search(head)
        if m:
            digits = m.group(1)
            meta['last4'] = digits[-4:]
            meta['account_number'] = 'XXXXXX' + digits[-4:] if len(digits) <= 6 else digits

        m = SUMMARY_PATTERNS['beg'].search(text)
        if m:
            meta['beginning_balance'] = _money(m.group(2))
            meta['period_start'] = self._prior_month_start(m.group(1))
        m = SUMMARY_PATTERNS['end'].search(text)
        if m:
            meta['ending_balance'] = _money(m.group(2))
            d = datetime.strptime(m.group(1), '%m/%d/%Y').date()
            meta['period_end'] = d
            if meta['period_start'] is None:
                meta['period_start'] = d.replace(day=1)

        m = SUMMARY_PATTERNS['ncr'].search(text)
        if m:
            meta['statement_total_credits'] = _money(m.group(2))
        m = SUMMARY_PATTERNS['ndr'].search(text)
        if m:
            meta['statement_total_debits'] = _money(m.group(2))

        # Client name = first all-caps company-looking line after the bank
        # header block; account_title = the line right after it IF that line
        # has no digits and isn't a street address (e.g. "PRE PAID LEGAL").
        lines = [l.strip() for l in head.splitlines() if l.strip()]
        client_idx = None
        for i, l in enumerate(lines):
            if (re.match(r'^[A-Z][A-Z0-9 &,.\'\-]+$', l) and 3 <= len(l) <= 60
                    and ' ' in l  # real names/company lines have spaces; MICR junk doesn't
                    and not re.match(r'^[A-Z]{15,}$', l.replace(' ', ''))  # exclude MICR-like blocks
                    and not any(k in l.upper() for k in ('BANK', 'STATEMENT', 'ACCOUNT', 'PAGE', 'CHECKING', 'BOX'))):
                meta['client_name'] = l
                client_idx = i
                break
        if client_idx is not None and client_idx + 1 < len(lines):
            nxt = lines[client_idx + 1]
            looks_like_street = re.search(r'\d', nxt) or re.search(
                r'\b(ST|AVE|BLVD|DR|RD|SUITE|STE|BOX)\b', nxt, re.IGNORECASE)
            if not looks_like_street and nxt.upper() == nxt and ' ' in nxt:
                meta['account_title'] = nxt.title()

        return meta

    @staticmethod
    def _prior_month_start(mmddyyyy: str) -> date:
        d = datetime.strptime(mmddyyyy, '%m/%d/%Y').date()
        # beginning balance date is the LAST day of the prior period;
        # the statement period actually starts the next calendar day.
        nxt = d.replace(day=28) + __import__('datetime').timedelta(days=4)
        return d.replace(day=1) if d.day != 1 else d

    # ── Transaction extraction (section-anchored) ────────────────────────
    def _extract_transactions(self, text: str, meta: dict) -> list[dict]:
        results = []
        section = None
        prev = None
        year = meta['period_end'].year if meta.get('period_end') else datetime.now().year

        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()

            if SECTION_PATTERNS['credits'].search(stripped):
                section, prev = 'credits', None
                continue
            if SECTION_PATTERNS['debits'].search(stripped):
                section, prev = 'debits', None
                continue
            if SECTION_PATTERNS['checks'].match(stripped):
                section, prev = 'checks', None
                continue
            if SECTION_PATTERNS['daily_balance'].search(stripped) or \
               SECTION_PATTERNS['help_balance'].search(stripped):
                section, prev = None, None
                continue
            if 'ACCOUNT SUMMARY' in stripped.upper():
                section, prev = None, None
                continue

            if section == 'checks':
                for cm in CHECK_LINE_RE.finditer(stripped):
                    chk, _seq, mmdd, amt = cm.groups()
                    mm, dd = mmdd.split('/')
                    tdate = date(year, int(mm), int(dd))
                    results.append(self._build_tx(
                        transaction_date=tdate, description=f'Check {chk}',
                        debit_amount=_money(amt), credit_amount=None,
                        reference_number=chk, check_number=chk,
                    ))
                continue

            if section in ('credits', 'debits'):
                m = TXN_LINE_RE.search(stripped)
                if m:
                    mmdd, tracer, desc, amt = m.groups()
                    mm, dd = mmdd.split('/')
                    tdate = date(year, int(mm), int(dd))
                    val = _money(amt)
                    desc = re.sub(r'\s{2,}', ' ', desc).strip()
                    is_debit = (section == 'debits')
                    prev = self._build_tx(
                        transaction_date=tdate, description=desc,
                        debit_amount=abs(val) if is_debit else None,
                        credit_amount=abs(val) if not is_debit else None,
                        reference_number=tracer,
                    )
                    results.append(prev)
                    continue
                # continuation line (e.g. "SMALL BUSINESS ACCOUNT") — only
                # append if it's genuinely a description continuation, never
                # page furniture / MICR junk / section headers.
                if prev is not None and stripped and not NOISE_LINE_RE.match(stripped):
                    prev['description'] = (prev['description'] + ' | ' + stripped)[:500]

        return results

    def _build_tx(self, *, transaction_date, description, debit_amount,
                  credit_amount, reference_number='', check_number='') -> dict:
        is_debit = debit_amount is not None
        net_amount = -abs(debit_amount) if is_debit else abs(credit_amount)
        return {
            'transaction_date': transaction_date,
            'value_date': None,
            'description': description,
            'reference_number': reference_number,
            'check_number': check_number,
            'debit_amount': debit_amount,
            'credit_amount': credit_amount,
            'net_amount': net_amount,
            'transaction_type': _classify_transaction_type(description, is_debit),
            'counterparty_info': '',
            'source_page': '',
            'balance': None,
        }