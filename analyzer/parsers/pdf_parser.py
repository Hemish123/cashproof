import pdfplumber
import re
import json
import base64
import os
import io
import urllib.request
import urllib.error
import datetime
import concurrent.futures
import threading
import random
import time
import logging
from decimal import Decimal
from django.conf import settings
from .base import BaseParser

# Global, process-wide cap on simultaneous OpenAI API calls. Without this,
# MAX_CONCURRENT_FILES x MAX_PDF_WORKERS (e.g. 5 x 15 = 75) all fire at once
# across files, which gets throttled/queued by the API and produces wildly
# uneven, sometimes very long, individual chunk times. Every AI call in this
# module — from any file, any thread — must acquire this semaphore first, so
# the actual number of in-flight API requests never exceeds this limit no
# matter how the file/page concurrency settings are configured.
_GLOBAL_AI_CALL_SEMAPHORE = threading.Semaphore(
    getattr(settings, 'MAX_TOTAL_CONCURRENT_AI_CALLS', 15)
)

class PDFStatementParser(BaseParser):
    @staticmethod
    def _normalize_account_number(value):
        """Digits-only form of an account number/tail, for matching the
        same account across different formatting ('Acct Ending 9625' vs
        '******9625' vs '9625')."""
        digits = re.sub(r'\D', '', str(value or ''))
        return digits or None
    
    def parse(self):
        """
        AI-First Statement Parsing Pipeline.
        Attempts OpenAI extraction (text-mode or vision-mode).
        Falls back to legacy deterministic rule-based parsing if OpenAI is unconfigured or unavailable.
        """
        try:
            return self._parse_via_openai()
        except Exception as ai_err:
            import logging
            import traceback
            
            print("\n" + "="*50)
            print("🛑 OPENAI PARSING FAILED! DEBUG INFO BELOW:")
            print("="*50)
            traceback.print_exc()
            print("="*50 + "\n")
            
            logging.warning(f"AI Parsing failed or skipped ({ai_err}). Falling back to rule-based parser.")
            return self._parse_via_legacy_rules()

    def _parse_via_openai(self):
        """
        AI Extraction Engine powered by OpenAI.
        Extracts metadata and transactions page-by-page to handle statements of any length with zero timeouts.
        """
        from openai import OpenAI

        # Only pull from settings.py, no .env loading
        openai_api_key = getattr(settings, "OPENAI_API_KEY", None)
        openai_model = getattr(settings, "OPENAI_MODEL", "gpt-5.5")

        if not openai_api_key:
            raise ValueError("OPENAI_API_KEY is not defined in Django settings.")

        client = OpenAI(api_key=openai_api_key)

        def parse_iso_date(d_str):
            if not d_str:
                return None
            try:
                return datetime.datetime.strptime(str(d_str).strip(), "%Y-%m-%d").date()
            except Exception:
                return self.parse_date(d_str)

        def make_ai_request(messages_list, max_retries=4):
            # Cap total simultaneous API calls across ALL files/pages being
            # processed right now, regardless of how many worker threads or
            # files are configured to run concurrently. This is what
            # actually prevents throttling-induced stragglers.
            with _GLOBAL_AI_CALL_SEMAPHORE:
                last_err = None
                for attempt in range(max_retries):
                    try:
                        response = client.chat.completions.create(
                            model=openai_model,
                            messages=messages_list,
                            # temperature=0.0,
                            response_format={"type": "json_object"},
                            timeout=90
                        )
                        return json.loads(response.choices[0].message.content)
                    except Exception as e:
                        last_err = e
                        err_text = str(e).lower()
                        is_rate_limit = (
                            '429' in err_text or 'rate limit' in err_text
                            or 'rate_limit' in err_text or 'too many requests' in err_text
                        )
                        is_transient = (
                            is_rate_limit or 'timeout' in err_text or 'timed out' in err_text
                            or '500' in err_text or '502' in err_text or '503' in err_text
                        )
                        if not is_transient or attempt == max_retries - 1:
                            raise
                        # Exponential backoff with a little jitter, longer for
                        # rate limits than for plain timeouts.
                        base_wait = 5 if is_rate_limit else 2
                        wait_s = base_wait * (2 ** attempt) + random.uniform(0, 1)
                        print(f"[DEBUG] Transient API error ({e}); retrying in {wait_s:.1f}s (attempt {attempt + 1}/{max_retries})")
                        time.sleep(wait_s)
                raise last_err

        with pdfplumber.open(self.file_path) as pdf:
            pages = pdf.pages
            if not pages:
                raise ValueError("PDF file contains no pages.")

            # Step 1: Extract Metadata from header pages (pages 1-3)
            doc_header_text = "\n".join([(p.extract_text() or "") for p in pages[:3]])
            self._extract_metadata_from_text(doc_header_text, first_page_text=pages[0].extract_text() or "")

            meta_prompt = (
                "Extract header metadata from these bank statement pages.\n"
                "IMPORTANT — MULTIPLE ACCOUNTS: Some statements cover MORE THAN ONE bank account in a single "
                "document (e.g. an 'Account Summary' box listing several rows like 'Acct Ending 9625' and "
                "'Acct Ending 4940', each with its OWN 'DETAIL TRANSACTIONS BY DATE' section later in the "
                "document). If you see more than one such account, list EVERY one of them in 'accounts' below, "
                "each with ITS OWN account number and ITS OWN beginning/ending balance (found in that "
                "account's own summary box, e.g. 'Previous Balance' / 'Ending Balance' near its transaction "
                "section — NOT the primary account's numbers). Always include the primary/main account in this "
                "list too, in addition to the flat 'account_number' field below.\n"
                "IMPORTANT — FINDING THE TRUE ENDING BALANCE: Some reports (e.g. QuickBooks-style "
                "'Reconciliation Detail' reports) include several subtotal lines near the end, such as "
                "'Total Checks and Payments', 'Total Deposits and Credits', or 'Total Cleared Transactions'. "
                "NONE of these is the ending balance — they are period totals, not account balances. The true "
                "ending balance is ONLY the figure on a line explicitly labeled 'Ending Balance' (or 'Closing "
                "Balance' / 'Register Balance as of <date>'), usually the very last such line in the document. "
                "If that line shows two dollar amounts side by side, the account balance is the SECOND (right-"
                "hand) one — the first is just that period's net change. Use the 'LAST PAGE OF DOCUMENT' text "
                "below if provided; it is included specifically because the ending balance often only appears "
                "there.\n"
                "JSON Schema:\n"
                "{\n"
                "  \"bank_name\": \"Bank Name\",\n"
                "  \"account_number\": \"Account Number\",\n"
                "  \"account_holder\": \"Account Holder Name or null\",\n"
                "  \"start_date\": \"YYYY-MM-DD or null\",\n"
                "  \"end_date\": \"YYYY-MM-DD or null\",\n"
                "  \"beginning_balance\": 1234.56,\n"
                "  \"ending_balance\": 5678.90,\n"
                "  \"accounts\": [\n"
                "    {\"account_number\": \"9625\", \"beginning_balance\": 1234.56, \"ending_balance\": 5678.90}\n"
                "  ]\n"
                "}\n\n"
                f"HEADER TEXT:\n{doc_header_text[:4000]}"
            )
            
            try:
                meta_res = make_ai_request([
                    {"role": "system", "content": "You are an expert bank statement metadata extraction agent."},
                    {"role": "user", "content": meta_prompt}
                ])
            except Exception as e:
                meta_res = {}

            def _is_usable(value, placeholder, unknown_value):
                if not value:
                    return False
                v = str(value).strip().lower()
                return v not in ('', placeholder.lower(), unknown_value.lower(), 'null', 'none', 'n/a')

            ai_bank_name = meta_res.get("bank_name")
            if _is_usable(ai_bank_name, "Bank Name", "Unknown Bank"):
                self.bank_name = ai_bank_name
            elif self.bank_name == "Unknown Bank":
                print(f"[DEBUG] Metadata extraction returned no usable bank_name ({ai_bank_name!r}); keeping regex-detected value ({self.bank_name!r})")

            ai_account_number = meta_res.get("account_number")
            if _is_usable(ai_account_number, "Account Number", "Unknown Account"):
                self.account_number = str(ai_account_number).replace('-', '')
            elif self.account_number == "Unknown Account":
                print(f"[DEBUG] Metadata extraction returned no usable account_number ({ai_account_number!r}); keeping regex-detected value ({self.account_number!r})")
            if meta_res.get("account_holder"):
                self.account_holder = meta_res.get("account_holder")
            
            beg_b = meta_res.get("beginning_balance")
            if beg_b is not None and self.beginning_balance is None:
                self.beginning_balance = Decimal(str(beg_b))
            elif self.beginning_balance is None:
                self.beginning_balance = Decimal("0.00")
            
            end_b = meta_res.get("ending_balance")
            if end_b is not None and self.ending_balance is None:
                self.ending_balance = Decimal(str(end_b))
            elif self.ending_balance is None:
                self.ending_balance = Decimal("0.00")

            if not self.start_date:
                self.start_date = parse_iso_date(meta_res.get("start_date"))
            if not self.end_date:
                self.end_date = parse_iso_date(meta_res.get("end_date"))

            self.detected_accounts = meta_res.get("accounts") or []
            if not any(
                self._normalize_account_number(a.get('account_number')) == self._normalize_account_number(self.account_number)
                for a in self.detected_accounts
            ):
                self.detected_accounts.append({
                    'account_number': self.account_number,
                    'beginning_balance': self.beginning_balance,
                    'ending_balance': self.ending_balance,
                })

            # Pre-extract text and images sequentially to avoid pdfplumber thread-safety crashes (segfaults)
            page_data_list = []
            for p_idx, page in enumerate(pages):
                p_text = page.extract_text() or ""
                
                # Check for garbled/scanned content
                cid_count = len(re.findall(r'\(cid:\d+\)', p_text))
                non_ascii = len([c for c in p_text if ord(c) < 32 or ord(c) > 126])
                is_garbled = len(p_text) > 0 and ((cid_count * 10 + non_ascii) / max(len(p_text), 1) > 0.3)
                
                use_vision = (len(p_text.strip()) < 50) or is_garbled
                b64_str = None
                
                if use_vision:
                    try:
                        pil_img = page.to_image(resolution=150).original
                        buf = io.BytesIO()
                        pil_img.save(buf, format="JPEG")
                        b64_str = base64.b64encode(buf.getvalue()).decode("utf-8")
                    except Exception:
                        pass # Ignore image extraction errors, fallback to text if possible
                
                page_data_list.append({
                    'p_idx': p_idx,
                    'p_text': p_text,
                    'use_vision': use_vision,
                    'b64_str': b64_str
                })

            # Step 1b: Group pages into chunks to cut down the number of
            # separate AI calls. Calling the API once per page (30 calls for
            # a 30-page statement) pays a fixed per-call overhead (network
            # round-trip + model latency) 30 times over, and that overhead
            # dominates total time far more than the actual page content
            # does. Batching several pages' TEXT into one call cuts total
            # calls roughly by CHUNK_SIZE, which is the single biggest lever
            # for reducing wall-clock time. Vision pages (scanned/garbled,
            # need the actual image) are kept as their own single-page
            # chunks — images are large and shouldn't be combined.
            chunk_size = getattr(settings, 'PDF_PAGE_CHUNK_SIZE', 5)
            chunks = []
            current_text_chunk = []
            for data in page_data_list:
                if data['use_vision']:
                    if current_text_chunk:
                        chunks.append(current_text_chunk)
                        current_text_chunk = []
                    chunks.append([data])
                else:
                    current_text_chunk.append(data)
                    if len(current_text_chunk) >= chunk_size:
                        chunks.append(current_text_chunk)
                        current_text_chunk = []
            if current_text_chunk:
                chunks.append(current_text_chunk)

            def _process_chunk(chunk):
                page_nums = [d['p_idx'] + 1 for d in chunk]
                start_time = time.time()
                print(f"[DEBUG] Starting AI extraction for page(s) {page_nums}")

                account_list_hint = ", ".join(
                    str(a.get('account_number')) for a in getattr(self, 'detected_accounts', []) if a.get('account_number')
                ) or str(self.account_number)

                tx_instruction = (
                    f"Extract ALL transaction line items from page(s) {page_nums} of this bank statement.\n"
                    "If multiple pages are included below, each is clearly marked with a '=== PAGE N ===' "
                    "header — extract transactions from EVERY page shown, not just the first one.\n"
                    "MULTIPLE ACCOUNTS: This statement may cover more than one bank account "
                    f"(known account numbers in this document: {account_list_hint}). Each account's transactions "
                    "appear under their own 'Acct Ending XXXX (Continued)' / 'Account Title' / 'DETAIL "
                    "TRANSACTIONS BY DATE' heading. For EVERY transaction, set 'account_number' to the account "
                    "number shown in the nearest preceding such heading on the SAME page. If a page has no "
                    f"account heading of its own, use the account from the most recent page that did: \"{self.account_number}\" "
                    "as the default if nothing else applies. Never merge two different accounts' transactions "
                    "under one account_number — a page switching from one account's detail section to another's "
                    "(as can happen partway down a page) means transactions above and below that switch belong "
                    "to DIFFERENT accounts.\n"
                    "CRITICAL SIGN RULE: 'amount' MUST be POSITIVE for deposits/credits/inflows, and NEGATIVE for debits/withdrawals/payments/fees.\n"
                    "SPECIAL RULE FOR CHECKS: If a page contains a 'CHECKS' or 'CHECK NUMBER' table, EVERY amount in that "
                    "table is a NEGATIVE outflow (money leaving the account) even though no minus sign is printed next to it. "
                    "Checks-paid tables NEVER show a minus sign in bank statements — you must apply the negative sign yourself.\n"
                    "SPECIAL RULE FOR CHECK IMAGES: Some statements include a page of scanned/photographed check images "
                    "(front-of-check facsimiles) near the end, each with its check number, date, and amount printed as a "
                    "caption. These are NOT new transactions — they are pictures of checks already listed once in the "
                    "'CHECKS' table elsewhere in the statement. If a page is a check-images page, contribute NO "
                    "transactions from it. Only extract from the actual CHECKS table (plain rows of check number/date/"
                    "amount with no check imagery), never from a page showing the check facsimiles themselves.\n"
                    "JSON Schema:\n"
                    "{\n"
                    "  \"transactions\": [\n"
                    "    {\n"
                    "      \"date\": \"YYYY-MM-DD\",\n"
                    "      \"description\": \"Full description text\",\n"
                    "      \"amount\": -150.00,\n"
                    "      \"balance\": 4500.00,\n"
                    "      \"category\": \"Category name or null\",\n"
                    "      \"account_number\": \"9625\"\n"
                    "    }\n"
                    "  ]\n"
                    "}"
                )

                if len(chunk) == 1 and chunk[0]['use_vision'] and chunk[0]['b64_str']:
                    user_msg = [
                        {"type": "text", "text": tx_instruction},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{chunk[0]['b64_str']}"}}
                    ]
                else:
                    combined_text = "\n\n".join(
                        f"=== PAGE {d['p_idx'] + 1} ===\n{d['p_text']}" for d in chunk
                    )
                    user_msg = f"{tx_instruction}\n\n{combined_text}"

                try:
                    tx_res = make_ai_request([
                        {"role": "system", "content": "You are a precise financial transaction audit extraction agent."},
                        {"role": "user", "content": user_msg}
                    ])
                except Exception as page_err:
                    print(f"🛑 [DEBUG] ERROR on page(s) {page_nums}: {page_err}")
                    raise
                    # return []

                # print(f"[DEBUG] Raw AI Response for page(s) {page_nums}: {json.dumps(tx_res)}")

                chunk_transactions = []
                for tx in tx_res.get("transactions", []):
                    d_val = parse_iso_date(tx.get("date"))
                    if not d_val:
                        continue

                    desc_text = str(tx.get("description") or "Transaction").strip()
                    amt_val = self.clean_amount(tx.get("amount"))
                    bal_val = self.clean_amount(tx.get("balance")) if tx.get("balance") is not None else None
                    cat_val = self.derive_category(desc_text, tx.get("category"), amt_val)

                    chunk_transactions.append({
                        'date': d_val,
                        'description': desc_text,
                        'amount': amt_val,
                        'balance': bal_val,
                        'category': cat_val,
                        'account_number': tx.get('account_number') or self.account_number
                    })

                elapsed = time.time() - start_time
                print(f"[DEBUG] Page(s) {page_nums} extraction completed in {elapsed:.2f}s (Found {len(chunk_transactions)} txs)")
                return chunk_transactions

            # Step 2: Extract Transactions chunk-by-chunk concurrently
            transactions = []
            total_start_time = time.time()
            max_workers = getattr(settings, 'MAX_PDF_WORKERS', 8)
            print(f"[DEBUG] Submitting {len(chunks)} chunk(s) covering {len(pages)} pages to ThreadPoolExecutor (max_workers={max_workers})...")

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(_process_chunk, chunk) for chunk in chunks]
                for future in concurrent.futures.as_completed(futures):
                    transactions.extend(future.result())
                    
            total_elapsed = time.time() - total_start_time
            print(f"[DEBUG] All pages extracted in {total_elapsed:.2f}s. Total transactions combined: {len(transactions)}")

            # Safety-net dedup: some statements repeat every check as a
            # scanned check-image page near the end (check number/date/amount
            # printed as a caption under each image). If the AI extraction
            # prompt fails to skip that page, the same check gets extracted
            # twice — once from the real CHECKS table, once from the image
            # captions — silently inflating Total Payments. Collapse any
            # transactions that share the same date AND amount AND look like
            # a check (description is just a bare check number, e.g. "52233"
            # or "Check 52233"), keeping only the first occurrence.
            seen_checks = set()
            deduped = []
            check_desc_re = re.compile(r'^\s*(?:check\s*#?\s*)?\d{4,6}\s*\*?\s*$', re.IGNORECASE)
            for tx in transactions:
                is_check_like = tx['amount'] < 0 and check_desc_re.match(tx['description'] or '')
                if is_check_like:
                    key = (self._normalize_account_number(tx.get('account_number')), tx['date'], tx['amount'])
                    if key in seen_checks:
                        print(f"[DEBUG] Dropping duplicate check-image transaction: {tx['date']} {tx['amount']} '{tx['description']}'")
                        continue
                    seen_checks.add(key)
                deduped.append(tx)
            transactions = deduped

        # Empty transactions is only a real failure if metadata extraction
        # also failed — meaning the AI likely couldn't read the document at
        # all. A dormant account (just BEGINNING/ENDING BALANCE lines) is a
        # valid, legitimate zero-transaction result and must still surface
        # on the dashboard with its bank name / account number / balances,
        # not get dropped or sent through the legacy parser.
        metadata_looks_valid = (
            self.account_number != "Unknown Account"
            and self.bank_name != "Unknown Bank"
        )
        if not transactions and not metadata_looks_valid:
            raise ValueError(
                "AI extraction produced 0 transactions and no usable "
                "metadata (bank_name/account_number) — treating as a "
                "genuine failure."
            )
        if not transactions:
            print(f"[DEBUG] 0 transactions found, but metadata is valid "
                f"(bank={self.bank_name}, acct={self.account_number}) — "
                f"dormant-account statement, not a failure.")

        # Sort transactions by date
        transactions.sort(key=lambda x: x['date'])

        if transactions:
            if not self.start_date:
                self.start_date = transactions[0]['date']
            if not self.end_date:
                self.end_date = transactions[-1]['date']
        # else: keep self.start_date/self.end_date as already set from meta_prompt

        # --- Split transactions by account -------------------------------
        primary_norm = self._normalize_account_number(self.account_number)
        groups = {}
        for tx in transactions:
            acct_raw = tx.pop('account_number', None) or self.account_number
            acct_norm = self._normalize_account_number(acct_raw) or primary_norm
            grp = groups.setdefault(acct_norm, {'account_number': acct_raw, 'txs': []})
            grp['txs'].append(tx)

        # No transactions at all -> groups would be empty -> no account
        # would ever reach the dashboard. Force the primary account through
        # with its extracted metadata and an empty transaction list.
        if not groups:
            groups[primary_norm] = {'account_number': self.account_number, 'txs': []}

        results = []
        for acct_norm, grp in groups.items():
            group_txs = sorted(grp['txs'], key=lambda x: x['date'])
            # NOTE: removed the old `if not group_txs: continue` — that
            # line (if present) is exactly what would silently drop a
            # dormant account from the dashboard. Do not skip empty groups.

            is_primary = (acct_norm == primary_norm)
            detected = next(
                (a for a in getattr(self, 'detected_accounts', [])
                 if self._normalize_account_number(a.get('account_number')) == acct_norm),
                None
            )

            if is_primary:
                beginning_balance = self.beginning_balance
                ending_balance = self.ending_balance
                start_date = self.start_date
                end_date = self.end_date
            else:
                beginning_balance = None
                ending_balance = None
                if detected and detected.get('beginning_balance') is not None:
                    try:
                        beginning_balance = Decimal(str(detected['beginning_balance']))
                    except Exception:
                        beginning_balance = None
                if detected and detected.get('ending_balance') is not None:
                    try:
                        ending_balance = Decimal(str(detected['ending_balance']))
                    except Exception:
                        ending_balance = None
                start_date = group_txs[0]['date'] if group_txs else self.start_date
                end_date = group_txs[-1]['date'] if group_txs else self.end_date

            if (beginning_balance is None or beginning_balance == Decimal("0.00")) and group_txs and group_txs[0].get('balance') is not None:
                beginning_balance = group_txs[0]['balance'] - group_txs[0]['amount']
            if (ending_balance is None or ending_balance == Decimal("0.00")) and group_txs and group_txs[-1].get('balance') is not None:
                ending_balance = group_txs[-1]['balance']
            beginning_balance = beginning_balance if beginning_balance is not None else Decimal("0.00")
            ending_balance = ending_balance if ending_balance is not None else Decimal("0.00")

            account_meta = {
                'bank_name': self.bank_name,
                'account_number': grp['account_number'],
                'account_holder': self.account_holder,
                'currency': self.currency,
                'start_date': start_date,
                'end_date': end_date,
                'beginning_balance': beginning_balance,
                'ending_balance': ending_balance,
            }
            results.append((account_meta, group_txs))

        return results

    def _parse_via_legacy_rules(self):
        """
        Legacy deterministic parser fallback.
        """
        all_rows = []
        full_text_first_page = ""
        self.statement_year = None

        with pdfplumber.open(self.file_path) as pdf:
            full_text = ""
            for p in pdf.pages:
                full_text += (p.extract_text() or "") + "\n"

            if len(pdf.pages) > 0:
                first_page = pdf.pages[0]
                full_text_first_page = first_page.extract_text() or ""

            self._extract_metadata_from_text(full_text, first_page_text=full_text_first_page)

            for page in pdf.pages:
                page_text = page.extract_text() or ""
                year_match = re.search(r'\b(20\d{2})\b', page_text)
                if year_match:
                    self.statement_year = int(year_match.group(1))
                    break

            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        cleaned_row = [str(cell).strip() if cell is not None else "" for cell in row]
                        if any(cleaned_row):
                            all_rows.append(cleaned_row)

        transactions = []
        if all_rows:
            header_idx, mappings = self._find_headers(all_rows)
            if header_idx is not None:
                raw_data_rows = all_rows[header_idx + 1:]
                date_col = mappings['date_col_idx']
                desc_col = mappings['desc_col_idx']
                amount_col = mappings['amount_col_idx']
                debit_col = mappings['debit_col_idx']
                credit_col = mappings['credit_col_idx']
                bal_col = mappings['bal_col_idx']

                for row in raw_data_rows:
                    max_len = max(x for x in (date_col, desc_col, amount_col, debit_col, credit_col, bal_col) if x is not None) + 1
                    if len(row) < max_len:
                        row = row + [""] * (max_len - len(row))

                    row_date_str = row[date_col]
                    parsed_date = self.parse_date(row_date_str, year=self.statement_year)
                    row_desc = row[desc_col]

                    if not parsed_date or not row_desc:
                        continue

                    amount = Decimal('0.00')
                    if amount_col is not None:
                        amount = self.clean_amount(row[amount_col])
                    else:
                        debit_val = self.clean_amount(row[debit_col]) if debit_col is not None else Decimal('0.00')
                        credit_val = self.clean_amount(row[credit_col]) if credit_col is not None else Decimal('0.00')
                        amount = credit_val - abs(debit_val)

                    if amount == 0:
                        continue

                    balance = self.clean_amount(row[bal_col]) if bal_col is not None and row[bal_col] else None

                    transactions.append({
                        'date': parsed_date,
                        'description': row_desc,
                        'amount': amount,
                        'balance': balance,
                        'category': self.derive_category(row_desc, None, amount)
                    })

        if transactions:
            sorted_txs = sorted(transactions, key=lambda x: x['date'])
            self.start_date = sorted_txs[0]['date']
            self.end_date = sorted_txs[-1]['date']

            account_meta = {
            'bank_name': self.bank_name,
            'account_number': self.account_number,
            'account_holder': self.account_holder,
            'currency': self.currency,
            'start_date': self.start_date,
            'end_date': self.end_date,
            'beginning_balance': self.beginning_balance or Decimal('0.00'),
            'ending_balance': self.ending_balance or Decimal('0.00'),
        }

        # Legacy rule-based parser only ever handles a single account, but
        # the caller (manager.py) now expects a list of (account_meta,
        # transactions) pairs to support multi-account PDFs from the AI
        # path — wrap this single result to match that interface.
        return [(account_meta, transactions)]

    def _extract_metadata_from_text(self, text, first_page_text=None):
        lookup_text = first_page_text if first_page_text else text
        bank_keywords = {
            'chase': 'Chase Bank', 'wells fargo': 'Wells Fargo',
            'bank of america': 'Bank of America', 'citi': 'Citibank',
            'hsbc': 'HSBC', 'barclays': 'Barclays',
            'qnb': 'QNB Bank', 'pnc': 'PNC Bank',
            'us bank': 'US Bank', 'capital one': 'Capital One',
            'td bank': 'TD Bank', 'truist': 'Truist'
        }
        for key, name in bank_keywords.items():
            if key in lookup_text.lower():
                self.bank_name = name
                break
                
        # Fallback account number extraction if AI misses it
        acc_match = re.search(r'\b(?:account|acc|a/c|no\.?)\b.*?\b(\d[0-9-]{3,17})\b', lookup_text, re.IGNORECASE)
        if acc_match and self.account_number == "Unknown Account":
            self.account_number = acc_match.group(1).strip()

    def _find_headers(self, all_rows):
        date_syn = {'date', 'transaction date', 'tx date', 'posted date'}
        desc_syn = {'description', 'details', 'particulars', 'narrative'}
        amount_syn = {'amount', 'transaction amount'}
        debit_syn = {'debit', 'withdrawals', 'payments'}
        credit_syn = {'credit', 'deposits', 'receipts'}
        bal_syn = {'balance', 'running balance'}

        for r_idx in range(min(50, len(all_rows))):
            row_cells = [cell.strip().lower() for cell in all_rows[r_idx]]
            mappings = {
                'date_col_idx': next((i for i, c in enumerate(row_cells) if any(s in c for s in date_syn)), None),
                'desc_col_idx': next((i for i, c in enumerate(row_cells) if any(s in c for s in desc_syn)), None),
                'amount_col_idx': next((i for i, c in enumerate(row_cells) if any(s in c for s in amount_syn)), None),
                'debit_col_idx': next((i for i, c in enumerate(row_cells) if any(s in c for s in debit_syn)), None),
                'credit_col_idx': next((i for i, c in enumerate(row_cells) if any(s in c for s in credit_syn)), None),
                'bal_col_idx': next((i for i, c in enumerate(row_cells) if any(s in c for s in bal_syn)), None),
            }
            if mappings['date_col_idx'] is not None and mappings['desc_col_idx'] is not None:
                return r_idx, mappings
        return None, None



