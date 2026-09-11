import pdfplumber
import re
import json
import base64
import os
import io
import urllib.request
import urllib.error
import datetime
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

            # Step 2: Extract Transactions Page-by-Page
            transactions = []
            for p_idx, page in enumerate(pages):
                p_text = page.extract_text() or ""
                
                # Check for garbled/scanned content
                cid_count = len(re.findall(r'\(cid:\d+\)', p_text))
                non_ascii = len([c for c in p_text if ord(c) < 32 or ord(c) > 126])
                is_garbled = len(p_text) > 0 and ((cid_count * 10 + non_ascii) / max(len(p_text), 1) > 0.3)
                
                use_vision = (len(p_text.strip()) < 50) or is_garbled

                tx_instruction = (
                    f"Extract ALL transaction line items from Page {p_idx + 1} of this bank statement.\n"
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

                if use_vision:
                    pil_img = page.to_image(resolution=150).original
                    buf = io.BytesIO()
                    pil_img.save(buf, format="JPEG")
                    b64_str = base64.b64encode(buf.getvalue()).decode("utf-8")
                    
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
                    continue

                for tx in tx_res.get("transactions", []):
                    d_val = parse_iso_date(tx.get("date"))
                    if not d_val:
                        continue

                    desc_text = str(tx.get("description") or "Transaction").strip()
                    amt_val = self.clean_amount(tx.get("amount"))
                    bal_val = self.clean_amount(tx.get("balance")) if tx.get("balance") is not None else None
                    cat_val = self.derive_category(desc_text, tx.get("category"), amt_val)

                    transactions.append({
                        'date': d_val,
                        'description': desc_text,
                        'amount': amt_val,
                        'balance': bal_val,
                        'category': cat_val
                    })

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
            'hsbc': 'HSBC', 'barclays': 'Barclays'
        }
        for key, name in bank_keywords.items():
            if key in lookup_text.lower():
                self.bank_name = name
                break

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

