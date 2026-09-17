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
#         Attempts Azure OpenAI extraction (text-mode or vision-mode).
#         Falls back to legacy deterministic rule-based parsing if Azure OpenAI is unconfigured or unavailable.
#         """
#         try:
#             return self._parse_via_azure_openai()
#         except Exception as ai_err:
#             import logging
#             logging.warning(f"AI Parsing failed or skipped ({ai_err}). Falling back to rule-based parser.")
#             return self._parse_via_legacy_rules()

#     def _parse_via_azure_openai(self):
#         """
#         AI Extraction Engine powered by Azure OpenAI (gpt-4.1-mini).
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

#         api_key = os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("AZURE_OPENAI_KEY") or getattr(settings, "AZURE_OPENAI_API_KEY", None)
#         endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT") or getattr(settings, "AZURE_OPENAI_ENDPOINT", None)
#         deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT") or os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME") or getattr(settings, "AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini")

#         if not api_key or not endpoint:
#             raise ValueError("Azure OpenAI credentials (API key or Endpoint) are missing.")

#         endpoint = endpoint.strip().rstrip("/")
#         url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-02-15-preview"

#         def parse_iso_date(d_str):
#             if not d_str:
#                 return None
#             try:
#                 return datetime.datetime.strptime(str(d_str).strip(), "%Y-%m-%d").date()
#             except Exception:
#                 return self.parse_date(d_str)

#         def make_ai_request(messages_list):
#             request_body = {
#                 "messages": messages_list,
#                 "temperature": 0.0,
#                 "response_format": {"type": "json_object"}
#             }
#             req = urllib.request.Request(
#                 url,
#                 data=json.dumps(request_body).encode("utf-8"),
#                 headers={"Content-Type": "application/json", "api-key": api_key},
#                 method="POST"
#             )
#             with urllib.request.urlopen(req, timeout=60) as response:
#                 resp_data = json.loads(response.read().decode("utf-8"))
#                 return json.loads(resp_data["choices"][0]["message"]["content"])

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
import time
import logging
from decimal import Decimal
from django.conf import settings
from .base import BaseParser

