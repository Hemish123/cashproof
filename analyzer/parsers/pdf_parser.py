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
# import threading
# import random
# import time
# import logging
# from decimal import Decimal
# from django.conf import settings
# from .base import BaseParser
# from openai import AzureOpenAI


# # Global, process-wide cap on simultaneous OpenAI API calls. Without this,
# # MAX_CONCURRENT_FILES x MAX_PDF_WORKERS (e.g. 5 x 15 = 75) all fire at once
# # across files, which gets throttled/queued by the API and produces wildly
# # uneven, sometimes very long, individual chunk times. Every AI call in this
# # module — from any file, any thread — must acquire this semaphore first, so
# # the actual number of in-flight API requests never exceeds this limit no
# # matter how the file/page concurrency settings are configured.
# _GLOBAL_AI_CALL_SEMAPHORE = threading.Semaphore(
#     getattr(settings, 'MAX_TOTAL_CONCURRENT_AI_CALLS', 15)
# )

# class PDFStatementParser(BaseParser):
#     @staticmethod
#     def _normalize_account_number(value):
#         """Digits-only form of an account number/tail, for matching the
#         same account across different formatting ('Acct Ending 9625' vs
#         '******9625' vs '9625')."""
#         digits = re.sub(r'\D', '', str(value or ''))
#         return digits or None
    
#     def parse(self):
#         """
#         AI-First Statement Parsing Pipeline.
#         Attempts OpenAI extraction (text-mode or vision-mode).
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
#         AI Extraction Engine powered by OpenAI.
#         Extracts metadata and transactions page-by-page to handle statements of any length with zero timeouts.
#         """
#         from openai import OpenAI

#         # Only pull from settings.py, no .env loading
#         # openai_api_key = getattr(settings, "OPENAI_API_KEY", None)
#         # openai_model = getattr(settings, "OPENAI_MODEL", "gpt-5.5")

#         azure_openai_api_key = getattr(settings, "AZURE_OPENAI_API_KEY", None)
#         openai_model = getattr(settings, "OPENAI_MODEL", "gpt-5.5")

#         if not azure_openai_api_key:
#             raise ValueError("OPENAI_API_KEY is not defined in Django settings.")

#         # client = OpenAI(api_key=openai_api_key)

#         client = AzureOpenAI(
#             api_key=azure_openai_api_key,
#             azure_endpoint=getattr(settings,"AZURE_OPENAI_ENDPOINT"),
#             api_version="2024-02-15-preview"
#         )

#         def parse_iso_date(d_str):
#             if not d_str:
#                 return None
#             try:
#                 return datetime.datetime.strptime(str(d_str).strip(), "%Y-%m-%d").date()
#             except Exception:
#                 return self.parse_date(d_str)

#         def make_ai_request(messages_list, max_retries=4):
#             # Cap total simultaneous API calls across ALL files/pages being
#             # processed right now, regardless of how many worker threads or
#             # files are configured to run concurrently. This is what
#             # actually prevents throttling-induced stragglers.
#             with _GLOBAL_AI_CALL_SEMAPHORE:
#                 last_err = None
#                 for attempt in range(max_retries):
#                     try:
#                         response = client.chat.completions.create(
#                             model="gpt-5.5",
#                             messages=messages_list,
#                             # temperature=0.0,
#                             response_format={"type": "json_object"},
#                             timeout=90
#                         )
#                         return json.loads(response.choices[0].message.content)
#                     except Exception as e:
#                         last_err = e
#                         err_text = str(e).lower()
#                         is_rate_limit = (
#                             '429' in err_text or 'rate limit' in err_text
#                             or 'rate_limit' in err_text or 'too many requests' in err_text
#                         )
#                         is_transient = (
#                             is_rate_limit or 'timeout' in err_text or 'timed out' in err_text
#                             or '500' in err_text or '502' in err_text or '503' in err_text
#                         )
#                         if not is_transient or attempt == max_retries - 1:
#                             raise
#                         # Exponential backoff with a little jitter, longer for
#                         # rate limits than for plain timeouts.
#                         base_wait = 5 if is_rate_limit else 2
#                         wait_s = base_wait * (2 ** attempt) + random.uniform(0, 1)
#                         print(f"[DEBUG] Transient API error ({e}); retrying in {wait_s:.1f}s (attempt {attempt + 1}/{max_retries})")
#                         time.sleep(wait_s)
#                 raise last_err

#         with pdfplumber.open(self.file_path) as pdf:
#             pages = pdf.pages
#             if not pages:
#                 raise ValueError("PDF file contains no pages.")

#             # Step 1: Extract Metadata from header pages (pages 1-3)
#             doc_header_text = "\n".join([(p.extract_text() or "") for p in pages[:3]])
#             self._extract_metadata_from_text(doc_header_text, first_page_text=pages[0].extract_text() or "")

#             meta_prompt = (
#                 "Extract header metadata from these bank statement pages.\n"
#                 "IMPORTANT — MULTIPLE ACCOUNTS: Some statements cover MORE THAN ONE bank account in a single "
#                 "document (e.g. an 'Account Summary' box listing several rows like 'Acct Ending 9625' and "
#                 "'Acct Ending 4940', each with its OWN 'DETAIL TRANSACTIONS BY DATE' section later in the "
#                 "document). If you see more than one such account, list EVERY one of them in 'accounts' below, "
#                 "each with ITS OWN account number and ITS OWN beginning/ending balance (found in that "
#                 "account's own summary box, e.g. 'Previous Balance' / 'Ending Balance' near its transaction "
#                 "section — NOT the primary account's numbers). Always include the primary/main account in this "
#                 "list too, in addition to the flat 'account_number' field below.\n"
#                 "IMPORTANT — FINDING THE TRUE ENDING BALANCE: Some reports (e.g. QuickBooks-style "
#                 "'Reconciliation Detail' reports) include several subtotal lines near the end, such as "
#                 "'Total Checks and Payments', 'Total Deposits and Credits', or 'Total Cleared Transactions'. "
#                 "NONE of these is the ending balance — they are period totals, not account balances. The true "
#                 "ending balance is ONLY the figure on a line explicitly labeled 'Ending Balance' (or 'Closing "
#                 "Balance' / 'Register Balance as of <date>'), usually the very last such line in the document. "
#                 "If that line shows two dollar amounts side by side, the account balance is the SECOND (right-"
#                 "hand) one — the first is just that period's net change. Use the 'LAST PAGE OF DOCUMENT' text "
#                 "below if provided; it is included specifically because the ending balance often only appears "
#                 "there.\n"
#                 "JSON Schema:\n"
#                 "{\n"
#                 "  \"bank_name\": \"Bank Name\",\n"
#                 "  \"account_number\": \"Account Number\",\n"
#                 "  \"account_holder\": \"Account Holder Name or null\",\n"
#                 "  \"start_date\": \"YYYY-MM-DD or null\",\n"
#                 "  \"end_date\": \"YYYY-MM-DD or null\",\n"
#                 "  \"beginning_balance\": 1234.56,\n"
#                 "  \"ending_balance\": 5678.90,\n"
#                 "  \"accounts\": [\n"
#                 "    {\"account_number\": \"9625\", \"beginning_balance\": 1234.56, \"ending_balance\": 5678.90}\n"
#                 "  ]\n"
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

#             def _is_usable(value, placeholder, unknown_value):
#                 if not value:
#                     return False
#                 v = str(value).strip().lower()
#                 return v not in ('', placeholder.lower(), unknown_value.lower(), 'null', 'none', 'n/a')

#             ai_bank_name = meta_res.get("bank_name")
#             if _is_usable(ai_bank_name, "Bank Name", "Unknown Bank"):
#                 self.bank_name = ai_bank_name
#             elif self.bank_name == "Unknown Bank":
#                 print(f"[DEBUG] Metadata extraction returned no usable bank_name ({ai_bank_name!r}); keeping regex-detected value ({self.bank_name!r})")