# import pdfplumber
# import re
# import json
# import base64
# import os
# import io
# import urllib.request
# import urllib.error
# import datetime
# import concurrent.futures
# import time
# import logging
# from decimal import Decimal
# from django.conf import settings
# from .base import BaseParser

# class PDFStatementParser(BaseParser):
#     def parse(self):
#         """
#         AI-First Statement Parsing Pipeline.
#         Attempts OpenAI / Azure OpenAI extraction (text-mode or vision-mode).
#         Falls back to legacy deterministic rule-based parsing if OpenAI is unconfigured or unavailable.
#         """
#         try:
#             return self._parse_via_openai()
#         except Exception as ai_err:
#             import logging
#             import traceback
            
#             print("\n" + "="*50)
#             print("🛑 OPENAI PARSING FAILED! DEBUG INFO BELOW:")
#             print("="*50)
#             traceback.print_exc()
#             print("="*50 + "\n")
            
#             logging.warning(f"AI Parsing failed or skipped ({ai_err}). Falling back to rule-based parser.")
#             return self._parse_via_legacy_rules()

#     def _parse_via_openai(self):
#         """
#         AI Extraction Engine powered by OpenAI / Azure OpenAI.
#         Extracts metadata and transactions page-by-page to handle statements of any length with zero timeouts.
#         """
#         # Load environment variables if not loaded
#         env_path = os.path.join(settings.BASE_DIR, '.env') if hasattr(settings, 'BASE_DIR') else '.env'
#         if os.path.exists(env_path):
#             with open(env_path, 'r') as f:
#                 for line in f:
#                     line = line.strip()
#                     if line and not line.startswith('#') and '=' in line:
#                         key, val = line.split('=', 1)
#                         os.environ[key.strip()] = val.strip()