class PDFStatementParser(BaseParser):
    def parse(self):
        """
        AI-First Statement Parsing Pipeline.
        Attempts Azure OpenAI extraction (text-mode or vision-mode).
        Falls back to legacy deterministic rule-based parsing if Azure OpenAI is unconfigured or unavailable.
        """
        try:
            return self._parse_via_azure_openai()
        except Exception as ai_err:
            import logging
            logging.warning(f"AI Parsing failed or skipped ({ai_err}). Falling back to rule-based parser.")
            return self._parse_via_legacy_rules()

    def _parse_via_azure_openai(self):
        """
        AI Extraction Engine powered by Azure OpenAI (gpt-4.1-mini).
        Extracts metadata and transactions page-by-page to handle statements of any length with zero timeouts.
        """
        # Load environment variables if not loaded
        env_path = os.path.join(settings.BASE_DIR, '.env') if hasattr(settings, 'BASE_DIR') else '.env'
        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, val = line.split('=', 1)
                        os.environ[key.strip()] = val.strip()

        api_key = os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("AZURE_OPENAI_KEY") or getattr(settings, "AZURE_OPENAI_API_KEY", None)
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT") or getattr(settings, "AZURE_OPENAI_ENDPOINT", None)
        deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT") or os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME") or getattr(settings, "AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini")

        if not api_key or not endpoint:
            raise ValueError("Azure OpenAI credentials (API key or Endpoint) are missing.")

        endpoint = endpoint.strip().rstrip("/")
        url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-02-15-preview"

        def parse_iso_date(d_str):
            if not d_str:
                return None
            try:
                return datetime.datetime.strptime(str(d_str).strip(), "%Y-%m-%d").date()
            except Exception:
                return self.parse_date(d_str)

        def make_ai_request(messages_list):
            request_body = {
                "messages": messages_list,
                "temperature": 0.0,
                "response_format": {"type": "json_object"}
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(request_body).encode("utf-8"),
                headers={"Content-Type": "application/json", "api-key": api_key},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=60) as response:
                resp_data = json.loads(response.read().decode("utf-8"))
                return json.loads(resp_data["choices"][0]["message"]["content"])

        with pdfplumber.open(self.file_path) as pdf:
            pages = pdf.pages
            if not pages:
                raise ValueError("PDF file contains no pages.")

            # Step 1: Extract Metadata from header pages (pages 1-3)
            doc_header_text = "\n".join([(p.extract_text() or "") for p in pages[:3]])
            self._extract_metadata_from_text(doc_header_text, first_page_text=pages[0].extract_text() or "")

            meta_prompt = (
                "Extract header metadata from these bank statement pages.\n"
                "JSON Schema:\n"
                "{\n"
                "  \"bank_name\": \"Bank Name\",\n"
                "  \"account_number\": \"Account Number\",\n"
                "  \"account_holder\": \"Account Holder Name or null\",\n"
                "  \"start_date\": \"YYYY-MM-DD or null\",\n"
                "  \"end_date\": \"YYYY-MM-DD or null\",\n"
                "  \"beginning_balance\": 1234.56,\n"
                "  \"ending_balance\": 5678.90\n"
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

            if meta_res.get("bank_name") and meta_res.get("bank_name") != "Bank Name":
                self.bank_name = meta_res.get("bank_name")
            if meta_res.get("account_number") and meta_res.get("account_number") != "Account Number":
                self.account_number = str(meta_res.get("account_number")).replace('-', '')
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

            # Pre-extract text and images sequentially to avoid pdfplumber thread-safety crashes (segfaults)
            #
            # SECTION TRACKING: Multi-page bank statements (e.g. Old National) print the
            # "DEPOSITS AND OTHER CREDITS" / "WITHDRAWALS AND OTHER DEBITS" section header
            # only ONCE, at the top of the section - not on every continuation page. Because
            # pages are later sent to the AI extractor in isolation, a mid-section page has no
            # header in its own text and the model has to guess the sign from the description
            # alone, which silently flips many withdrawals to positive "deposits". This must be
            # done as a sequential pass (in page order) before pages are handed to the thread
            # pool, since the section is a running state across the whole document.
            DEPOSIT_HEADER_RE = re.compile(r'DEPOSITS\s+AND\s+OTHER\s+CREDITS', re.I)
            WITHDRAWAL_HEADER_RE = re.compile(r'WITHDRAWALS\s+AND\s+OTHER\s+DEBITS|CHECKS\s+PAID', re.I)

            current_section = 'credit'  # statements conventionally list Deposits before Withdrawals
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

                # Locate every section header on this page, in reading order, to detect
                # transitions. section_at_start = the section still in effect when this
                # page begins (carried forward from prior pages if no header appears here).
                header_matches = []
                for m in DEPOSIT_HEADER_RE.finditer(p_text):
                    header_matches.append((m.start(), 'credit'))
                for m in WITHDRAWAL_HEADER_RE.finditer(p_text):
                    header_matches.append((m.start(), 'debit'))
                header_matches.sort(key=lambda t: t[0])

                section_at_start = current_section
                if header_matches:
                    current_section = header_matches[-1][1]  # section in effect for pages after this one

                page_data_list.append({
                    'p_idx': p_idx,
                    'p_text': p_text,
                    'use_vision': use_vision,
                    'b64_str': b64_str,
                    'section_at_start': section_at_start,
                    'section_transitions': header_matches,
                    'section_is_uniform': len(header_matches) == 0,
                })

            SECTION_LABELS = {
                'credit': 'DEPOSITS AND OTHER CREDITS',
                'debit': 'WITHDRAWALS AND OTHER DEBITS',
            }

            def _process_page(data):
                p_idx = data['p_idx']
                p_text = data['p_text']
                use_vision = data['use_vision']
                b64_str = data['b64_str']
                section_at_start = data['section_at_start']
                section_transitions = data['section_transitions']
                section_is_uniform = data['section_is_uniform']

                start_time = time.time()
                print(f"[DEBUG] Starting AI extraction for page {p_idx + 1} (section={section_at_start}, uniform={section_is_uniform})")

                if section_is_uniform:
                    section_note = (
                        f"SECTION CONTEXT: This entire page falls within the "
                        f"'{SECTION_LABELS[section_at_start]}' section of the statement. "
                        "That section header may have been printed on an earlier page and "
                        "will NOT appear on this page - do not be confused by its absence. "
                        f"Every transaction amount on this page MUST be "
                        f"{'POSITIVE' if section_at_start == 'credit' else 'NEGATIVE'}, regardless of what the description sounds like.\n"
                    )
                else:
                    order = " then ".join(SECTION_LABELS[s] for _, s in section_transitions)
                    section_note = (
                        f"SECTION CONTEXT: This page begins inside the '{SECTION_LABELS[section_at_start]}' "
                        f"section and transitions, in top-to-bottom order, to: {order}. "
                        "Assign each transaction's sign based on which section header it visually "
                        "appears under on this specific page.\n"
                    )

                tx_instruction = (
                    f"Extract ALL transaction line items from Page {p_idx + 1} of this bank statement.\n"
                    f"{section_note}"
                    "CRITICAL SIGN RULE: 'amount' MUST be POSITIVE for deposits/credits/inflows, and NEGATIVE for debits/withdrawals/payments/fees.\n"
                    "JSON Schema:\n"
                    "{\n"
                    "  \"transactions\": [\n"
                    "    {\n"
                    "      \"date\": \"YYYY-MM-DD\",\n"
                    "      \"description\": \"Full description text\",\n"
                    "      \"amount\": -150.00,\n"
                    "      \"balance\": 4500.00,\n"
                    "      \"category\": \"Category name or null\"\n"
                    "    }\n"
                    "  ]\n"
                    "}"
                )

                if use_vision and b64_str:
                    user_msg = [
                        {"type": "text", "text": tx_instruction},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_str}"}}
                    ]
                else:
                    user_msg = f"{tx_instruction}\n\nPAGE {p_idx + 1} TEXT:\n{p_text}"

                try:
                    tx_res = make_ai_request([
                        {"role": "system", "content": "You are a precise financial transaction audit extraction agent."},
                        {"role": "user", "content": user_msg}
                    ])
                except Exception as page_err:
                    return []

                page_transactions = []
                for tx in tx_res.get("transactions", []):
                    d_val = parse_iso_date(tx.get("date"))
                    if not d_val:
                        continue

                    desc_text = str(tx.get("description") or "Transaction").strip()
                    amt_val = self.clean_amount(tx.get("amount"))

                    # Safety net: when the whole page sits inside a single known section,
                    # don't rely on the model to have gotten the sign right from the
                    # description alone - force it from the section we already determined
                    # deterministically from the header positions. This is what actually
                    # fixes the deposit/withdrawal miscategorization bug, independent of
                    # whether the prompt hint above is followed.
                    if section_is_uniform:
                        amt_val = abs(amt_val) if section_at_start == 'credit' else -abs(amt_val)

                    bal_val = self.clean_amount(tx.get("balance")) if tx.get("balance") is not None else None
                    cat_val = self.derive_category(desc_text, tx.get("category"), amt_val)

                    page_transactions.append({
                        'date': d_val,
                        'description': desc_text,
                        'amount': amt_val,
                        'balance': bal_val,
                        'category': cat_val
                    })
                
                elapsed = time.time() - start_time
                print(f"[DEBUG] Page {p_idx + 1} extraction completed in {elapsed:.2f}s (Found {len(page_transactions)} txs)")
                return page_transactions

            # Step 2: Extract Transactions Page-by-Page concurrently
            transactions = []
            total_start_time = time.time()
            max_workers = getattr(settings, 'MAX_PDF_WORKERS', 20)
            print(f"[DEBUG] Submitting {len(pages)} pages to ThreadPoolExecutor (max_workers={max_workers})...")
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(_process_page, data) for data in page_data_list]
                for future in concurrent.futures.as_completed(futures):
                    transactions.extend(future.result())
                    
            total_elapsed = time.time() - total_start_time
            print(f"[DEBUG] All pages extracted in {total_elapsed:.2f}s. Total transactions combined: {len(transactions)}")

        if not transactions:
            raise ValueError("AI Page-by-Page extraction produced 0 transactions.")

        # Sort transactions by date
        transactions.sort(key=lambda x: x['date'])

        if not self.start_date:
            self.start_date = transactions[0]['date']
        if not self.end_date:
            self.end_date = transactions[-1]['date']

        # Fallback balance derivation if ending/beginning balances were 0
        if self.beginning_balance == Decimal("0.00") and transactions[0].get('balance') is not None:
            self.beginning_balance = transactions[0]['balance'] - transactions[0]['amount']
        if self.ending_balance == Decimal("0.00") and transactions[-1].get('balance') is not None:
            self.ending_balance = transactions[-1]['balance']

        account_meta = {
            'bank_name': self.bank_name,
            'account_number': self.account_number,
            'account_holder': self.account_holder,
            'currency': self.currency,
            'start_date': self.start_date,
            'end_date': self.end_date,
            'beginning_balance': self.beginning_balance,
            'ending_balance': self.ending_balance,
        }

        return account_meta, transactions

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

        return account_meta, transactions

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

        # Generic fallback: if the bank isn't one of the ~12 known brands
        # above, try to detect an institution name from the statement
        # header itself instead of leaving it as "Unknown Bank".
        if self.bank_name == "Unknown Bank":
            guessed = self.guess_bank_name_generic(lookup_text)
            if guessed:
                self.bank_name = guessed

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