#             ai_account_number = meta_res.get("account_number")
#             if _is_usable(ai_account_number, "Account Number", "Unknown Account"):
#                 self.account_number = str(ai_account_number).replace('-', '')
#             elif self.account_number == "Unknown Account":
#                 print(f"[DEBUG] Metadata extraction returned no usable account_number ({ai_account_number!r}); keeping regex-detected value ({self.account_number!r})")
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

#             self.detected_accounts = meta_res.get("accounts") or []
#             if not any(
#                 self._normalize_account_number(a.get('account_number')) == self._normalize_account_number(self.account_number)
#                 for a in self.detected_accounts
#             ):
#                 self.detected_accounts.append({
#                     'account_number': self.account_number,
#                     'beginning_balance': self.beginning_balance,
#                     'ending_balance': self.ending_balance,
#                 })

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

#             # Step 1b: Group pages into chunks to cut down the number of
#             # separate AI calls. Calling the API once per page (30 calls for
#             # a 30-page statement) pays a fixed per-call overhead (network
#             # round-trip + model latency) 30 times over, and that overhead
#             # dominates total time far more than the actual page content
#             # does. Batching several pages' TEXT into one call cuts total
#             # calls roughly by CHUNK_SIZE, which is the single biggest lever
#             # for reducing wall-clock time. Vision pages (scanned/garbled,
#             # need the actual image) are kept as their own single-page
#             # chunks — images are large and shouldn't be combined.
#             chunk_size = getattr(settings, 'PDF_PAGE_CHUNK_SIZE', 5)
#             chunks = []
#             current_text_chunk = []
#             for data in page_data_list:
#                 if data['use_vision']:
#                     if current_text_chunk:
#                         chunks.append(current_text_chunk)
#                         current_text_chunk = []
#                     chunks.append([data])
#                 else:
#                     current_text_chunk.append(data)
#                     if len(current_text_chunk) >= chunk_size:
#                         chunks.append(current_text_chunk)
#                         current_text_chunk = []
#             if current_text_chunk:
#                 chunks.append(current_text_chunk)

#             def _process_chunk(chunk):
#                 page_nums = [d['p_idx'] + 1 for d in chunk]
#                 start_time = time.time()
#                 print(f"[DEBUG] Starting AI extraction for page(s) {page_nums}")

#                 account_list_hint = ", ".join(
#                     str(a.get('account_number')) for a in getattr(self, 'detected_accounts', []) if a.get('account_number')
#                 ) or str(self.account_number)

#                 tx_instruction = (
#                     f"Extract ALL transaction line items from page(s) {page_nums} of this bank statement.\n"
#                     "If multiple pages are included below, each is clearly marked with a '=== PAGE N ===' "
#                     "header — extract transactions from EVERY page shown, not just the first one.\n"
#                     "MULTIPLE ACCOUNTS: This statement may cover more than one bank account "
#                     f"(known account numbers in this document: {account_list_hint}). Each account's transactions "
#                     "appear under their own 'Acct Ending XXXX (Continued)' / 'Account Title' / 'DETAIL "
#                     "TRANSACTIONS BY DATE' heading. For EVERY transaction, set 'account_number' to the account "
#                     "number shown in the nearest preceding such heading on the SAME page. If a page has no "
#                     f"account heading of its own, use the account from the most recent page that did: \"{self.account_number}\" "
#                     "as the default if nothing else applies. Never merge two different accounts' transactions "
#                     "under one account_number — a page switching from one account's detail section to another's "
#                     "(as can happen partway down a page) means transactions above and below that switch belong "
#                     "to DIFFERENT accounts.\n"
#                     "CRITICAL SIGN RULE: 'amount' MUST be POSITIVE for deposits/credits/inflows, and NEGATIVE for debits/withdrawals/payments/fees.\n"
#                     "SPECIAL RULE FOR CHECKS: If a page contains a 'CHECKS' or 'CHECK NUMBER' table, EVERY amount in that "
#                     "table is a NEGATIVE outflow (money leaving the account) even though no minus sign is printed next to it. "
#                     "Checks-paid tables NEVER show a minus sign in bank statements — you must apply the negative sign yourself.\n"
#                     "SPECIAL RULE FOR CHECK IMAGES: Some statements include a page of scanned/photographed check images "
#                     "(front-of-check facsimiles) near the end, each with its check number, date, and amount printed as a "
#                     "caption. These are NOT new transactions — they are pictures of checks already listed once in the "
#                     "'CHECKS' table elsewhere in the statement. If a page is a check-images page, contribute NO "
#                     "transactions from it. Only extract from the actual CHECKS table (plain rows of check number/date/"
#                     "amount with no check imagery), never from a page showing the check facsimiles themselves.\n"
#                     "JSON Schema:\n"
#                     "{\n"
#                     "  \"transactions\": [\n"
#                     "    {\n"
#                     "      \"date\": \"YYYY-MM-DD\",\n"
#                     "      \"description\": \"Full description text\",\n"
#                     "      \"amount\": -150.00,\n"
#                     "      \"balance\": 4500.00,\n"
#                     "      \"category\": \"Category name or null\",\n"
#                     "      \"account_number\": \"9625\"\n"
#                     "    }\n"
#                     "  ]\n"
#                     "}"
#                 )

#                 if len(chunk) == 1 and chunk[0]['use_vision'] and chunk[0]['b64_str']:
#                     user_msg = [
#                         {"type": "text", "text": tx_instruction},
#                         {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{chunk[0]['b64_str']}"}}
#                     ]
#                 else:
#                     combined_text = "\n\n".join(
#                         f"=== PAGE {d['p_idx'] + 1} ===\n{d['p_text']}" for d in chunk
#                     )
#                     user_msg = f"{tx_instruction}\n\n{combined_text}"

#                 try:
#                     tx_res = make_ai_request([
#                         {"role": "system", "content": "You are a precise financial transaction audit extraction agent."},
#                         {"role": "user", "content": user_msg}
#                     ])
#                 except Exception as page_err:
#                     print(f"🛑 [DEBUG] ERROR on page(s) {page_nums}: {page_err}")
#                     raise
#                     # return []

#                 # print(f"[DEBUG] Raw AI Response for page(s) {page_nums}: {json.dumps(tx_res)}")

#                 chunk_transactions = []
#                 for tx in tx_res.get("transactions", []):
#                     d_val = parse_iso_date(tx.get("date"))
#                     if not d_val:
#                         continue

#                     desc_text = str(tx.get("description") or "Transaction").strip()
#                     amt_val = self.clean_amount(tx.get("amount"))
#                     bal_val = self.clean_amount(tx.get("balance")) if tx.get("balance") is not None else None
#                     cat_val = self.derive_category(desc_text, tx.get("category"), amt_val)

#                     chunk_transactions.append({
#                         'date': d_val,
#                         'description': desc_text,
#                         'amount': amt_val,
#                         'balance': bal_val,
#                         'category': cat_val,
#                         'account_number': tx.get('account_number') or self.account_number
#                     })

#                 elapsed = time.time() - start_time
#                 print(f"[DEBUG] Page(s) {page_nums} extraction completed in {elapsed:.2f}s (Found {len(chunk_transactions)} txs)")
#                 return chunk_transactions

#             # Step 2: Extract Transactions chunk-by-chunk concurrently
#             transactions = []
#             total_start_time = time.time()
#             max_workers = getattr(settings, 'MAX_PDF_WORKERS', 2)
#             print(f"[DEBUG] Submitting {len(chunks)} chunk(s) covering {len(pages)} pages to ThreadPoolExecutor (max_workers={max_workers})...")

#             with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
#                 futures = [executor.submit(_process_chunk, chunk) for chunk in chunks]
#                 for future in concurrent.futures.as_completed(futures):
#                     transactions.extend(future.result())
                    