#         openai_api_key = os.environ.get("OPENAI_API_KEY") or getattr(settings, "OPENAI_API_KEY", None)
#         openai_model = os.environ.get("OPENAI_MODEL") or getattr(settings, "OPENAI_MODEL", "gpt-4o-mini")

#         azure_api_key = os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("AZURE_OPENAI_KEY") or getattr(settings, "AZURE_OPENAI_API_KEY", None)
#         azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT") or getattr(settings, "AZURE_OPENAI_ENDPOINT", None)
#         azure_deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT") or os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME") or getattr(settings, "AZURE_OPENAI_DEPLOYMENT", "gpt-5.5")

#         from openai import OpenAI, AzureOpenAI

#         if openai_api_key:
#             client = OpenAI(api_key=openai_api_key)
#             model_name = openai_model
#         elif azure_api_key and azure_endpoint:
#             client = AzureOpenAI(
#                 api_key=azure_api_key,
#                 api_version="2024-02-15-preview",
#                 azure_endpoint=azure_endpoint
#             )
#             model_name = azure_deployment
#         else:
#             raise ValueError("OpenAI or Azure OpenAI credentials are missing.")

#         def parse_iso_date(d_str):
#             if not d_str:
#                 return None
#             try:
#                 return datetime.datetime.strptime(str(d_str).strip(), "%Y-%m-%d").date()
#             except Exception:
#                 return self.parse_date(d_str)