# # import pdfplumber
# # import re
# # import json
# # import base64
# # import os
# # import io
# # import urllib.request
# # import urllib.error
# # import datetime
# # import concurrent.futures
# # import time
# # import logging
# # from decimal import Decimal
# # from django.conf import settings
# # from .base import BaseParser

# # class PDFStatementParser(BaseParser):
# #     def parse(self):
# #         """
# #         AI-First Statement Parsing Pipeline.
# #         Attempts Azure OpenAI extraction (text-mode or vision-mode).
# #         Falls back to legacy deterministic rule-based parsing if Azure OpenAI is unconfigured or unavailable.
# #         """
# #         try:
# #             return self._parse_via_azure_openai()
# #         except Exception as ai_err:
# #             import logging
# #             logging.warning(f"AI Parsing failed or skipped ({ai_err}). Falling back to rule-based parser.")
# #             return self._parse_via_legacy_rules()

# #     def _parse_via_azure_openai(self):
# #         """
# #         AI Extraction Engine powered by Azure OpenAI (gpt-4.1-mini).
# #         Extracts metadata and transactions page-by-page to handle statements of any length with zero timeouts.
# #         """
# #         # Load environment variables if not loaded
# #         env_path = os.path.join(settings.BASE_DIR, '.env') if hasattr(settings, 'BASE_DIR') else '.env'
# #         if os.path.exists(env_path):
# #             with open(env_path, 'r') as f:
# #                 for line in f:
# #                     line = line.strip()
# #                     if line and not line.startswith('#') and '=' in line:
# #                         key, val = line.split('=', 1)
# #                         os.environ[key.strip()] = val.strip()

# #         api_key = os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("AZURE_OPENAI_KEY") or getattr(settings, "AZURE_OPENAI_API_KEY", None)
# #         endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT") or getattr(settings, "AZURE_OPENAI_ENDPOINT", None)
# #         deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT") or os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME") or getattr(settings, "AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini")

# #         if not api_key or not endpoint:
# #             raise ValueError("Azure OpenAI credentials (API key or Endpoint) are missing.")

# #         endpoint = endpoint.strip().rstrip("/")
# #         url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-02-15-preview"

# #         def parse_iso_date(d_str):
# #             if not d_str:
# #                 return None
# #             try:
# #                 return datetime.datetime.strptime(str(d_str).strip(), "%Y-%m-%d").date()
# #             except Exception:
# #                 return self.parse_date(d_str)

# #         def make_ai_request(messages_list):
# #             request_body = {
# #                 "messages": messages_list,
# #                 "temperature": 0.0,
# #                 "response_format": {"type": "json_object"}
# #             }
# #             req = urllib.request.Request(
# #                 url,
# #                 data=json.dumps(request_body).encode("utf-8"),
# #                 headers={"Content-Type": "application/json", "api-key": api_key},
# #                 method="POST"
# #             )
# #             with urllib.request.urlopen(req, timeout=60) as response:
# #                 resp_data = json.loads(response.read().decode("utf-8"))
# #                 return json.loads(resp_data["choices"][0]["message"]["content"])

# #         with pdfplumber.open(self.file_path) as pdf:
# #             pages = pdf.pages
# #             if not pages:
# #                 raise ValueError("PDF file contains no pages.")