#             total_elapsed = time.time() - total_start_time
#             print(f"[DEBUG] All pages extracted in {total_elapsed:.2f}s. Total transactions combined: {len(transactions)}")

#             # Safety-net dedup: some statements repeat every check as a
#             # scanned check-image page near the end (check number/date/amount
#             # printed as a caption under each image). If the AI extraction
#             # prompt fails to skip that page, the same check gets extracted
#             # twice — once from the real CHECKS table, once from the image
#             # captions — silently inflating Total Payments. Collapse any
#             # transactions that share the same date AND amount AND look like
#             # a check (description is just a bare check number, e.g. "52233"
#             # or "Check 52233"), keeping only the first occurrence.
#             seen_checks = set()
#             deduped = []
#             check_desc_re = re.compile(r'^\s*(?:check\s*#?\s*)?\d{4,6}\s*\*?\s*$', re.IGNORECASE)
#             for tx in transactions:
#                 is_check_like = tx['amount'] < 0 and check_desc_re.match(tx['description'] or '')
#                 if is_check_like:
#                     key = (self._normalize_account_number(tx.get('account_number')), tx['date'], tx['amount'])
#                     if key in seen_checks:
#                         print(f"[DEBUG] Dropping duplicate check-image transaction: {tx['date']} {tx['amount']} '{tx['description']}'")
#                         continue
#                     seen_checks.add(key)
#                 deduped.append(tx)
#             transactions = deduped

#         # Empty transactions is only a real failure if metadata extraction
#         # also failed — meaning the AI likely couldn't read the document at
#         # all. A dormant account (just BEGINNING/ENDING BALANCE lines) is a
#         # valid, legitimate zero-transaction result and must still surface
#         # on the dashboard with its bank name / account number / balances,
#         # not get dropped or sent through the legacy parser.
#         metadata_looks_valid = (
#             self.account_number != "Unknown Account"
#             and self.bank_name != "Unknown Bank"
#         )
#         if not transactions and not metadata_looks_valid:
#             raise ValueError(
#                 "AI extraction produced 0 transactions and no usable "
#                 "metadata (bank_name/account_number) — treating as a "
#                 "genuine failure."
#             )
#         if not transactions:
#             print(f"[DEBUG] 0 transactions found, but metadata is valid "
#                 f"(bank={self.bank_name}, acct={self.account_number}) — "
#                 f"dormant-account statement, not a failure.")

#         # Sort transactions by date
#         transactions.sort(key=lambda x: x['date'])

#         if transactions:
#             if not self.start_date:
#                 self.start_date = transactions[0]['date']
#             if not self.end_date:
#                 self.end_date = transactions[-1]['date']
#         # else: keep self.start_date/self.end_date as already set from meta_prompt

#         # --- Split transactions by account -------------------------------
#         primary_norm = self._normalize_account_number(self.account_number)
#         groups = {}
#         for tx in transactions:
#             acct_raw = tx.pop('account_number', None) or self.account_number
#             acct_norm = self._normalize_account_number(acct_raw) or primary_norm
#             grp = groups.setdefault(acct_norm, {'account_number': acct_raw, 'txs': []})
#             grp['txs'].append(tx)

#         # No transactions at all -> groups would be empty -> no account
#         # would ever reach the dashboard. Force the primary account through
#         # with its extracted metadata and an empty transaction list.
#         if not groups:
#             groups[primary_norm] = {'account_number': self.account_number, 'txs': []}

#         results = []
#         for acct_norm, grp in groups.items():
#             group_txs = sorted(grp['txs'], key=lambda x: x['date'])
#             # NOTE: removed the old `if not group_txs: continue` — that
#             # line (if present) is exactly what would silently drop a
#             # dormant account from the dashboard. Do not skip empty groups.

#             is_primary = (acct_norm == primary_norm)
#             detected = next(
#                 (a for a in getattr(self, 'detected_accounts', [])
#                  if self._normalize_account_number(a.get('account_number')) == acct_norm),
#                 None
#             )

#             if is_primary:
#                 beginning_balance = self.beginning_balance
#                 ending_balance = self.ending_balance
#                 start_date = self.start_date
#                 end_date = self.end_date
#             else:
#                 beginning_balance = None
#                 ending_balance = None
#                 if detected and detected.get('beginning_balance') is not None:
#                     try:
#                         beginning_balance = Decimal(str(detected['beginning_balance']))
#                     except Exception:
#                         beginning_balance = None
#                 if detected and detected.get('ending_balance') is not None:
#                     try:
#                         ending_balance = Decimal(str(detected['ending_balance']))
#                     except Exception:
#                         ending_balance = None
#                 start_date = group_txs[0]['date'] if group_txs else self.start_date
#                 end_date = group_txs[-1]['date'] if group_txs else self.end_date

#             if (beginning_balance is None or beginning_balance == Decimal("0.00")) and group_txs and group_txs[0].get('balance') is not None:
#                 beginning_balance = group_txs[0]['balance'] - group_txs[0]['amount']
#             if (ending_balance is None or ending_balance == Decimal("0.00")) and group_txs and group_txs[-1].get('balance') is not None:
#                 ending_balance = group_txs[-1]['balance']
#             beginning_balance = beginning_balance if beginning_balance is not None else Decimal("0.00")
#             ending_balance = ending_balance if ending_balance is not None else Decimal("0.00")

#             account_meta = {
#                 'bank_name': self.bank_name,
#                 'account_number': grp['account_number'],
#                 'account_holder': self.account_holder,
#                 'currency': self.currency,
#                 'start_date': start_date,
#                 'end_date': end_date,
#                 'beginning_balance': beginning_balance,
#                 'ending_balance': ending_balance,
#             }
#             results.append((account_meta, group_txs))

#         return results

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

#             account_meta = {
#             'bank_name': self.bank_name,
#             'account_number': self.account_number,
#             'account_holder': self.account_holder,
#             'currency': self.currency,
#             'start_date': self.start_date,
#             'end_date': self.end_date,
#             'beginning_balance': self.beginning_balance or Decimal('0.00'),
#             'ending_balance': self.ending_balance or Decimal('0.00'),
#         }

#         # Legacy rule-based parser only ever handles a single account, but
#         # the caller (manager.py) now expects a list of (account_meta,
#         # transactions) pairs to support multi-account PDFs from the AI
#         # path — wrap this single result to match that interface.
#         return [(account_meta, transactions)]

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


import base64
import concurrent.futures
import datetime
import io
import json
import logging
import random
import re
import threading
import time
import traceback
from decimal import Decimal, InvalidOperation

import pdfplumber
from django.conf import settings
from openai import AzureOpenAI
from openai import OpenAI

from .base import BaseParser

logger = logging.getLogger(__name__)


# Global, process-wide cap on simultaneous OpenAI API calls (see original
# note): every AI call from any file/thread acquires this first.
_GLOBAL_AI_CALL_SEMAPHORE = threading.Semaphore(
    getattr(settings, 'MAX_TOTAL_CONCURRENT_AI_CALLS', 15)
)

# Page types the model may report for pages that legitimately hold no
# transactions. Anything else with 0 transactions is treated as suspicious.
NON_TX_PAGE_TYPES = {'summary', 'check_images', 'blank', 'other', 'disclosures'}

# A line that starts with a date and ends with an amount, e.g.
# "1/05 Wire Transfer Credit   9,000.00" or "Jan 05 ACH ... 84.00-"
_TX_LINE_RE = re.compile(
    r'^\s*(?:\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|[A-Z][a-z]{2}\s+\d{1,2})\s+\S.*\d[\d,]*\.\d{2}',
    re.MULTILINE,
)
# Pulls a check number out of descriptions like "Check 83425", "Check No. 83425",
# "83425*", "CHECK #83425". Bare numbers must be the whole description.
_CHECK_NUM_RE = re.compile(r'^\s*(?:check\b\D{0,30}?)?(\d{3,8})\s*\*?\s*$', re.IGNORECASE)