#         def make_ai_request(messages_list):
#             response = client.chat.completions.create(
#                 model=model_name,
#                 messages=messages_list,
#                 temperature=0.0,
#                 response_format={"type": "json_object"},
#                 timeout=60
#             )
#             return json.loads(response.choices[0].message.content)

#         with pdfplumber.open(self.file_path) as pdf:
#             pages = pdf.pages
#             if not pages:
#                 raise ValueError("PDF file contains no pages.")

#             # Step 1: Extract Metadata from header pages (pages 1-3)
#             doc_header_text = "\n".join([(p.extract_text() or "") for p in pages[:3]])
#             self._extract_metadata_from_text(doc_header_text, first_page_text=pages[0].extract_text() or "")

#             meta_prompt = (
#                 "Extract header metadata from these bank statement pages.\n"
#                 "JSON Schema:\n"
#                 "{\n"
#                 "  \"bank_name\": \"Bank Name\",\n"
#                 "  \"account_number\": \"Account Number\",\n"
#                 "  \"account_holder\": \"Account Holder Name or null\",\n"
#                 "  \"start_date\": \"YYYY-MM-DD or null\",\n"
#                 "  \"end_date\": \"YYYY-MM-DD or null\",\n"
#                 "  \"beginning_balance\": 1234.56,\n"
#                 "  \"ending_balance\": 5678.90\n"
#                 "}\n\n"
#                 f"HEADER TEXT:\n{doc_header_text[:4000]}"
#             )
            