# #             # Step 1: Extract Metadata from header pages (pages 1-3)
# #             doc_header_text = "\n".join([(p.extract_text() or "") for p in pages[:3]])
# #             self._extract_metadata_from_text(doc_header_text, first_page_text=pages[0].extract_text() or "")

# #             meta_prompt = (
# #                 "Extract header metadata from these bank statement pages.\n"
# #                 "JSON Schema:\n"
# #                 "{\n"
# #                 "  \"bank_name\": \"Bank Name\",\n"
# #                 "  \"account_number\": \"Account Number\",\n"
# #                 "  \"account_holder\": \"Account Holder Name or null\",\n"
# #                 "  \"start_date\": \"YYYY-MM-DD or null\",\n"
# #                 "  \"end_date\": \"YYYY-MM-DD or null\",\n"
# #                 "  \"beginning_balance\": 1234.56,\n"
# #                 "  \"ending_balance\": 5678.90\n"
# #                 "}\n\n"
# #                 f"HEADER TEXT:\n{doc_header_text[:4000]}"
# #             )
            
# #             try:
# #                 meta_res = make_ai_request([
# #                     {"role": "system", "content": "You are an expert bank statement metadata extraction agent."},
# #                     {"role": "user", "content": meta_prompt}
# #                 ])
# #             except Exception as e:
# #                 meta_res = {}

# #             if meta_res.get("bank_name") and meta_res.get("bank_name") != "Bank Name":
# #                 self.bank_name = meta_res.get("bank_name")
# #             if meta_res.get("account_number") and meta_res.get("account_number") != "Account Number":
# #                 self.account_number = str(meta_res.get("account_number")).replace('-', '')
# #             if meta_res.get("account_holder"):
# #                 self.account_holder = meta_res.get("account_holder")
            
# #             beg_b = meta_res.get("beginning_balance")
# #             if beg_b is not None and self.beginning_balance is None:
# #                 self.beginning_balance = Decimal(str(beg_b))
# #             elif self.beginning_balance is None:
# #                 self.beginning_balance = Decimal("0.00")
            
# #             end_b = meta_res.get("ending_balance")
# #             if end_b is not None and self.ending_balance is None:
# #                 self.ending_balance = Decimal(str(end_b))
# #             elif self.ending_balance is None:
# #                 self.ending_balance = Decimal("0.00")

# #             if not self.start_date:
# #                 self.start_date = parse_iso_date(meta_res.get("start_date"))
# #             if not self.end_date:
# #                 self.end_date = parse_iso_date(meta_res.get("end_date"))

# #             # Pre-extract text and images sequentially to avoid pdfplumber thread-safety crashes (segfaults)
# #             page_data_list = []
# #             for p_idx, page in enumerate(pages):
# #                 p_text = page.extract_text() or ""
                
# #                 # Check for garbled/scanned content
# #                 cid_count = len(re.findall(r'\(cid:\d+\)', p_text))
# #                 non_ascii = len([c for c in p_text if ord(c) < 32 or ord(c) > 126])
# #                 is_garbled = len(p_text) > 0 and ((cid_count * 10 + non_ascii) / max(len(p_text), 1) > 0.3)
                
# #                 use_vision = (len(p_text.strip()) < 50) or is_garbled
# #                 b64_str = None
                
# #                 if use_vision:
# #                     try:
# #                         pil_img = page.to_image(resolution=150).original
# #                         buf = io.BytesIO()
# #                         pil_img.save(buf, format="JPEG")
# #                         b64_str = base64.b64encode(buf.getvalue()).decode("utf-8")
# #                     except Exception:
# #                         pass # Ignore image extraction errors, fallback to text if possible
                
# #                 page_data_list.append({
# #                     'p_idx': p_idx,
# #                     'p_text': p_text,
# #                     'use_vision': use_vision,
# #                     'b64_str': b64_str
# #                 })

# #             def _process_page(data):
# #                 p_idx = data['p_idx']
# #                 p_text = data['p_text']
# #                 use_vision = data['use_vision']
# #                 b64_str = data['b64_str']
                
# #                 start_time = time.time()
# #                 print(f"[DEBUG] Starting AI extraction for page {p_idx + 1}")

# #                 tx_instruction = (
# #                     f"Extract ALL transaction line items from Page {p_idx + 1} of this bank statement.\n"
# #                     "CRITICAL SIGN RULE: 'amount' MUST be POSITIVE for deposits/credits/inflows, and NEGATIVE for debits/withdrawals/payments/fees.\n"
# #                     "JSON Schema:\n"
# #                     "{\n"
# #                     "  \"transactions\": [\n"
# #                     "    {\n"
# #                     "      \"date\": \"YYYY-MM-DD\",\n"
# #                     "      \"description\": \"Full description text\",\n"
# #                     "      \"amount\": -150.00,\n"
# #                     "      \"balance\": 4500.00,\n"
# #                     "      \"category\": \"Category name or null\"\n"
# #                     "    }\n"
# #                     "  ]\n"
# #                     "}"
# #                 )

# #                 if use_vision and b64_str:
# #                     user_msg = [
# #                         {"type": "text", "text": tx_instruction},
# #                         {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_str}"}}
# #                     ]
# #                 else:
# #                     user_msg = f"{tx_instruction}\n\nPAGE {p_idx + 1} TEXT:\n{p_text}"

# #                 try:
# #                     tx_res = make_ai_request([
# #                         {"role": "system", "content": "You are a precise financial transaction audit extraction agent."},
# #                         {"role": "user", "content": user_msg}
# #                     ])
# #                 except Exception as page_err:
# #                     return []

