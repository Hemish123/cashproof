import pdfplumber
import re
from decimal import Decimal
from .base import BaseParser

class PDFStatementParser(BaseParser):
    def parse(self):
        all_rows = []
        full_text_first_page = ""
        self.statement_year = None

        with pdfplumber.open(self.file_path) as pdf:
            # 1. Extract metadata from all pages text to guarantee finding summary headers
            full_text = ""
            for p in pdf.pages:
                full_text += (p.extract_text() or "") + "\n"

            if len(pdf.pages) > 0:
                first_page = pdf.pages[0]
                full_text_first_page = first_page.extract_text() or ""

            self._extract_metadata_from_text(full_text, first_page_text=full_text_first_page)

            # Check if the text is empty or corrupted (CID fonts / garbled placeholders)
            is_garbled = False
            if full_text_first_page:
                cid_count = len(re.findall(r'\(cid:\d+\)', full_text_first_page))
                non_ascii_count = len([c for c in full_text_first_page if ord(c) < 32 or ord(c) > 126])
                if len(full_text_first_page) > 0 and (cid_count * 10 + non_ascii_count) / len(full_text_first_page) > 0.3:
                    is_garbled = True

            if not full_text_first_page or is_garbled:
                return self._parse_via_azure_openai()

            # Scan text of pages to find a 4-digit statement year
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                year_match = re.search(r'\b(20\d{2})\b', page_text)
                if year_match:
                    self.statement_year = int(year_match.group(1))
                    break

            # 2. Extract tables page-by-page
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        cleaned_row = [str(cell).strip() if cell is not None else "" for cell in row]
                        row_str = " ".join(cleaned_row).lower()
                        if 'checks paid' in row_str or 'check date amount' in row_str or 'type of charge' in row_str:
                            continue
                        if any(cleaned_row):
                            all_rows.append(cleaned_row)

        transactions = []

        parsed_via_table = False
        if all_rows:
            # 3. Find header row using synonym heuristic
            header_idx, mappings = self._find_headers(all_rows)
            
            if header_idx is not None:
                parsed_via_table = True
                # 4. Extract data rows and stitch wrapped lines
                raw_data_rows = all_rows[header_idx + 1:]
                current_tx = None

                date_col = mappings['date_col_idx']
                desc_col = mappings['desc_col_idx']
                amount_col = mappings['amount_col_idx']
                debit_col = mappings['debit_col_idx']
                credit_col = mappings['credit_col_idx']
                bal_col = mappings['bal_col_idx']

                for row in raw_data_rows:
                    # Ensure row length matches header requirements
                    max_len = max(
                        x for x in (date_col, desc_col, amount_col, debit_col, credit_col, bal_col) if x is not None
                    ) + 1
                    if len(row) < max_len:
                        row = row + [""] * (max_len - len(row))

                    row_date_str = row[date_col]
                    parsed_date = self.parse_date(row_date_str, year=self.statement_year)
                    row_desc = row[desc_col]
                    
                    # Continuation line detection
                    is_continuation = False
                    if current_tx and not parsed_date:
                        amount_empty = True
                        if amount_col is not None and row[amount_col]:
                            amount_empty = False
                        if debit_col is not None and row[debit_col]:
                            amount_empty = False
                        if credit_col is not None and row[credit_col]:
                            amount_empty = False
                        
                        if amount_empty and row_desc:
                            is_continuation = True

                    if is_continuation:
                        current_tx['description'] += " " + row_desc
                        if bal_col is not None and row[bal_col]:
                            bal_val = self.clean_amount(row[bal_col])
                            if bal_val:
                                current_tx['balance'] = bal_val
                    else:
                        if not parsed_date:
                            continue

                        if not row_desc or row_desc.lower() in ('total', 'subtotal', 'balance forward', 'summary', 'beginning balance', 'ending balance'):
                            continue

                        # Calculate amount
                        amount = Decimal('0.00')
                        if amount_col is not None:
                            amount = self.clean_amount(row[amount_col])
                        else:
                            debit_val = Decimal('0.00')
                            credit_val = Decimal('0.00')
                            if debit_col is not None:
                                debit_val = self.clean_amount(row[debit_val])
                                if debit_val > 0:
                                    debit_val = -debit_val
                            if credit_col is not None:
                                credit_val = self.clean_amount(row[credit_col])
                                if credit_val < 0:
                                    credit_val = abs(credit_val)
                            amount = debit_val + credit_val

                        if amount == 0:
                            continue

                        balance = None
                        if bal_col is not None and row[bal_col]:
                            balance = self.clean_amount(row[bal_col])

                        current_tx = {
                            'date': parsed_date,
                            'description': row_desc,
                            'amount': amount,
                            'balance': balance,
                            'category': 'Uncategorized'
                        }
                        transactions.append(current_tx)
        
        if not parsed_via_table or len(transactions) < 2:
            transactions = []
            parsed_via_table = False
            # Fallback: Parse line-by-line using regular expression matching (for borderless layouts)
            date_start_re = re.compile(r'^(\d{1,2}[-/.]\d{1,2}(?:[-/.]\d{2,4})?|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-zA-Z]*(?:\s*,\s*\d{2,4})?|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-zA-Z]*\s+\d{1,2}(?:\s*,\s*\d{2,4})?)\b', re.IGNORECASE)
            number_re = re.compile(r'(-?\$?\d[\d,]*\.\d{2}(?:\s*-)?|\(\$?\d[\d,]*\.\d{2}\))')

            with pdfplumber.open(self.file_path) as pdf:
                in_activity_section = False
                current_tx = None

                for page in pdf.pages:
                    text = page.extract_text() or ""
                    for line in text.split('\n'):
                        line_clean = line.strip()
                        if not line_clean:
                            continue
                        line_lower = line_clean.lower()

                        if any(kw in line_lower for kw in ('checking activity', 'account activity', 'transaction history', 'deposits and credits', 'withdrawals and debits')):
                            in_activity_section = True
                            continue
                        if any(kw in line_lower for kw in ('checks paid', 'fee summary', 'service charge summary', 'customer service information', 'daily balance summary', 'relationship summary')):
                            in_activity_section = False
                            current_tx = None
                            continue

                        if not in_activity_section:
                            continue

                        if any(kw in line_lower for kw in ('total debits/credits', 'subtotal', 'balance forward', 'statement period', 'page ')):
                            continue

                        date_match = date_start_re.match(line_clean)
                        if date_match:
                            date_str = date_match.group(1)
                            parsed_date = self.parse_date(date_str, year=self.statement_year)
                            if parsed_date:
                                remainder = line_clean[date_match.end():].strip()
                                num_matches = list(number_re.finditer(remainder))

                                if num_matches:
                                    first_num_start = num_matches[0].start()
                                    desc_text = remainder[:first_num_start].strip()
                                    amounts = [self.clean_amount(m.group(1)) for m in num_matches]

                                    amount = Decimal('0.00')
                                    balance = None
                                    if len(amounts) >= 2:
                                        amount = amounts[-2]
                                        balance = amounts[-1]
                                    elif len(amounts) == 1:
                                        amount = amounts[0]

                                    current_tx = {
                                        'date': parsed_date,
                                        'description': desc_text,
                                        'amount': amount,
                                        'balance': balance,
                                        'category': 'Uncategorized'
                                    }
                                    transactions.append(current_tx)
                                else:
                                    current_tx = {
                                        'date': parsed_date,
                                        'description': remainder,
                                        'amount': Decimal('0.00'),
                                        'balance': None,
                                        'category': 'Uncategorized'
                                    }
                                    transactions.append(current_tx)
                        elif current_tx:
                            num_matches = list(number_re.finditer(line_clean))
                            if num_matches and current_tx['balance'] is None:
                                first_num_start = num_matches[0].start()
                                sub_desc = line_clean[:first_num_start].strip()
                                if sub_desc:
                                    current_tx['description'] = (current_tx['description'] + " " + sub_desc).strip()
                                amounts = [self.clean_amount(m.group(1)) for m in num_matches]
                                if len(amounts) >= 2:
                                    current_tx['amount'] = amounts[-2]
                                    current_tx['balance'] = amounts[-1]
                                elif len(amounts) == 1:
                                    current_tx['amount'] = amounts[0]
                            else:
                                if not any(kw in line_lower for kw in ('date', 'description', 'debits', 'credits', 'balance', 'beginning balance', 'ending balance')):
                                    current_tx['description'] = (current_tx['description'] + " " + line_clean).strip()

            if not transactions:
                raise ValueError("No table structures or transaction patterns found in the PDF. Please check if this is a scanned PDF or has an unrecognized layout.")

        # Adjust transaction signs using balance changes or keywords
        if transactions:
            for i in range(len(transactions)):
                curr_tx = transactions[i]
                if i == 0:
                    prev_bal = self.beginning_balance
                else:
                    prev_bal = transactions[i-1]['balance']

                if curr_tx['balance'] is not None and prev_bal is not None:
                    diff = curr_tx['balance'] - prev_bal
                    if diff != 0:
                        curr_tx['amount'] = diff
                else:
                    # Fallback check by description keyword
                    desc_lower = curr_tx['description'].lower()
                    debit_keywords = ['debit', 'withdrawal', 'payment', 'check', 'fee', 'charge', 'chk', 'wire out', 'wire to', 'transfer to', 'pmt', 'paylocity', 'hartford', 'aegon', 'paychex']
                    credit_keywords = ['credit', 'deposit', 'receipt', 'wire from', 'transfer from', 'interest paid', 'electronic credit']
                    
                    if any(kw in desc_lower for kw in debit_keywords):
                        if curr_tx['amount'] > 0:
                            curr_tx['amount'] = -curr_tx['amount']
                    elif any(kw in desc_lower for kw in credit_keywords):
                        if curr_tx['amount'] < 0:
                            curr_tx['amount'] = abs(curr_tx['amount'])

        # Clean description whitespace
        for tx in transactions:
            tx['description'] = re.sub(r'\s+', ' ', tx['description']).strip()

        # Calculate start and end date
        if transactions:
            sorted_txs = sorted(transactions, key=lambda x: x['date'])
            self.start_date = sorted_txs[0]['date']
            self.end_date = sorted_txs[-1]['date']

        # Determine self.ending_balance if not set
        if getattr(self, 'ending_balance', None) is None:
            if transactions:
                sorted_by_date = sorted(transactions, key=lambda x: x['date'])
                last_tx = sorted_by_date[-1]
                if last_tx.get('balance') is not None:
                    self.ending_balance = last_tx['balance']
                else:
                    self.ending_balance = Decimal('0.00')
            else:
                self.ending_balance = Decimal('0.00')

        account_meta = {
            'bank_name': self.bank_name,
            'account_number': self.account_number,
            'account_holder': self.account_holder,
            'currency': self.currency,
            'start_date': self.start_date,
            'end_date': self.end_date,
            'beginning_balance': self.beginning_balance or Decimal('0.00'),
            'ending_balance': self.ending_balance,
        }

        return account_meta, transactions

    def _extract_metadata_from_text(self, text, first_page_text=None):
        import re
        bank_keywords = {
            'chase': 'Chase Bank',
            'wells fargo': 'Wells Fargo',
            'bank of america': 'Bank of America',
            'citi': 'Citibank',
            'hsb': 'HSBC',
            'barclays': 'Barclays',
        }

        # Scan bank keywords only in the first page text to avoid matching transaction logs
        lookup_text = first_page_text if first_page_text else text
        for key, name in bank_keywords.items():
            if key in lookup_text.lower():
                self.bank_name = name
                break

        # Find all account number candidates
        candidates = []
        for match in re.finditer(r'(?:\b(?:account|acc|a/c|no\.?)\b|#).*?\b(\d[0-9-]{3,17})\b', text, re.IGNORECASE):
            candidates.append(match.group(1).replace('-', ''))
        for match in re.finditer(r'(?:\b(?:account|acc|a/c|no\.?)\b|#)\s*\n\s*\b(\d[0-9-]{3,17})\b', text, re.IGNORECASE):
            candidates.append(match.group(1).replace('-', ''))
            
        valid_candidates = []
        for cand in candidates:
            if len(cand) == 4 and (cand.startswith('20') or cand.startswith('19')):
                continue
            valid_candidates.append(cand)
            
        if valid_candidates:
            self.account_number = max(valid_candidates, key=len)

        # Resilient Beginning Balance Extraction
        beg_patterns = [
            r'(?:beginning|starting|opening|previous|prior)\s+balance\s*[\s:]\s*([-\$]?\d[\d,]*\.\d{2})',
            r'balance\s+forward\s*[\s:]\s*([-\$]?\d[\d,]*\.\d{2})',
        ]
        self.beginning_balance = None
        for pat in beg_patterns:
            match = re.search(pat, text, re.IGNORECASE)
            if match:
                val_str = match.group(1).replace('$', '').replace(',', '')
                try:
                    self.beginning_balance = Decimal(val_str)
                    break
                except Exception:
                    pass

        # Resilient Ending Balance Extraction
        end_patterns = [
            r'(?:ending|closing|current|new)\s+balance\s*[\s:]\s*([-\$]?\d[\d,]*\.\d{2})',
            r'balance\s+as\s+of\s+.*?\s*([-\$]?\d[\d,]*\.\d{2})',
        ]
        self.ending_balance = None
        for pat in end_patterns:
            match = re.search(pat, text, re.IGNORECASE)
            if match:
                val_str = match.group(1).replace('$', '').replace(',', '')
                try:
                    self.ending_balance = Decimal(val_str)
                    break
                except Exception:
                    pass

        name_match = re.search(r'(?:prepared\s+for|statement\s+for|customer|holder)\s*:?\s*([a-zA-Z\s]{3,30})', text, re.IGNORECASE)
        if name_match:
            self.account_holder = name_match.group(1).strip().split('\n')[0]

    def _find_headers(self, all_rows):
        date_syn = {'date', 'transaction date', 'tx date', 'value date', 'booking date', 'post date', 'posted date'}
        desc_syn = {'description', 'transaction details', 'details', 'particulars', 'narrative', 'remarks', 'transaction', 'memo', 'payee'}
        amount_syn = {'amount', 'value', 'transaction amount', 'net amount'}
        debit_syn = {'debit', 'withdrawals', 'payments', 'amount out', 'outflow', 'charge', 'withdrawal', 'paid out'}
        credit_syn = {'credit', 'deposits', 'receipts', 'amount in', 'inflow', 'deposit', 'paid in'}
        bal_syn = {'balance', 'running balance', 'ledger balance', 'outstanding balance'}

        best_row_idx = None
        best_score = 0
        best_mappings = None

        for r_idx in range(min(50, len(all_rows))):
            row_cells = [cell.strip().lower() for cell in all_rows[r_idx]]
            
            mappings = {
                'date_col_idx': None,
                'desc_col_idx': None,
                'amount_col_idx': None,
                'debit_col_idx': None,
                'credit_col_idx': None,
                'bal_col_idx': None
            }
            
            score = 0
            for col_idx, cell in enumerate(row_cells):
                if self.matches_header_type(cell, date_syn) and mappings['date_col_idx'] is None:
                    mappings['date_col_idx'] = col_idx
                    score += 2
                elif self.matches_header_type(cell, desc_syn) and mappings['desc_col_idx'] is None:
                    mappings['desc_col_idx'] = col_idx
                    score += 2
                elif self.matches_header_type(cell, amount_syn) and mappings['amount_col_idx'] is None:
                    mappings['amount_col_idx'] = col_idx
                    score += 2
                elif self.matches_header_type(cell, debit_syn) and mappings['debit_col_idx'] is None:
                    mappings['debit_col_idx'] = col_idx
                    score += 1
                elif self.matches_header_type(cell, credit_syn) and mappings['credit_col_idx'] is None:
                    mappings['credit_col_idx'] = col_idx
                    score += 1
                elif self.matches_header_type(cell, bal_syn) and mappings['bal_col_idx'] is None:
                    mappings['bal_col_idx'] = col_idx
                    score += 1

            has_date = mappings['date_col_idx'] is not None
            has_desc = mappings['desc_col_idx'] is not None
            has_amount = mappings['amount_col_idx'] is not None or (mappings['debit_col_idx'] is not None and mappings['credit_col_idx'] is not None)

            if has_date and has_desc and has_amount:
                if score > best_score:
                    best_score = score
                    best_row_idx = r_idx
                    best_mappings = mappings

        return best_row_idx, best_mappings

    def _parse_via_azure_openai(self):
        import base64
        import json
        import urllib.request
        import urllib.error
        import os
        import fitz
        import datetime
        from django.conf import settings

        # Retrieve Azure OpenAI configuration
        # Try loading from .env file manually if BASE_DIR contains it
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
        deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT") or os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME") or getattr(settings, "AZURE_OPENAI_DEPLOYMENT", None)

        if not api_key or not endpoint or not deployment:
            raise ValueError(
                "This bank statement PDF has a corrupt/empty text layer (or is a scanned image) "
                "which requires AI Vision parsing. Please configure your Azure OpenAI credentials in your "
                "environment variables or settings:\n"
                "- AZURE_OPENAI_API_KEY\n"
                "- AZURE_OPENAI_ENDPOINT\n"
                "- AZURE_OPENAI_DEPLOYMENT (the deployment ID of gpt-4o or gpt-4o-mini)"
            )

        # Clean endpoint
        endpoint = endpoint.strip().rstrip("/")
        url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-02-15-preview"

        # Render PDF pages to base64 images using PyMuPDF
        doc = fitz.open(self.file_path)
        base64_images = []
        for page in doc:
            pix = page.get_pixmap(dpi=150)
            img_data = pix.tobytes("jpeg")
            base64_str = base64.b64encode(img_data).decode("utf-8")
            base64_images.append(base64_str)

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a precise financial document parsing agent. Your task is to extract "
                    "bank statement metadata and transaction records from the provided page images. "
                    "Respond ONLY with a valid JSON object matching the requested schema."
                )
            }
        ]

        content_payload = [
            {
                "type": "text",
                "text": (
                    "Analyze the provided bank statement images. Extract the following information "
                    "and return it strictly as a JSON object:\n\n"
                    "JSON Schema:\n"
                    "{\n"
                    "  \"bank_name\": \"Name of the Bank\",\n"
                    "  \"account_number\": \"Account Number\",\n"
                    "  \"account_holder\": \"Account Holder Name or null\",\n"
                    "  \"beginning_balance\": 12345.67,\n"
                    "  \"ending_balance\": 12345.67,\n"
                    "  \"transactions\": [\n"
                    "    {\n"
                    "      \"date\": \"YYYY-MM-DD\",\n"
                    "      \"description\": \"Transaction Description\",\n"
                    "      \"amount\": -350.00,  // Positive for deposits, negative for payments/debits\n"
                    "      \"balance\": 12000.00  // Running balance on that line, or null\n"
                    "    }\n"
                    "  ]\n"
                    "}\n\n"
                    "Make sure all transaction amounts are correctly signed (deposits/credits positive, payments/withdrawals negative)."
                )
            }
        ]

        for b64_img in base64_images:
            content_payload.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{b64_img}"
                }
            })

        messages.append({
            "role": "user",
            "content": content_payload
        })

        request_body = {
            "messages": messages,
            "temperature": 0.0,
            "response_format": {"type": "json_object"}
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "api-key": api_key
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(req) as response:
                resp_data = json.loads(response.read().decode("utf-8"))
                content_str = resp_data["choices"][0]["message"]["content"]
                result = json.loads(content_str)
        except urllib.error.HTTPError as he:
            err_msg = he.read().decode("utf-8")
            raise ValueError(f"Azure OpenAI API request failed: {he.code} - {err_msg}")
        except Exception as e:
            raise ValueError(f"Failed to communicate with Azure OpenAI or parse JSON: {str(e)}")

        self.bank_name = result.get("bank_name", "Unknown Bank")
        self.account_number = result.get("account_number", "Unknown Account")
        self.account_holder = result.get("account_holder")
        self.beginning_balance = Decimal(str(result.get("beginning_balance") or "0.00"))
        self.ending_balance = Decimal(str(result.get("ending_balance") or "0.00"))

        transactions = []
        for tx in result.get("transactions", []):
            try:
                date_val = datetime.datetime.strptime(tx["date"], "%Y-%m-%d").date()
            except Exception:
                date_val = datetime.date.today()

            transactions.append({
                'date': date_val,
                'description': tx.get("description", "Unknown Transaction"),
                'amount': Decimal(str(tx.get("amount", "0.00"))),
                'balance': Decimal(str(tx["balance"])) if tx.get("balance") is not None else None,
                'category': 'Uncategorized'
            })

        # Calculate start and end date
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
            'beginning_balance': self.beginning_balance,
            'ending_balance': self.ending_balance,
        }

        return account_meta, transactions