#             try:
#                 meta_res = make_ai_request([
#                     {"role": "system", "content": "You are an expert bank statement metadata extraction agent."},
#                     {"role": "user", "content": meta_prompt}
#                 ])
#             except Exception as e:
#                 meta_res = {}

#             if meta_res.get("bank_name") and meta_res.get("bank_name") != "Bank Name":
#                 self.bank_name = meta_res.get("bank_name")
#             if meta_res.get("account_number") and meta_res.get("account_number") != "Account Number":
#                 self.account_number = str(meta_res.get("account_number")).replace('-', '')
#             if meta_res.get("account_holder"):
#                 self.account_holder = meta_res.get("account_holder")
            
#             beg_b = meta_res.get("beginning_balance")
#             if beg_b is not None and self.beginning_balance is None:
#                 self.beginning_balance = Decimal(str(beg_b))
#             elif self.beginning_balance is None:
#                 self.beginning_balance = Decimal("0.00")
            
#             end_b = meta_res.get("ending_balance")
#             if end_b is not None and self.ending_balance is None:
#                 self.ending_balance = Decimal(str(end_b))
#             elif self.ending_balance is None:
#                 self.ending_balance = Decimal("0.00")

#             if not self.start_date:
#                 self.start_date = parse_iso_date(meta_res.get("start_date"))
#             if not self.end_date:
#                 self.end_date = parse_iso_date(meta_res.get("end_date"))