# #                 page_transactions = []
# #                 for tx in tx_res.get("transactions", []):
# #                     d_val = parse_iso_date(tx.get("date"))
# #                     if not d_val:
# #                         continue

# #                     desc_text = str(tx.get("description") or "Transaction").strip()
# #                     amt_val = self.clean_amount(tx.get("amount"))
# #                     bal_val = self.clean_amount(tx.get("balance")) if tx.get("balance") is not None else None
# #                     cat_val = self.derive_category(desc_text, tx.get("category"), amt_val)

# #                     page_transactions.append({
# #                         'date': d_val,
# #                         'description': desc_text,
# #                         'amount': amt_val,
# #                         'balance': bal_val,
# #                         'category': cat_val
# #                     })
                
# #                 elapsed = time.time() - start_time
# #                 print(f"[DEBUG] Page {p_idx + 1} extraction completed in {elapsed:.2f}s (Found {len(page_transactions)} txs)")
# #                 return page_transactions

# #             # Step 2: Extract Transactions Page-by-Page concurrently
# #             transactions = []
# #             total_start_time = time.time()
# #             max_workers = getattr(settings, 'MAX_PDF_WORKERS', 20)
# #             print(f"[DEBUG] Submitting {len(pages)} pages to ThreadPoolExecutor (max_workers={max_workers})...")
            
# #             with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
# #                 futures = [executor.submit(_process_page, data) for data in page_data_list]
# #                 for future in concurrent.futures.as_completed(futures):
# #                     transactions.extend(future.result())
                    
# #             total_elapsed = time.time() - total_start_time
# #             print(f"[DEBUG] All pages extracted in {total_elapsed:.2f}s. Total transactions combined: {len(transactions)}")

# #         if not transactions:
# #             raise ValueError("AI Page-by-Page extraction produced 0 transactions.")

# #         # Sort transactions by date
# #         transactions.sort(key=lambda x: x['date'])

# #         if not self.start_date:
# #             self.start_date = transactions[0]['date']
# #         if not self.end_date:
# #             self.end_date = transactions[-1]['date']

# #         # Fallback balance derivation if ending/beginning balances were 0
# #         if self.beginning_balance == Decimal("0.00") and transactions[0].get('balance') is not None:
# #             self.beginning_balance = transactions[0]['balance'] - transactions[0]['amount']
# #         if self.ending_balance == Decimal("0.00") and transactions[-1].get('balance') is not None:
# #             self.ending_balance = transactions[-1]['balance']

# #         account_meta = {
# #             'bank_name': self.bank_name,
# #             'account_number': self.account_number,
# #             'account_holder': self.account_holder,
# #             'currency': self.currency,
# #             'start_date': self.start_date,
# #             'end_date': self.end_date,
# #             'beginning_balance': self.beginning_balance,
# #             'ending_balance': self.ending_balance,
# #         }

# #         return account_meta, transactions

# #     def _parse_via_legacy_rules(self):
# #         """
# #         Legacy deterministic parser fallback.
# #         """
# #         all_rows = []
# #         full_text_first_page = ""
# #         self.statement_year = None

# #         with pdfplumber.open(self.file_path) as pdf:
# #             full_text = ""
# #             for p in pdf.pages:
# #                 full_text += (p.extract_text() or "") + "\n"

# #             if len(pdf.pages) > 0:
# #                 first_page = pdf.pages[0]
# #                 full_text_first_page = first_page.extract_text() or ""

# #             self._extract_metadata_from_text(full_text, first_page_text=full_text_first_page)

# #             for page in pdf.pages:
# #                 page_text = page.extract_text() or ""
# #                 year_match = re.search(r'\b(20\d{2})\b', page_text)
# #                 if year_match:
# #                     self.statement_year = int(year_match.group(1))
# #                     break

# #             for page in pdf.pages:
# #                 tables = page.extract_tables()
# #                 for table in tables:
# #                     for row in table:
# #                         cleaned_row = [str(cell).strip() if cell is not None else "" for cell in row]
# #                         if any(cleaned_row):
# #                             all_rows.append(cleaned_row)

# #         transactions = []
# #         if all_rows:
# #             header_idx, mappings = self._find_headers(all_rows)
# #             if header_idx is not None:
# #                 raw_data_rows = all_rows[header_idx + 1:]
# #                 date_col = mappings['date_col_idx']
# #                 desc_col = mappings['desc_col_idx']
# #                 amount_col = mappings['amount_col_idx']
# #                 debit_col = mappings['debit_col_idx']
# #                 credit_col = mappings['credit_col_idx']
# #                 bal_col = mappings['bal_col_idx']

# #                 for row in raw_data_rows:
# #                     max_len = max(x for x in (date_col, desc_col, amount_col, debit_col, credit_col, bal_col) if x is not None) + 1
# #                     if len(row) < max_len:
# #                         row = row + [""] * (max_len - len(row))

# #                     row_date_str = row[date_col]
# #                     parsed_date = self.parse_date(row_date_str, year=self.statement_year)
# #                     row_desc = row[desc_col]

# #                     if not parsed_date or not row_desc:
# #                         continue

# #                     amount = Decimal('0.00')
# #                     if amount_col is not None:
# #                         amount = self.clean_amount(row[amount_col])
# #                     else:
# #                         debit_val = self.clean_amount(row[debit_col]) if debit_col is not None else Decimal('0.00')
# #                         credit_val = self.clean_amount(row[credit_col]) if credit_col is not None else Decimal('0.00')
# #                         amount = credit_val - abs(debit_val)

# #                     if amount == 0:
# #                         continue

# #                     balance = self.clean_amount(row[bal_col]) if bal_col is not None and row[bal_col] else None