# pdfium (used by pdfplumber's page.to_image) is NOT thread-safe. Several
# statements are parsed at the same time in different threads, and two
# simultaneous renders can crash the whole Python process (segfault, no
# traceback). Every render in this module must hold this lock.
_PDFIUM_LOCK = threading.Lock()


class StatementParseError(ValueError):
    """The statement could not be read reliably. Message is user-presentable."""


class _TruncatedResponse(Exception):
    """Model output was cut off (finish_reason == 'length')."""


class _EmptyResponse(Exception):
    """Model returned no content at all."""


def _dbg(msg):
    print(f"[DEBUG] {msg}")


class PDFStatementParser(BaseParser):
    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_account_number(value):
        """Digits-only form of an account number/tail, for matching the
        same account across different formatting ('Acct Ending 9625' vs
        '******9625' vs '9625')."""
        digits = re.sub(r'\D', '', str(value or ''))
        return digits or None

    @staticmethod
    def _safe_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_decimal(value):
        if value is None or value == '':
            return None
        try:
            return Decimal(str(value).replace(',', '').replace('$', '').strip())
        except (InvalidOperation, ValueError):
            return None

    def _parse_iso_date(self, d_str):
        if not d_str:
            return None
        try:
            return datetime.datetime.strptime(str(d_str).strip(), "%Y-%m-%d").date()
        except Exception:
            try:
                return self.parse_date(d_str)
            except Exception:
                return None

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def parse(self):
        """
        AI-first statement parsing.

        1. Every page is classified as text or vision (scanned/garbled).
        2. Transactions are extracted per page (chunked for text pages).
        3. Pages that look like they should have transactions but returned
           none are retried once at higher resolution.
        4. Results are reconciled against the statement balances; problems
           are recorded in `self.review_notes` instead of being hidden.

        Falls back to the rule-based parser ONLY when the PDF has a real
        text layer. Scanned PDFs cannot be parsed by the rule-based parser,
        so for those we fail with a clear message instead.
        """
        self.review_notes = []
        self._vision_page_ratio = 0.0
        try:
            return self._parse_via_openai()
        except Exception as ai_err:
            print("\n" + "=" * 50)
            print("🛑 AI PARSING FAILED! DEBUG INFO BELOW:")
            print("=" * 50)
            traceback.print_exc()
            print("=" * 50 + "\n")
            logger.warning("AI parsing failed (%s).", ai_err)

            if self._vision_page_ratio >= 0.5:
                raise StatementParseError(
                    "This looks like a scanned / image-only PDF and AI vision "
                    f"extraction failed: {ai_err}"
                ) from ai_err

            logger.warning("Falling back to rule-based parser.")
            return self._parse_via_legacy_rules()

    # ------------------------------------------------------------------
    # AI plumbing
    # ------------------------------------------------------------------
    def _build_client(self):
        api_key = getattr(settings, "OPENAI_API_KEY", None)
        # endpoint = getattr(settings, "AZURE_OPENAI_ENDPOINT", None)
        if not api_key:
            raise ValueError("AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT are not defined in Django settings.")
        return OpenAI(
            api_key=api_key,
            # azure_endpoint=endpoint,
            # If image input seems to be ignored on your deployment, try a
            # newer version here (e.g. "2024-10-21" or later).
            # api_version=getattr(settings, "AZURE_OPENAI_API_VERSION", "2024-02-15-preview"),
        )

    @staticmethod
    def _loads_json(content):
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.IGNORECASE).strip()
        return json.loads(text)

    def _ai_request(self, client, messages, max_retries=4):
        """One JSON chat completion with retries on transient errors.
        The global semaphore is held only during the call itself, never
        while sleeping between retries."""
        model = getattr(settings, "OPENAI_MODEL", "gpt-5.5")
        for attempt in range(max_retries):
            try:
                with _GLOBAL_AI_CALL_SEMAPHORE:
                    response = client.chat.completions.create(
                        model='gpt-5.5',
                        messages=messages,
                        response_format={"type": "json_object"},
                        timeout=90,
                    )
                choice = response.choices[0]
                finish = choice.finish_reason
                content = choice.message.content
                if finish == 'length':
                    raise _TruncatedResponse("Model output truncated (finish_reason=length)")
                if finish == 'content_filter':
                    raise StatementParseError("The AI provider's content filter blocked this request.")
                if not content:
                    refusal = getattr(choice.message, 'refusal', None)
                    raise _EmptyResponse(f"Empty model response (finish_reason={finish!r}, refusal={refusal!r})")
                return self._loads_json(content)
            except (_TruncatedResponse, StatementParseError):
                raise
            except Exception as e:
                err_text = str(e).lower()
                is_rate_limit = (
                    '429' in err_text or 'rate limit' in err_text
                    or 'rate_limit' in err_text or 'too many requests' in err_text
                )
                is_transient = (
                    is_rate_limit or isinstance(e, _EmptyResponse)
                    or 'timeout' in err_text or 'timed out' in err_text
                    or '500' in err_text or '502' in err_text or '503' in err_text
                )
                if not is_transient or attempt == max_retries - 1:
                    raise
                base_wait = 5 if is_rate_limit else 2
                wait_s = base_wait * (2 ** attempt) + random.uniform(0, 1)
                _dbg(f"Transient API error ({e}); retrying in {wait_s:.1f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_s)

    # ------------------------------------------------------------------
    # Page classification / rendering
    # ------------------------------------------------------------------
    @staticmethod
    def _page_needs_vision(page, text):
        stripped = text.strip()
        if len(stripped) < 50:
            return True  # scanned / image-only
        cid_count = len(re.findall(r'\(cid:\d+\)', text))
        non_ascii = sum(1 for c in text if (ord(c) < 32 and c not in '\n\r\t') or ord(c) > 126)
        if (cid_count * 10 + non_ascii) / max(len(text), 1) > 0.3:
            return True  # garbled font encoding
        try:
            if len(stripped) < 200 and len(page.images) > 0:
                return True  # a photo/scan with only a stamp of text on it
        except Exception:
            pass
        return False

    @staticmethod
    def _render_b64(page, dpi):
        """Render a page to a base64 JPEG. Deliberately raises on failure:
        callers must decide what to do, never silently continue without
        an image."""
        with _PDFIUM_LOCK:
            img = page.to_image(resolution=dpi).original.convert("L")
        img.thumbnail((2000, 2000))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode("utf-8")

    def _prepare_pages(self, pages):
        """Sequential (pdfplumber is not thread-safe): extract text and
        render images for pages that need vision."""
        dpi = getattr(settings, 'PDF_VISION_DPI', 150)
        page_data = []
        for p_idx, page in enumerate(pages):
            text = page.extract_text() or ""
            use_vision = self._page_needs_vision(page, text)
            b64 = None
            if use_vision:
                try:
                    b64 = self._render_b64(page, dpi)
                except Exception as e:
                    _dbg(f"Vision render FAILED for page {p_idx + 1}: {e!r}")
                    if len(text.strip()) >= 50:
                        _dbg(f"Page {p_idx + 1}: falling back to (garbled) text mode")
                        use_vision = False
            page_data.append({'p_idx': p_idx, 'p_text': text, 'use_vision': use_vision, 'b64_str': b64})
            try:
                page.flush_cache()
            except Exception:
                pass
        return page_data

    @staticmethod
    def _make_chunks(page_data):
        """Group consecutive text pages; vision pages are single-page chunks."""
        chunk_size = getattr(settings, 'PDF_PAGE_CHUNK_SIZE', 5)
        chunks, current = [], []
        for data in page_data:
            if data['use_vision']:
                if current:
                    chunks.append(current)
                    current = []
                chunks.append([data])
            else:
                current.append(data)
                if len(current) >= chunk_size:
                    chunks.append(current)
                    current = []
        if current:
            chunks.append(current)
        return chunks

    # ------------------------------------------------------------------
    # Metadata extraction
    # ------------------------------------------------------------------
    def _extract_metadata(self, client, pages, page_data):
        header_text = "\n".join(d['p_text'] for d in page_data[:3])
        first_text = page_data[0]['p_text']
        self._extract_metadata_from_text(header_text, first_page_text=first_text)

        last_text = page_data[-1]['p_text'] if len(pages) > 3 else ""

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
            "(or image) if provided; it is included specifically because the ending balance often only "
            "appears there.\n"
            "If a value cannot be found, use null — never guess.\n"
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
        )

        # Scanned / garbled header -> send page images instead of (empty) text.
        if page_data[0]['use_vision'] or len(header_text.strip()) < 200:
            idxs = list(dict.fromkeys(list(range(min(3, len(pages)))) + [len(pages) - 1]))[:4]
            dpi = getattr(settings, 'PDF_VISION_DPI', 150)
            content = [{"type": "text", "text": meta_prompt + "The statement pages are attached as images "
                        "(first pages, then the last page)."}]
            for i in idxs:
                try:
                    b64 = page_data[i]['b64_str'] or self._render_b64(pages[i], dpi)
                except Exception as e:
                    _dbg(f"Metadata: could not render page {i + 1}: {e!r}")
                    continue
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}})
            if len(content) == 1:
                content = meta_prompt + f"HEADER TEXT:\n{header_text[:4000]}"
        else:
            content = meta_prompt + f"HEADER TEXT:\n{header_text[:4000]}"
            if last_text:
                content += f"\n\nLAST PAGE OF DOCUMENT:\n{last_text[:3000]}"

        try:
            meta_res = self._ai_request(client, [
                {"role": "system", "content": "You are an expert bank statement metadata extraction agent."},
                {"role": "user", "content": content},
            ])
        except Exception as e:
            _dbg(f"Metadata extraction call FAILED: {e!r}")
            meta_res = {}
        if not isinstance(meta_res, dict):
            meta_res = {}

        def _is_usable(value, placeholder, unknown_value):
            if not value:
                return False
            v = str(value).strip().lower()
            return v not in ('', placeholder.lower(), unknown_value.lower(), 'null', 'none', 'n/a')

        ai_bank = meta_res.get("bank_name")
        if _is_usable(ai_bank, "Bank Name", "Unknown Bank"):
            self.bank_name = ai_bank
        elif self.bank_name == "Unknown Bank":
            _dbg(f"Metadata extraction returned no usable bank_name ({ai_bank!r}); keeping {self.bank_name!r}")

        ai_acct = meta_res.get("account_number")
        if _is_usable(ai_acct, "Account Number", "Unknown Account"):
            self.account_number = str(ai_acct).replace('-', '')
        elif self.account_number == "Unknown Account":
            _dbg(f"Metadata extraction returned no usable account_number ({ai_acct!r}); keeping {self.account_number!r}")

        if meta_res.get("account_holder"):
            self.account_holder = meta_res.get("account_holder")

        beg = self._to_decimal(meta_res.get("beginning_balance"))
        if beg is not None and self.beginning_balance is None:
            self.beginning_balance = beg
        elif self.beginning_balance is None:
            self.beginning_balance = Decimal("0.00")

        end = self._to_decimal(meta_res.get("ending_balance"))
        if end is not None and self.ending_balance is None:
            self.ending_balance = end
        elif self.ending_balance is None:
            self.ending_balance = Decimal("0.00")

        if not self.start_date:
            self.start_date = self._parse_iso_date(meta_res.get("start_date"))
        if not self.end_date:
            self.end_date = self._parse_iso_date(meta_res.get("end_date"))

        accounts = []
        for a in (meta_res.get("accounts") or []):
            if isinstance(a, dict) and a.get('account_number'):
                accounts.append({
                    'account_number': a['account_number'],
                    'beginning_balance': self._to_decimal(a.get('beginning_balance')),
                    'ending_balance': self._to_decimal(a.get('ending_balance')),
                })
        self.detected_accounts = accounts
        primary_norm = self._normalize_account_number(self.account_number)
        if not any(self._normalize_account_number(a['account_number']) == primary_norm for a in accounts):
            accounts.append({
                'account_number': self.account_number,
                'beginning_balance': self.beginning_balance,
                'ending_balance': self.ending_balance,
            })

    # ------------------------------------------------------------------
    # Transaction extraction
    # ------------------------------------------------------------------
    def _tx_instruction(self, page_nums):
        account_list_hint = ", ".join(
            str(a.get('account_number')) for a in getattr(self, 'detected_accounts', []) if a.get('account_number')
        ) or str(self.account_number)
        period = ""
        if self.start_date and self.end_date:
            period = f"Statement period (from header metadata): {self.start_date} to {self.end_date}.\n"

        return (
            f"Extract ALL transaction line items from page(s) {page_nums} of this bank statement.\n"
            + period +
            "Dates often omit the year (e.g. '1/05'). Infer the year from the statement date/period printed "
            "on the page or given above, and ALWAYS output full YYYY-MM-DD dates.\n"
            "If multiple pages are included, each is marked '=== PAGE N ==='. Return exactly ONE entry in "
            "'pages' for EVERY page shown, with its page number.\n"
            "'page_type' must be one of: transactions | summary | check_images | blank | other | unreadable.\n"
            "  - 'transactions': the page contains ANY transaction rows (then list them all).\n"
            "  - 'unreadable': you cannot read the page (never return an empty list for a page that visibly "
            "contains transaction rows — use 'unreadable' instead).\n"
            "MULTIPLE ACCOUNTS: This statement may cover more than one bank account "
            f"(known account numbers in this document: {account_list_hint}). Each account's transactions "
            "appear under their own 'Acct Ending XXXX (Continued)' / 'Account Title' / 'DETAIL "
            "TRANSACTIONS BY DATE' heading. For EVERY transaction, set 'account_number' to the account "
            "number shown in the nearest preceding such heading on the SAME page. If a page has no "
            f"account heading of its own, use the account from the most recent page that did, or \"{self.account_number}\" "
            "as the default if nothing else applies. Never merge two different accounts' transactions "
            "under one account_number — a page switching from one account's detail section to another's "
            "(as can happen partway down a page) means transactions above and below that switch belong "
            "to DIFFERENT accounts.\n"
            "CRITICAL SIGN RULE: 'amount' MUST be POSITIVE for deposits/credits/inflows, and NEGATIVE for "
            "debits/withdrawals/payments/fees. Some banks print a trailing minus ('84.00-') for debits — that "
            "is NEGATIVE. Use the running balance column, if present, to double-check the sign.\n"
            "SPECIAL RULE FOR CHECKS: If a page contains a 'CHECKS', 'CHECK DETAIL' or 'CHECK NUMBER' table, "
            "EVERY amount in that table is a NEGATIVE outflow even though no minus sign is printed next to it.\n"
            "For EVERY transaction set 'section': 'detail' if the row comes from the main running-balance ledger "
            "(e.g. 'DETAIL TRANSACTIONS BY DATE'), 'check_list' if it comes from a separate table that only "
            "lists checks (e.g. 'CHECK DETAIL'), otherwise 'other'. For check rows set 'check_number' (string), "
            "otherwise null. Extract check-list rows too — they will be de-duplicated afterwards against the main "
            "ledger.\n"
            "SPECIAL RULE FOR CHECK IMAGES: Pages of scanned/photographed check images (facsimiles with the "
            "check number, date and amount as a caption) are NOT new transactions — they duplicate the "
            "'CHECKS' table. For such a page return page_type 'check_images' and NO transactions.\n"
            "Preserve the order of rows exactly as printed. Copy the running 'balance' when the statement "
            "prints one, else null.\n"
            "JSON Schema:\n"
            "{\n"
            "  \"pages\": [\n"
            "    {\n"
            "      \"page\": 2,\n"
            "      \"page_type\": \"transactions\",\n"
            "      \"transactions\": [\n"
            "        {\n"
            "          \"date\": \"YYYY-MM-DD\",\n"
            "          \"description\": \"Full description text\",\n"
            "          \"amount\": -150.00,\n"
            "          \"balance\": 4500.00,\n"
            "          \"category\": \"Category name or null\",\n"
            "          \"account_number\": \"9625\",\n"
            "          \"section\": \"detail\",\n"
            "          \"check_number\": null\n"
            "        }\n"
            "      ]\n"
            "    }\n"
            "  ]\n"
            "}"
        )

    def _build_tx(self, raw):
        if not isinstance(raw, dict):
            return None
        d_val = self._parse_iso_date(raw.get("date"))
        if not d_val:
            return None
        desc = str(raw.get("description") or "Transaction").strip()
        try:
            amt = self.clean_amount(raw.get("amount"))
            bal = self.clean_amount(raw.get("balance")) if raw.get("balance") is not None else None
        except Exception:
            _dbg(f"Skipping row with unparseable amount/balance: {raw!r}")
            return None
        return {
            'date': d_val,
            'description': desc,
            'amount': amt,
            'balance': bal,
            'category': self.derive_category(desc, raw.get("category"), amt),
            'account_number': raw.get('account_number') or self.account_number,
            # internal only — removed before results are returned
            '_section': str(raw.get('section') or '').strip().lower() or None,
            '_check_no': (str(raw.get('check_number')).strip().rstrip('*') if raw.get('check_number') else None),
        }

    def _normalise_tx_response(self, tx_res, page_nums):
        """-> (transactions, {page: count}, {page: page_type})"""
        entries = tx_res.get('pages') if isinstance(tx_res, dict) else None
        if not isinstance(entries, list):
            # tolerate the old flat {"transactions": [...]} shape
            entries = [{'page': None, 'page_type': None,
                        'transactions': (tx_res or {}).get('transactions') or []}]
        txs, counts, page_types = [], {}, {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            n = page_nums[0] if len(page_nums) == 1 else self._safe_int(entry.get('page'))
            ptype = str(entry.get('page_type') or '').strip().lower() or None
            if n is not None:
                page_types[n] = ptype
            if ptype == 'check_images':
                _dbg(f"Page {n}: check-image page, ignoring its rows")
                continue
            for raw in entry.get('transactions') or []:
                tx = self._build_tx(raw)
                if not tx:
                    continue
                txs.append(tx)
                if n is not None:
                    counts[n] = counts.get(n, 0) + 1
        return txs, counts, page_types

    @staticmethod
    def _find_suspicious_pages(chunk, counts, page_types):
        """Pages that returned 0 transactions although they probably have some."""
        suspicious = []
        for d in chunk:
            n = d['p_idx'] + 1
            if counts.get(n, 0) > 0:
                continue
            ptype = page_types.get(n)
            if d['use_vision']:
                if ptype not in NON_TX_PAGE_TYPES:  # None, 'transactions', 'unreadable'
                    suspicious.append(n)
            else:
                looks_like_tx = len(_TX_LINE_RE.findall(d['p_text'])) >= 5
                if looks_like_tx or ptype in ('transactions', 'unreadable'):
                    suspicious.append(n)
        return suspicious

    def _extract_chunk(self, client, chunk):
        page_nums = [d['p_idx'] + 1 for d in chunk]
        start = time.time()
        _dbg(f"Starting AI extraction for page(s) {page_nums}")

        is_vision = len(chunk) == 1 and chunk[0]['use_vision']
        if is_vision and not chunk[0]['b64_str']:
            _dbg(f"Page(s) {page_nums} need vision but have no image — marking suspicious (never calling AI without the image)")
            return {'txs': [], 'suspicious': page_nums}

        instruction = self._tx_instruction(page_nums)
        if is_vision:
            user_msg = [
                {"type": "text", "text": instruction},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{chunk[0]['b64_str']}", "detail": "high"}},
            ]
        else:
            combined = "\n\n".join(f"=== PAGE {d['p_idx'] + 1} ===\n{d['p_text']}" for d in chunk)
            user_msg = f"{instruction}\n\n{combined}"

        try:
            tx_res = self._ai_request(client, [
                {"role": "system", "content": "You are a precise financial transaction audit extraction agent."},
                {"role": "user", "content": user_msg},
            ])
        except _TruncatedResponse:
            if len(chunk) > 1:
                _dbg(f"Page(s) {page_nums}: output truncated, retrying page by page")
                merged = {'txs': [], 'suspicious': []}
                for d in chunk:
                    sub = self._extract_chunk(client, [d])
                    merged['txs'].extend(sub['txs'])
                    merged['suspicious'].extend(sub['suspicious'])
                return merged
            raise
        except Exception as err:
            print(f"🛑 [DEBUG] ERROR on page(s) {page_nums}: {err}")
            raise

        txs, counts, page_types = self._normalise_tx_response(tx_res, page_nums)
        suspicious = self._find_suspicious_pages(chunk, counts, page_types)

        elapsed = time.time() - start
        mode = "vision" if is_vision else "text"
        _dbg(f"Page(s) {page_nums} [{mode}] done in {elapsed:.2f}s "
             f"(Found {len(txs)} txs, types={page_types}, suspicious={suspicious})")
        if not txs and suspicious:
            snippet = json.dumps(tx_res)[:300] if tx_res is not None else 'None'
            _dbg(f"Page(s) {page_nums}: 0 txs from a page that looks like it has some. Raw response: {snippet}")
        return {'txs': txs, 'suspicious': suspicious}

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------
    def _check_number(self, tx):
        """Check number for a check row (from the model, else from the description)."""
        if tx.get('_check_no'):
            return re.sub(r'\D', '', tx['_check_no']) or None
        m = _CHECK_NUM_RE.match(tx.get('description') or '')
        return m.group(1) if m else None

    def _dedupe_checks(self, transactions):
        """Many statements list every check twice: once in the main ledger and
        again in a separate CHECK DETAIL table. Rows are matched on
        (account, check number, amount) — not on date/description — and the
        ledger row (which carries the running balance) wins. A check that
        appears ONLY in the check table is kept."""
        def key(tx):
            no = self._check_number(tx)
            if not no or tx['amount'] >= 0:
                return None
            return (self._normalize_account_number(tx.get('account_number')), no, tx['amount'])

        ledger_keys = {key(t) for t in transactions
                       if key(t) and t.get('_section') != 'check_list'}
        seen, out, dropped = set(), [], 0
        for tx in transactions:
            k = key(tx)
            if k:
                if tx.get('_section') == 'check_list' and k in ledger_keys:
                    dropped += 1
                    continue
                if k in seen:
                    dropped += 1
                    continue
                seen.add(k)
            out.append(tx)
        if dropped:
            _dbg(f"Dropped {dropped} duplicate check row(s) (same check listed in ledger and check table)")
        return out

    def _repair_from_running_balances(self, transactions):
        """The printed running balance is usually more reliable than a
        vision-read amount, so for each row where prev_balance + amount !=
        balance we look at the NEXT row to decide what was misread:
          * next row ties out against this row's balance -> the AMOUNT was
            misread -> amount = balance - prev
          * next row ties out against prev + this amount -> the BALANCE was
            misread -> balance = prev + amount
          * otherwise leave the row alone.

        SAFETY: repairs are only kept if they make the account reconcile
        (beginning + sum(amounts) == ending). If the account already
        reconciles, nothing is touched. This matters for statements that
        print only a daily/ending balance (not a balance per row), where the
        'running balance' logic does not apply and would corrupt good data."""
        tol = Decimal("0.01")
        primary_norm = self._normalize_account_number(self.account_number)

        def acct_of(tx):
            return self._normalize_account_number(tx.get('account_number')) or primary_norm

        by_acct = {}
        for i, tx in enumerate(transactions):
            by_acct.setdefault(acct_of(tx), []).append(i)

        for acct, idxs in by_acct.items():
            detected = next(
                (a for a in getattr(self, 'detected_accounts', [])
                 if self._normalize_account_number(a.get('account_number')) == acct), None)
            beg = self._to_decimal(detected.get('beginning_balance')) if detected else None
            end = self._to_decimal(detected.get('ending_balance')) if detected else None
            if acct == primary_norm:
                beg = beg if beg is not None else self.beginning_balance
                end = end if end is not None else self.ending_balance
            if beg is None or end is None:
                continue  # cannot verify a repair -> do not attempt one

            rows = [transactions[i] for i in idxs]
            err_before = abs(beg + sum((t['amount'] for t in rows), Decimal("0.00")) - end)
            if err_before <= tol:
                continue  # already reconciles; leave it alone

            journal, notes = [], []
            for pos, cur in enumerate(rows):
                if cur.get('balance') is None:
                    continue
                prev_bal = beg if pos == 0 else rows[pos - 1].get('balance')
                if prev_bal is None:
                    continue
                if abs(prev_bal + cur['amount'] - cur['balance']) <= tol:
                    continue

                nxt = rows[pos + 1] if pos + 1 < len(rows) else None
                nxt_ties_to_cur_balance = (
                    nxt is None or nxt.get('balance') is None
                    or abs(cur['balance'] + nxt['amount'] - nxt['balance']) <= tol
                )
                label = f"{cur['date']} '{(cur['description'] or '')[:40]}'"
                if nxt_ties_to_cur_balance:
                    old, new = cur['amount'], cur['balance'] - prev_bal
                    journal.append((cur, 'amount', old))
                    cur['amount'] = new
                    notes.append(f"Auto-corrected amount {old} -> {new} for account {acct}, {label} (from running balance)")
                elif nxt.get('balance') is not None and \
                        abs(prev_bal + cur['amount'] + nxt['amount'] - nxt['balance']) <= tol:
                    old, new = cur['balance'], prev_bal + cur['amount']
                    journal.append((cur, 'balance', old))
                    cur['balance'] = new
                    notes.append(f"Auto-corrected balance {old} -> {new} for account {acct}, {label}")

            if not journal:
                continue
            err_after = abs(beg + sum((t['amount'] for t in rows), Decimal("0.00")) - end)
            if err_after <= tol:
                for tx, field, _old in journal:
                    if field == 'amount':
                        tx['category'] = self.derive_category(tx['description'], tx.get('category'), tx['amount'])
                self.review_notes.extend(notes)
                _dbg(f"Account {acct}: repaired {len(journal)} value(s) using the running balance; now reconciles")
            else:
                for tx, field, old in reversed(journal):
                    tx[field] = old
                self.review_notes.append(
                    f"Account {acct}: running-balance repair was tried but would not reconcile "
                    f"(error {err_before} -> {err_after}); original values kept — please review"
                )

    def _check_running_balances(self, transactions):
        """Walk rows in ORIGINAL statement order and verify that each printed
        balance equals the previous balance plus the amounts since. Catches
        missed rows, duplicates and flipped signs without knowing the bank."""
        tol = Decimal("0.01")
        primary_norm = self._normalize_account_number(self.account_number)
        run, stats = {}, {}
        for tx in transactions:
            acct = self._normalize_account_number(tx.get('account_number')) or primary_norm
            bal, amt = tx.get('balance'), tx['amount']
            before = run.get(acct)
            if before is not None:
                run[acct] = before + amt
            if bal is not None:
                if before is not None:
                    s = stats.setdefault(acct, {'checked': 0, 'breaks': 0, 'flipped': 0, 'first': None})
                    s['checked'] += 1
                    if abs(run[acct] - bal) > tol:
                        s['breaks'] += 1
                        if abs((before - amt) - bal) <= tol:
                            s['flipped'] += 1
                        if s['first'] is None:
                            s['first'] = f"{tx['date']} '{(tx['description'] or '')[:40]}'"
                run[acct] = bal
        for acct, s in stats.items():
            if s['checked'] >= 3 and s['breaks']:
                msg = (f"Account {acct}: running balance does not tie out on {s['breaks']} of "
                       f"{s['checked']} rows (first at {s['first']})")
                if s['flipped']:
                    msg += f"; {s['flipped']} of those look like flipped debit/credit signs"
                self.review_notes.append(msg)

    # ------------------------------------------------------------------
    # Main AI pipeline
    # ------------------------------------------------------------------
    def _parse_via_openai(self):
        client = self._build_client()

        with pdfplumber.open(self.file_path) as pdf:
            pages = pdf.pages
            if not pages:
                raise ValueError("PDF file contains no pages.")

            page_data = self._prepare_pages(pages)
            vision_count = sum(1 for d in page_data if d['use_vision'])
            self._vision_page_ratio = vision_count / len(page_data)
            _dbg(f"{len(page_data)} pages: {vision_count} need vision, {len(page_data) - vision_count} text")

            self._extract_metadata(client, pages, page_data)

            chunks = self._make_chunks(page_data)
            max_workers = getattr(settings, 'MAX_PDF_WORKERS', 2)
            _dbg(f"Submitting {len(chunks)} chunk(s) covering {len(pages)} pages "
                 f"to ThreadPoolExecutor (max_workers={max_workers})...")

            # (first page number, transactions) — keeps statement order.
            page_results = []
            suspicious_pages = []
            total_start = time.time()
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(self._extract_chunk, client, c): c for c in chunks}
                for future in concurrent.futures.as_completed(futures):
                    chunk = futures[future]
                    res = future.result()
                    page_results.append((chunk[0]['p_idx'] + 1, res['txs']))
                    suspicious_pages.extend(res['suspicious'])
            _dbg(f"All chunks extracted in {time.time() - total_start:.2f}s.")

            # Retry suspicious pages ONCE, sequentially in this thread
            # (pdfplumber rendering is not thread-safe), at higher resolution.
            unresolved = []
            if suspicious_pages:
                retry_dpi = getattr(settings, 'PDF_VISION_RETRY_DPI', 200)
                retry_cap = getattr(settings, 'PDF_MAX_RETRY_PAGES', 12)
                _dbg(f"Retrying {len(set(suspicious_pages))} suspicious page(s) at {retry_dpi} dpi: {sorted(set(suspicious_pages))}")
                for i, n in enumerate(sorted(set(suspicious_pages))):
                    if i >= retry_cap:
                        unresolved.append(n)
                        continue
                    try:
                        b64 = self._render_b64(pages[n - 1], retry_dpi)
                        retry_chunk = [{'p_idx': n - 1, 'p_text': page_data[n - 1]['p_text'],
                                        'use_vision': True, 'b64_str': b64}]
                        res = self._extract_chunk(client, retry_chunk)
                    except Exception as e:
                        _dbg(f"Retry of page {n} failed: {e!r}")
                        unresolved.append(n)
                        continue
                    if res['txs']:
                        page_results.append((n, res['txs']))
                    else:
                        unresolved.append(n)

            page_results.sort(key=lambda r: r[0])
            transactions = [tx for _, txs in page_results for tx in txs]

        if unresolved:
            self.review_notes.append(
                f"Pages {sorted(unresolved)} looked like transaction pages but no transactions could be "
                f"extracted — statement may be incomplete."
            )

        transactions = self._dedupe_checks(transactions)

        # --- Decide whether "no transactions" is legitimate -----------------
        if not transactions:
            beg, end = self.beginning_balance, self.ending_balance
            bank_known = self.bank_name != "Unknown Bank"
            # The account number is required; the bank name is not (a missing
            # bank name alone must not turn a valid dormant statement into a
            # failure). An all-zero, bank-less result is still rejected.
            metadata_ok = (self.account_number != "Unknown Account"
                           and (bank_known or (beg or 0) != 0 or (end or 0) != 0))
            balances_agree = (beg is not None and end is not None and abs(beg - end) <= Decimal("0.01"))
            if metadata_ok and not bank_known:
                self.review_notes.append("Bank name could not be detected for this statement.")
            if unresolved or not metadata_ok or not balances_agree:
                raise StatementParseError(
                    "AI extraction produced 0 transactions and the statement does not look dormant "
                    f"(bank={self.bank_name!r}, account={self.account_number!r}, "
                    f"begin={beg}, end={end}, unresolved pages={sorted(unresolved)})."
                )
            _dbg(f"0 transactions, but metadata is valid and balances agree "
                 f"(bank={self.bank_name}, acct={self.account_number}) — dormant account, not a failure.")

        # Repair misread amounts from the printed running balance, then verify
        # (original statement order, before date-sorting).
        if transactions:
            self._repair_from_running_balances(transactions)
            self._check_running_balances(transactions)

        transactions.sort(key=lambda x: x['date'])  # stable: keeps in-day order
        if transactions:
            if not self.start_date:
                self.start_date = transactions[0]['date']
            if not self.end_date:
                self.end_date = transactions[-1]['date']

        results = self._build_results(transactions)

        for note in self.review_notes:
            print(f"⚠️ [REVIEW] {note}")
        return results

    # ------------------------------------------------------------------
    # Split by account + balances
    # ------------------------------------------------------------------
    def _build_results(self, transactions):
        primary_norm = self._normalize_account_number(self.account_number)
        groups = {}
        for tx in transactions:
            tx.pop('_section', None)
            tx.pop('_check_no', None)
            acct_raw = tx.pop('account_number', None) or self.account_number
            acct_norm = self._normalize_account_number(acct_raw) or primary_norm
            grp = groups.setdefault(acct_norm, {'account_number': acct_raw, 'txs': []})
            grp['txs'].append(tx)

        # Nothing extracted (valid dormant statement) -> still surface the primary account.
        if not groups:
            groups[primary_norm] = {'account_number': self.account_number, 'txs': []}

        results = []
        for acct_norm, grp in groups.items():
            group_txs = sorted(grp['txs'], key=lambda x: x['date'])
            is_primary = (acct_norm == primary_norm)
            detected = next(
                (a for a in getattr(self, 'detected_accounts', [])
                 if self._normalize_account_number(a.get('account_number')) == acct_norm),
                None,
            )

            if is_primary:
                beginning_balance = self.beginning_balance
                ending_balance = self.ending_balance
                start_date, end_date = self.start_date, self.end_date
            else:
                beginning_balance = self._to_decimal(detected.get('beginning_balance')) if detected else None
                ending_balance = self._to_decimal(detected.get('ending_balance')) if detected else None
                start_date = group_txs[0]['date'] if group_txs else self.start_date
                end_date = group_txs[-1]['date'] if group_txs else self.end_date

            if (beginning_balance is None or beginning_balance == Decimal("0.00")) \
                    and group_txs and group_txs[0].get('balance') is not None:
                beginning_balance = group_txs[0]['balance'] - group_txs[0]['amount']
            if (ending_balance is None or ending_balance == Decimal("0.00")) \
                    and group_txs and group_txs[-1].get('balance') is not None:
                ending_balance = group_txs[-1]['balance']
            beginning_balance = beginning_balance if beginning_balance is not None else Decimal("0.00")
            ending_balance = ending_balance if ending_balance is not None else Decimal("0.00")

            # Reconcile: beginning + sum(amounts) must equal ending.
            if group_txs:
                computed = beginning_balance + sum((t['amount'] for t in group_txs), Decimal("0.00"))
                if abs(computed - ending_balance) > Decimal("0.01"):
                    no_bal = sum(1 for t in group_txs if t.get('balance') is None)
                    self.review_notes.append(
                        f"Account {grp['account_number']}: beginning {beginning_balance} + transactions "
                        f"= {computed}, but statement ending balance is {ending_balance} "
                        f"(difference {computed - ending_balance}); {len(group_txs)} rows, "
                        f"{no_bal} without a running balance"
                        + (" (a duplicate listing such as a check table is a common cause)" if no_bal else "")
                    )

            results.append(({
                'bank_name': self.bank_name,
                'account_number': grp['account_number'],
                'account_holder': self.account_holder,
                'currency': self.currency,
                'start_date': start_date,
                'end_date': end_date,
                'beginning_balance': beginning_balance,
                'ending_balance': ending_balance,
            }, group_txs))
        return results

    # ------------------------------------------------------------------
    # Legacy rule-based parser (text PDFs only)
    # ------------------------------------------------------------------
    def _parse_via_legacy_rules(self):
        all_rows = []
        self.statement_year = None

        with pdfplumber.open(self.file_path) as pdf:
            full_text = ""
            for p in pdf.pages:
                full_text += (p.extract_text() or "") + "\n"

            first_text = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
            self._extract_metadata_from_text(full_text, first_page_text=first_text)

            for page in pdf.pages:
                year_match = re.search(r'\b(20\d{2})\b', page.extract_text() or "")
                if year_match:
                    self.statement_year = int(year_match.group(1))
                    break

            for page in pdf.pages:
                for table in page.extract_tables():
                    for row in table:
                        cleaned = [str(cell).strip() if cell is not None else "" for cell in row]
                        if any(cleaned):
                            all_rows.append(cleaned)

        transactions = []
        if all_rows:
            header_idx, mappings = self._find_headers(all_rows)
            if header_idx is not None:
                date_col = mappings['date_col_idx']
                desc_col = mappings['desc_col_idx']
                amount_col = mappings['amount_col_idx']
                debit_col = mappings['debit_col_idx']
                credit_col = mappings['credit_col_idx']
                bal_col = mappings['bal_col_idx']
                max_len = max(x for x in (date_col, desc_col, amount_col, debit_col, credit_col, bal_col)
                              if x is not None) + 1

                for row in all_rows[header_idx + 1:]:
                    if len(row) < max_len:
                        row = row + [""] * (max_len - len(row))
                    parsed_date = self.parse_date(row[date_col], year=self.statement_year)
                    row_desc = row[desc_col]
                    if not parsed_date or not row_desc:
                        continue

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
                        'category': self.derive_category(row_desc, None, amount),
                    })

        if not transactions:
            raise StatementParseError(
                "No transactions could be extracted from this PDF (AI extraction failed and the "
                "rule-based parser found no transaction table)."
            )

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
        # manager.py expects a list of (account_meta, transactions) pairs.
        return [(account_meta, sorted_txs)]

    # ------------------------------------------------------------------
    # Regex metadata fallbacks
    # ------------------------------------------------------------------
    def _extract_metadata_from_text(self, text, first_page_text=None):
        lookup_text = (first_page_text if first_page_text else text) or ""
        bank_keywords = {
            'chase': 'Chase Bank', 'wells fargo': 'Wells Fargo',
            'bank of america': 'Bank of America', 'citi': 'Citibank',
            'hsbc': 'HSBC', 'barclays': 'Barclays',
            'qnb': 'QNB Bank', 'pnc': 'PNC Bank',
            'us bank': 'US Bank', 'capital one': 'Capital One',
            'td bank': 'TD Bank', 'truist': 'Truist',
        }
        low = lookup_text.lower()
        for key, name in bank_keywords.items():
            # word boundaries so 'citi' doesn't match 'city', etc.
            if re.search(r'\b' + re.escape(key) + r'\b', low):
                self.bank_name = name
                break

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