#             # Pre-extract text and images sequentially to avoid pdfplumber thread-safety crashes (segfaults)
#             page_data_list = []
#             for p_idx, page in enumerate(pages):
#                 p_text = page.extract_text() or ""
                
#                 # Check for garbled/scanned content
#                 cid_count = len(re.findall(r'\(cid:\d+\)', p_text))
#                 non_ascii = len([c for c in p_text if ord(c) < 32 or ord(c) > 126])
#                 is_garbled = len(p_text) > 0 and ((cid_count * 10 + non_ascii) / max(len(p_text), 1) > 0.3)
                
#                 use_vision = (len(p_text.strip()) < 50) or is_garbled
#                 b64_str = None
                
#                 if use_vision:
#                     try:
#                         pil_img = page.to_image(resolution=150).original
#                         buf = io.BytesIO()
#                         pil_img.save(buf, format="JPEG")
#                         b64_str = base64.b64encode(buf.getvalue()).decode("utf-8")
#                     except Exception:
#                         pass # Ignore image extraction errors, fallback to text if possible
                
#                 page_data_list.append({
#                     'p_idx': p_idx,
#                     'p_text': p_text,
#                     'use_vision': use_vision,
#                     'b64_str': b64_str
#                 })

#             def _process_page(data):
#                 p_idx = data['p_idx']
#                 p_text = data['p_text']
#                 use_vision = data['use_vision']
#                 b64_str = data['b64_str']
                