# #                     transactions.append({
# #                         'date': parsed_date,
# #                         'description': row_desc,
# #                         'amount': amount,
# #                         'balance': balance,
# #                         'category': self.derive_category(row_desc, None, amount)
# #                     })

# #         if transactions:
# #             sorted_txs = sorted(transactions, key=lambda x: x['date'])
# #             self.start_date = sorted_txs[0]['date']
# #             self.end_date = sorted_txs[-1]['date']

# #         account_meta = {
# #             'bank_name': self.bank_name,
# #             'account_number': self.account_number,
# #             'account_holder': self.account_holder,
# #             'currency': self.currency,
# #             'start_date': self.start_date,
# #             'end_date': self.end_date,
# #             'beginning_balance': self.beginning_balance or Decimal('0.00'),
# #             'ending_balance': self.ending_balance or Decimal('0.00'),
# #         }

# #         return account_meta, transactions

# #     def _extract_metadata_from_text(self, text, first_page_text=None):
# #         lookup_text = first_page_text if first_page_text else text
# #         bank_keywords = {
# #             'chase': 'Chase Bank', 'wells fargo': 'Wells Fargo',
# #             'bank of america': 'Bank of America', 'citi': 'Citibank',
# #             'hsbc': 'HSBC', 'barclays': 'Barclays',
# #             'qnb': 'QNB Bank', 'pnc': 'PNC Bank',
# #             'us bank': 'US Bank', 'capital one': 'Capital One',
# #             'td bank': 'TD Bank', 'truist': 'Truist'
# #         }
# #         for key, name in bank_keywords.items():
# #             if key in lookup_text.lower():
# #                 self.bank_name = name
# #                 break
                
# #         # Fallback account number extraction if AI misses it
# #         acc_match = re.search(r'\b(?:account|acc|a/c|no\.?)\b.*?\b(\d[0-9-]{3,17})\b', lookup_text, re.IGNORECASE)
# #         if acc_match and self.account_number == "Unknown Account":
# #             self.account_number = acc_match.group(1).strip()

# #     def _find_headers(self, all_rows):
# #         date_syn = {'date', 'transaction date', 'tx date', 'posted date'}
# #         desc_syn = {'description', 'details', 'particulars', 'narrative'}
# #         amount_syn = {'amount', 'transaction amount'}
# #         debit_syn = {'debit', 'withdrawals', 'payments'}
# #         credit_syn = {'credit', 'deposits', 'receipts'}
# #         bal_syn = {'balance', 'running balance'}

# #         for r_idx in range(min(50, len(all_rows))):
# #             row_cells = [cell.strip().lower() for cell in all_rows[r_idx]]
# #             mappings = {
# #                 'date_col_idx': next((i for i, c in enumerate(row_cells) if any(s in c for s in date_syn)), None),
# #                 'desc_col_idx': next((i for i, c in enumerate(row_cells) if any(s in c for s in desc_syn)), None),
# #                 'amount_col_idx': next((i for i, c in enumerate(row_cells) if any(s in c for s in amount_syn)), None),
# #                 'debit_col_idx': next((i for i, c in enumerate(row_cells) if any(s in c for s in debit_syn)), None),
# #                 'credit_col_idx': next((i for i, c in enumerate(row_cells) if any(s in c for s in credit_syn)), None),
# #                 'bal_col_idx': next((i for i, c in enumerate(row_cells) if any(s in c for s in bal_syn)), None),
# #             }
# #             if mappings['date_col_idx'] is not None and mappings['desc_col_idx'] is not None:
# #                 return r_idx, mappings
# #         return None, None



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
#         Attempts Azure OpenAI extraction (text-mode or vision-mode).
#         Falls back to legacy deterministic rule-based parsing if Azure OpenAI is unconfigured or unavailable.
#         """
#         try:
#             return self._parse_via_azure_openai()
#         except Exception as ai_err:
#             import logging
#             logging.warning(f"AI Parsing failed or skipped ({ai_err}). Falling back to rule-based parser.")
#             return self._parse_via_legacy_rules()

#     def _parse_via_azure_openai(self):
#         """
#         AI Extraction Engine powered by Azure OpenAI (gpt-4.1-mini).
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

#         api_key = os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("AZURE_OPENAI_KEY") or getattr(settings, "AZURE_OPENAI_API_KEY", None)
#         endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT") or getattr(settings, "AZURE_OPENAI_ENDPOINT", None)
#         deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT") or os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME") or getattr(settings, "AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini")

#         if not api_key or not endpoint:
#             raise ValueError("Azure OpenAI credentials (API key or Endpoint) are missing.")

#         endpoint = endpoint.strip().rstrip("/")
#         url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-02-15-preview"

#         def parse_iso_date(d_str):
#             if not d_str:
#                 return None
#             try:
#                 return datetime.datetime.strptime(str(d_str).strip(), "%Y-%m-%d").date()
#             except Exception:
#                 return self.parse_date(d_str)

#         def make_ai_request(messages_list):
#             request_body = {
#                 "messages": messages_list,
#                 "temperature": 0.0,
#                 "response_format": {"type": "json_object"}
#             }
#             req = urllib.request.Request(
#                 url,
#                 data=json.dumps(request_body).encode("utf-8"),
#                 headers={"Content-Type": "application/json", "api-key": api_key},
#                 method="POST"
#             )
#             with urllib.request.urlopen(req, timeout=60) as response:
#                 resp_data = json.loads(response.read().decode("utf-8"))
#                 return json.loads(resp_data["choices"][0]["message"]["content"])

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
#                 "  \"ending_balance\": 5678.90,\n"
#                 "  \"total_deposits_credits\": 1234.56,\n"
#                 "  \"total_withdrawals_debits\": 1234.56\n"
#                 "}\n"
#                 "total_deposits_credits and total_withdrawals_debits come from the "
#                 "statement's own Account Summary block (e.g. 'Deposits/Credits' and "
#                 "'Withdrawals/Debits' totals) - both as POSITIVE numbers, or null if not printed.\n\n"
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