#                 start_time = time.time()
#                 print(f"[DEBUG] Starting AI extraction for page {p_idx + 1}")

#                 tx_instruction = (
#                     f"Extract ALL transaction line items from Page {p_idx + 1} of this bank statement.\n"
#                     "CRITICAL SIGN RULE: 'amount' MUST be POSITIVE for deposits/credits/inflows, and NEGATIVE for debits/withdrawals/payments/fees.\n"
#                     "JSON Schema:\n"
#                     "{\n"
#                     "  \"transactions\": [\n"
#                     "    {\n"
#                     "      \"date\": \"YYYY-MM-DD\",\n"
#                     "      \"description\": \"Full description text\",\n"
#                     "      \"amount\": -150.00,\n"
#                     "      \"balance\": 4500.00,\n"
#                     "      \"category\": \"Category name or null\"\n"
#                     "    }\n"
#                     "  ]\n"
#                     "}"
#                 )

#                 if use_vision and b64_str:
#                     user_msg = [
#                         {"type": "text", "text": tx_instruction},
#                         {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_str}"}}
#                     ]
#                 else:
#                     user_msg = f"{tx_instruction}\n\nPAGE {p_idx + 1} TEXT:\n{p_text}"

#                 try:
#                     tx_res = make_ai_request([
#                         {"role": "system", "content": "You are a precise financial transaction audit extraction agent."},
#                         {"role": "user", "content": user_msg}
#                     ])
#                 except Exception as page_err:
#                     print(f"🛑 [DEBUG] ERROR on page {p_idx + 1}: {page_err}")
#                     return []

#                 page_transactions = []
#                 for tx in tx_res.get("transactions", []):
#                     d_val = parse_iso_date(tx.get("date"))
#                     if not d_val:
#                         continue

#                     desc_text = str(tx.get("description") or "Transaction").strip()
#                     amt_val = self.clean_amount(tx.get("amount"))
#                     bal_val = self.clean_amount(tx.get("balance")) if tx.get("balance") is not None else None
#                     cat_val = self.derive_category(desc_text, tx.get("category"), amt_val)

#                     page_transactions.append({
#                         'date': d_val,
#                         'description': desc_text,
#                         'amount': amt_val,
#                         'balance': bal_val,
#                         'category': cat_val
#                     })
                
#                 elapsed = time.time() - start_time
#                 print(f"[DEBUG] Page {p_idx + 1} extraction completed in {elapsed:.2f}s (Found {len(page_transactions)} txs)")
#                 return page_transactions

#             # Step 2: Extract Transactions Page-by-Page concurrently
#             transactions = []
#             total_start_time = time.time()
#             max_workers = getattr(settings, 'MAX_PDF_WORKERS', 20)
#             print(f"[DEBUG] Submitting {len(pages)} pages to ThreadPoolExecutor (max_workers={max_workers})...")
            
#             with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
#                 futures = [executor.submit(_process_page, data) for data in page_data_list]
#                 for future in concurrent.futures.as_completed(futures):
#                     transactions.extend(future.result())
                    
#             total_elapsed = time.time() - total_start_time
#             print(f"[DEBUG] All pages extracted in {total_elapsed:.2f}s. Total transactions combined: {len(transactions)}")

#         if not transactions:
#             raise ValueError("AI Page-by-Page extraction produced 0 transactions.")

#         # Sort transactions by date
#         transactions.sort(key=lambda x: x['date'])

#         if not self.start_date:
#             self.start_date = transactions[0]['date']
#         if not self.end_date:
#             self.end_date = transactions[-1]['date']

#         # Fallback balance derivation if ending/beginning balances were 0
#         if self.beginning_balance == Decimal("0.00") and transactions[0].get('balance') is not None:
#             self.beginning_balance = transactions[0]['balance'] - transactions[0]['amount']
#         if self.ending_balance == Decimal("0.00") and transactions[-1].get('balance') is not None:
#             self.ending_balance = transactions[-1]['balance']

#         account_meta = {
#             'bank_name': self.bank_name,
#             'account_number': self.account_number,
#             'account_holder': self.account_holder,
#             'currency': self.currency,
#             'start_date': self.start_date,
#             'end_date': self.end_date,
#             'beginning_balance': self.beginning_balance,
#             'ending_balance': self.ending_balance,
#         }

#         return account_meta, transactions

#     def _parse_via_legacy_rules(self):
#         """
#         Legacy deterministic parser fallback.
#         """
#         all_rows = []
#         full_text_first_page = ""
#         self.statement_year = None

#         with pdfplumber.open(self.file_path) as pdf:
#             full_text = ""
#             for p in pdf.pages:
#                 full_text += (p.extract_text() or "") + "\n"

#             if len(pdf.pages) > 0:
#                 first_page = pdf.pages[0]
#                 full_text_first_page = first_page.extract_text() or ""

#             self._extract_metadata_from_text(full_text, first_page_text=full_text_first_page)

#             for page in pdf.pages:
#                 page_text = page.extract_text() or ""
#                 year_match = re.search(r'\b(20\d{2})\b', page_text)
#                 if year_match:
#                     self.statement_year = int(year_match.group(1))
#                     break

#             for page in pdf.pages:
#                 tables = page.extract_tables()
#                 for table in tables:
#                     for row in table:
#                         cleaned_row = [str(cell).strip() if cell is not None else "" for cell in row]
#                         if any(cleaned_row):
#                             all_rows.append(cleaned_row)

#         transactions = []
#         if all_rows:
#             header_idx, mappings = self._find_headers(all_rows)
#             if header_idx is not None:
#                 raw_data_rows = all_rows[header_idx + 1:]
#                 date_col = mappings['date_col_idx']
#                 desc_col = mappings['desc_col_idx']
#                 amount_col = mappings['amount_col_idx']
#                 debit_col = mappings['debit_col_idx']
#                 credit_col = mappings['credit_col_idx']
#                 bal_col = mappings['bal_col_idx']

#                 for row in raw_data_rows:
#                     max_len = max(x for x in (date_col, desc_col, amount_col, debit_col, credit_col, bal_col) if x is not None) + 1
#                     if len(row) < max_len:
#                         row = row + [""] * (max_len - len(row))

#                     row_date_str = row[date_col]
#                     parsed_date = self.parse_date(row_date_str, year=self.statement_year)
#                     row_desc = row[desc_col]

#                     if not parsed_date or not row_desc:
#                         continue

#                     amount = Decimal('0.00')
#                     if amount_col is not None:
#                         amount = self.clean_amount(row[amount_col])
#                     else:
#                         debit_val = self.clean_amount(row[debit_col]) if debit_col is not None else Decimal('0.00')
#                         credit_val = self.clean_amount(row[credit_col]) if credit_col is not None else Decimal('0.00')
#                         amount = credit_val - abs(debit_val)

#                     if amount == 0:
#                         continue

#                     balance = self.clean_amount(row[bal_col]) if bal_col is not None and row[bal_col] else None

#                     transactions.append({
#                         'date': parsed_date,
#                         'description': row_desc,
#                         'amount': amount,
#                         'balance': balance,
#                         'category': self.derive_category(row_desc, None, amount)
#                     })

#         if transactions:
#             sorted_txs = sorted(transactions, key=lambda x: x['date'])
#             self.start_date = sorted_txs[0]['date']
#             self.end_date = sorted_txs[-1]['date']

#         account_meta = {
#             'bank_name': self.bank_name,
#             'account_number': self.account_number,
#             'account_holder': self.account_holder,
#             'currency': self.currency,
#             'start_date': self.start_date,
#             'end_date': self.end_date,
#             'beginning_balance': self.beginning_balance or Decimal('0.00'),
#             'ending_balance': self.ending_balance or Decimal('0.00'),
#         }

#         return account_meta, transactions

#     def _extract_metadata_from_text(self, text, first_page_text=None):
#         lookup_text = first_page_text if first_page_text else text
#         bank_keywords = {
#             'chase': 'Chase Bank', 'wells fargo': 'Wells Fargo',
#             'bank of america': 'Bank of America', 'citi': 'Citibank',
#             'hsbc': 'HSBC', 'barclays': 'Barclays',
#             'qnb': 'QNB Bank', 'pnc': 'PNC Bank',
#             'us bank': 'US Bank', 'capital one': 'Capital One',
#             'td bank': 'TD Bank', 'truist': 'Truist'
#         }
#         for key, name in bank_keywords.items():
#             if key in lookup_text.lower():
#                 self.bank_name = name
#                 break

#         # Generic fallback: if the bank isn't one of the ~12 known brands
#         # above, try to detect an institution name from the statement
#         # header itself instead of leaving it as "Unknown Bank".
#         if self.bank_name == "Unknown Bank":
#             guessed = self.guess_bank_name_generic(lookup_text)
#             if guessed:
#                 self.bank_name = guessed

#         # Fallback account number extraction if AI misses it
#         acc_match = re.search(r'\b(?:account|acc|a/c|no\.?)\b.*?\b(\d[0-9-]{3,17})\b', lookup_text, re.IGNORECASE)
#         if acc_match and self.account_number == "Unknown Account":
#             self.account_number = acc_match.group(1).strip()

#     def _find_headers(self, all_rows):
#         date_syn = {'date', 'transaction date', 'tx date', 'posted date'}
#         desc_syn = {'description', 'details', 'particulars', 'narrative'}
#         amount_syn = {'amount', 'transaction amount'}
#         debit_syn = {'debit', 'withdrawals', 'payments'}
#         credit_syn = {'credit', 'deposits', 'receipts'}
#         bal_syn = {'balance', 'running balance'}

#         for r_idx in range(min(50, len(all_rows))):
#             row_cells = [cell.strip().lower() for cell in all_rows[r_idx]]
#             mappings = {
#                 'date_col_idx': next((i for i, c in enumerate(row_cells) if any(s in c for s in date_syn)), None),
#                 'desc_col_idx': next((i for i, c in enumerate(row_cells) if any(s in c for s in desc_syn)), None),
#                 'amount_col_idx': next((i for i, c in enumerate(row_cells) if any(s in c for s in amount_syn)), None),
#                 'debit_col_idx': next((i for i, c in enumerate(row_cells) if any(s in c for s in debit_syn)), None),
#                 'credit_col_idx': next((i for i, c in enumerate(row_cells) if any(s in c for s in credit_syn)), None),
#                 'bal_col_idx': next((i for i, c in enumerate(row_cells) if any(s in c for s in bal_syn)), None),
#             }
#             if mappings['date_col_idx'] is not None and mappings['desc_col_idx'] is not None:
#                 return r_idx, mappings
#         return None, None