#             # Stated Account Summary totals, used purely as a post-extraction
#             # reconciliation check below (not fed back into extraction). This
#             # is the bank-agnostic backstop: it catches a sign/miscount bug on
#             # ANY statement format, even one whose section headers don't match
#             # DEPOSIT_HEADER_RE / WITHDRAWAL_HEADER_RE at all.
#             stated_total_credits = meta_res.get("total_deposits_credits")
#             stated_total_debits = meta_res.get("total_withdrawals_debits")
#             try:
#                 stated_total_credits = Decimal(str(stated_total_credits)) if stated_total_credits is not None else None
#             except Exception:
#                 stated_total_credits = None
#             try:
#                 stated_total_debits = Decimal(str(stated_total_debits)) if stated_total_debits is not None else None
#             except Exception:
#                 stated_total_debits = None

#             # Pre-extract text and images sequentially to avoid pdfplumber thread-safety crashes (segfaults)
#             #
#             # SECTION TRACKING: Multi-page bank statements (e.g. Old National) print the
#             # "DEPOSITS AND OTHER CREDITS" / "WITHDRAWALS AND OTHER DEBITS" section header
#             # only ONCE, at the top of the section - not on every continuation page. Because
#             # pages are later sent to the AI extractor in isolation, a mid-section page has no
#             # header in its own text and the model has to guess the sign from the description
#             # alone, which silently flips many withdrawals to positive "deposits". This must be
#             # done as a sequential pass (in page order) before pages are handed to the thread
#             # pool, since the section is a running state across the whole document.
#             # Broadened, bank-agnostic header vocabulary. Different banks label
#             # these sections differently (Old National: "DEPOSITS AND OTHER
#             # CREDITS" / "WITHDRAWALS AND OTHER DEBITS"; others: "DEPOSITS",
#             # "CREDITS", "CHECKS PAID", "ELECTRONIC WITHDRAWALS", "ATM/DEBIT
#             # CARD WITHDRAWALS", "OTHER DEBITS", etc). Matching is deliberately
#             # loose (word-boundary keyword, not exact phrase) so this isn't
#             # tied to one issuer's wording - but see the reconciliation check
#             # below, which is the real bank-agnostic backstop: it doesn't
#             # depend on these patterns matching at all.
#             DEPOSIT_HEADER_RE = re.compile(
#                 r'\bDEPOSITS?\s+(AND\s+OTHER\s+)?CREDITS?\b|\bDEPOSITS?\s+&\s+CREDITS?\b',
#                 re.I
#             )
#             WITHDRAWAL_HEADER_RE = re.compile(
#                 r'\bWITHDRAWALS?\s+(AND\s+OTHER\s+)?DEBITS?\b'
#                 r'|\bCHECKS?\s+PAID\b'
#                 r'|\bELECTRONIC\s+WITHDRAWALS?\b'
#                 r'|\b(ATM|DEBIT\s+CARD)\s+WITHDRAWALS?\b'
#                 r'|\bOTHER\s+DEBITS?\b'
#                 r'|\bWITHDRAWALS?\s+&\s+DEBITS?\b',
#                 re.I
#             )

#             current_section = 'credit'  # statements conventionally list Deposits before Withdrawals
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

#                 # Locate every section header on this page, in reading order, to detect
#                 # transitions. section_at_start = the section still in effect when this
#                 # page begins (carried forward from prior pages if no header appears here).
#                 header_matches = []
#                 for m in DEPOSIT_HEADER_RE.finditer(p_text):
#                     header_matches.append((m.start(), 'credit'))
#                 for m in WITHDRAWAL_HEADER_RE.finditer(p_text):
#                     header_matches.append((m.start(), 'debit'))
#                 header_matches.sort(key=lambda t: t[0])

#                 section_at_start = current_section
#                 if header_matches:
#                     current_section = header_matches[-1][1]  # section in effect for pages after this one

#                 # A page is only a genuine mid-page TRANSITION if headers from more than
#                 # one distinct section appear on it. Many banks (Old National included)
#                 # reprint the *same* section's header as "... (continued)" on every
#                 # continuation page - that must NOT be treated as a transition, or the
#                 # hard sign-enforcement safety net below gets disabled on nearly every
#                 # page, which defeats the point of tracking sections at all.
#                 distinct_sections_on_page = {s for _, s in header_matches}
#                 is_transition_page = len(distinct_sections_on_page) > 1

#                 page_data_list.append({
#                     'p_idx': p_idx,
#                     'p_text': p_text,
#                     'use_vision': use_vision,
#                     'b64_str': b64_str,
#                     'section_at_start': section_at_start,
#                     'section_transitions': header_matches,
#                     'section_is_uniform': not is_transition_page,
#                 })

#             SECTION_LABELS = {
#                 'credit': 'DEPOSITS AND OTHER CREDITS',
#                 'debit': 'WITHDRAWALS AND OTHER DEBITS',
#             }

#             def _process_page(data):
#                 p_idx = data['p_idx']
#                 p_text = data['p_text']
#                 use_vision = data['use_vision']
#                 b64_str = data['b64_str']
#                 section_at_start = data['section_at_start']
#                 section_transitions = data['section_transitions']
#                 section_is_uniform = data['section_is_uniform']

#                 start_time = time.time()
#                 print(f"[DEBUG] Starting AI extraction for page {p_idx + 1} (section={section_at_start}, uniform={section_is_uniform})")

#                 if section_is_uniform:
#                     section_note = (
#                         f"SECTION CONTEXT: This entire page falls within the "
#                         f"'{SECTION_LABELS[section_at_start]}' section of the statement. "
#                         "That section header may have been printed on an earlier page and "
#                         "will NOT appear on this page - do not be confused by its absence. "
#                         f"Every transaction amount on this page MUST be "
#                         f"{'POSITIVE' if section_at_start == 'credit' else 'NEGATIVE'}, regardless of what the description sounds like.\n"
#                     )
#                 else:
#                     order = " then ".join(SECTION_LABELS[s] for _, s in section_transitions)
#                     section_note = (
#                         f"SECTION CONTEXT: This page begins inside the '{SECTION_LABELS[section_at_start]}' "
#                         f"section and transitions, in top-to-bottom order, to: {order}. "
#                         "Assign each transaction's sign based on which section header it visually "
#                         "appears under on this specific page.\n"
#                     )

#                 tx_instruction = (
#                     f"Extract ALL transaction line items from Page {p_idx + 1} of this bank statement.\n"
#                     f"{section_note}"
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

#                 MAX_PAGE_RETRIES = 2
#                 tx_res = None
#                 last_err = None
#                 for attempt in range(1, MAX_PAGE_RETRIES + 1):
#                     try:
#                         tx_res = make_ai_request([
#                             {"role": "system", "content": "You are a precise financial transaction audit extraction agent."},
#                             {"role": "user", "content": user_msg}
#                         ])
#                         break
#                     except Exception as page_err:
#                         last_err = page_err
#                         print(f"[WARNING] Page {p_idx + 1} extraction attempt {attempt}/{MAX_PAGE_RETRIES} failed: {page_err!r}")
#                         logging.warning(f"Page {p_idx + 1} extraction attempt {attempt}/{MAX_PAGE_RETRIES} failed: {page_err!r}")

#                 if tx_res is None:
#                     # Every retry failed. This page is now dropped from the ledger -
#                     # log it loudly rather than silently returning [], since a missing
#                     # page understates both deposits and withdrawals and will not
#                     # produce the sign-flip pattern the reconciliation check above is
#                     # tuned to describe.
#                     fail_msg = (
#                         f"[PAGE EXTRACTION FAILED] {self.file_path} page {p_idx + 1}: "
#                         f"gave up after {MAX_PAGE_RETRIES} attempts ({last_err!r}). "
#                         "This page's transactions are MISSING from the ledger."
#                     )
#                     print(fail_msg)
#                     logging.error(fail_msg)
#                     self.failed_pages.append(p_idx + 1)
#                     return []

#                 page_transactions = []
#                 for tx in tx_res.get("transactions", []):
#                     d_val = parse_iso_date(tx.get("date"))
#                     if not d_val:
#                         continue

#                     desc_text = str(tx.get("description") or "Transaction").strip()
#                     amt_val = self.clean_amount(tx.get("amount"))

#                     # Safety net: when the whole page sits inside a single known section,
#                     # don't rely on the model to have gotten the sign right from the
#                     # description alone - force it from the section we already determined
#                     # deterministically from the header positions. This is what actually
#                     # fixes the deposit/withdrawal miscategorization bug, independent of
#                     # whether the prompt hint above is followed.
#                     if section_is_uniform:
#                         amt_val = abs(amt_val) if section_at_start == 'credit' else -abs(amt_val)

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
#             self.failed_pages = []
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

#         # --- Bank-agnostic reconciliation check ---
#         # Compare what we actually extracted against the statement's own
#         # printed Account Summary totals. This does NOT depend on the
#         # DEPOSIT_HEADER_RE / WITHDRAWAL_HEADER_RE patterns matching this
#         # statement's wording, so it still catches a sign/miscategorization
#         # bug on bank formats the header regex doesn't recognize at all.
#         extracted_credits = sum((t['amount'] for t in transactions if t['amount'] > 0), Decimal('0.00'))
#         extracted_debits = sum((-t['amount'] for t in transactions if t['amount'] < 0), Decimal('0.00'))
#         TOLERANCE = Decimal('1.00')  # allow for rounding / unposted items
#         recon_notes = []
#         if stated_total_credits is not None and abs(extracted_credits - stated_total_credits) > TOLERANCE:
#             gap = extracted_credits - stated_total_credits
#             recon_notes.append(
#                 f"DEPOSITS mismatch: extracted ${extracted_credits} vs statement's printed "
#                 f"${stated_total_credits} (off by ${gap})"
#             )
#         if stated_total_debits is not None and abs(extracted_debits - stated_total_debits) > TOLERANCE:
#             gap = extracted_debits - stated_total_debits
#             recon_notes.append(
#                 f"WITHDRAWALS mismatch: extracted ${extracted_debits} vs statement's printed "
#                 f"${stated_total_debits} (off by ${gap})"
#             )
#         if recon_notes or self.failed_pages:
#             failed_note = f" Pages that failed extraction entirely: {self.failed_pages}." if self.failed_pages else ""
#             warning_msg = (
#                 f"[RECONCILIATION WARNING] {self.file_path}: extraction does not match "
#                 f"this statement's printed Account Summary." + failed_note +
#                 (" Likely sign/section miscategorization on this statement's layout. " if recon_notes else "") +
#                 "; ".join(recon_notes)
#             )
#             print(warning_msg)
#             logging.warning(warning_msg)
#             # Surface it on the parser instance so callers (services.py / the
#             # dashboard) can flag the report instead of silently trusting it.
#             self.reconciliation_warning = warning_msg
#         else:
#             self.reconciliation_warning = None

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