import pandas as pd
from decimal import Decimal
from .base import BaseParser

class CSVExcelParser(BaseParser):
    def parse(self):
        # Determine file type and load
        if self.file_path.endswith('.csv'):
            # Try parsing with default commas, if that fails or looks like one column, try semicolon/tabs
            try:
                df_raw = pd.read_csv(self.file_path, header=None)
            except Exception:
                df_raw = pd.read_csv(self.file_path, header=None, sep=None, engine='python')
        else:
            df_raw = pd.read_excel(self.file_path, header=None)

        # 1. Search for metadata in the first few rows (before header)
        self._extract_metadata(df_raw)

        # 2. Find header row using synonym heuristic
        header_idx, mappings = self._find_headers(df_raw)
        
        if header_idx is None:
            raise ValueError("Could not find standard transaction headers (Date, Description, Amount/Debit/Credit) in the statement.")

        # 3. Read data from header row onwards
        df = df_raw.iloc[header_idx + 1:].copy()
        df.columns = df_raw.iloc[header_idx].tolist()
        
        # Clean column names to string for mapping
        df.columns = [str(col).strip().lower() if col is not None else "" for col in df.columns]

        transactions = []
        for _, row in df.iterrows():
            # Get date
            date_val = row.iloc[mappings['date_col_idx']]
            parsed_date = self.parse_date(date_val)
            if not parsed_date:
                continue # Skip row if it doesn't have a valid date (e.g. footer/summary rows)

            # Get description
            desc_val = row.iloc[mappings['desc_col_idx']]
            description = str(desc_val).strip() if pd.notna(desc_val) else ""
            if not description or description.lower() in ('total', 'subtotal', 'balance forward', 'summary'):
                continue # Skip summary rows

            # Get amount
            amount = Decimal('0.00')
            if mappings['amount_col_idx'] is not None:
                amount = self.clean_amount(row.iloc[mappings['amount_col_idx']])
            else:
                # Split Debit/Credit columns
                debit_val = Decimal('0.00')
                credit_val = Decimal('0.00')
                if mappings['debit_col_idx'] is not None:
                    debit_val = self.clean_amount(row.iloc[mappings['debit_col_idx']])
                    # If debits are positive in the sheet, make them negative
                    if debit_val > 0:
                        debit_val = -debit_val
                if mappings['credit_col_idx'] is not None:
                    credit_val = self.clean_amount(row.iloc[mappings['credit_col_idx']])
                    if credit_val < 0:
                        # Ensure credit is positive
                        credit_val = abs(credit_val)
                amount = debit_val + credit_val

            # Skip transactions with zero amount to avoid spamming the database
            if amount == 0:
                continue

            # Get balance
            balance = None
            if mappings['bal_col_idx'] is not None:
                balance_val = row.iloc[mappings['bal_col_idx']]
                if pd.notna(balance_val):
                    balance = self.clean_amount(balance_val)

            # Get category if column present in file or derive from description
            raw_cat = None
            if mappings.get('cat_col_idx') is not None:
                cat_val = row.iloc[mappings['cat_col_idx']]
                if pd.notna(cat_val):
                    raw_cat = str(cat_val).strip()

            category = self.derive_category(description, raw_cat, amount)

            transactions.append({
                'date': parsed_date,
                'description': description,
                'amount': amount,
                'balance': balance,
                'category': category
            })

        # Calculate statement start and end date
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
        }

        return account_meta, transactions

    def _extract_metadata(self, df):
        # Scan first 15 rows for metadata patterns
        import re
        bank_keywords = {
            'chase': 'Chase Bank',
            'wells fargo': 'Wells Fargo',
            'bank of america': 'Bank of America',
            'citi': 'Citibank',
            'hsb': 'HSBC',
            'barclays': 'Barclays',
        }
        
        for r_idx in range(min(15, len(df))):
            row_str = " ".join([str(val) for val in df.iloc[r_idx] if pd.notna(val)])
            
            # Detect bank name
            for key, name in bank_keywords.items():
                if key in row_str.lower() and self.bank_name == "Unknown Bank":
                    self.bank_name = name

            # Detect account number
            acc_match = re.search(r'\b(?:account|acc|a/c|no\.?)\b.*?\b(\d[0-9-]{3,17})\b', row_str, re.IGNORECASE)
            if acc_match and self.account_number == "Unknown Account":
                self.account_number = acc_match.group(1)

            # Detect account holder
            name_match = re.search(r'(?:name|holder|customer|client)\s*:?\s*([a-zA-Z\s]{3,30})', row_str, re.IGNORECASE)
            if name_match and not self.account_holder:
                self.account_holder = name_match.group(1).strip()

    def _find_headers(self, df):
        # Synonyms
        date_syn = {'date', 'transaction date', 'tx date', 'value date', 'booking date', 'post date', 'posted date'}
        desc_syn = {'description', 'transaction details', 'details', 'particulars', 'narrative', 'remarks', 'transaction', 'memo', 'payee'}
        amount_syn = {'amount', 'value', 'transaction amount', 'net amount'}
        debit_syn = {'debit', 'withdrawals', 'payments', 'amount out', 'outflow', 'charge', 'withdrawal', 'paid out'}
        credit_syn = {'credit', 'deposits', 'receipts', 'amount in', 'inflow', 'deposit', 'paid in'}
        bal_syn = {'balance', 'running balance', 'ledger balance', 'outstanding balance'}
        cat_syn = {'category', 'cat', 'type', 'transaction type', 'tx type', 'classification', 'channel', 'code', 'category name'}

        best_row_idx = None
        best_score = 0
        best_mappings = None

        # Look at the first 25 rows to identify the header row
        for r_idx in range(min(25, len(df))):
            row_cells = [str(val).strip().lower() if pd.notna(val) else "" for val in df.iloc[r_idx]]
            
            mappings = {
                'date_col_idx': None,
                'desc_col_idx': None,
                'amount_col_idx': None,
                'debit_col_idx': None,
                'credit_col_idx': None,
                'bal_col_idx': None,
                'cat_col_idx': None
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
                elif self.matches_header_type(cell, cat_syn) and mappings['cat_col_idx'] is None:
                    mappings['cat_col_idx'] = col_idx
                    score += 1

            # Valid header must have at least a date, description, and some amount indicators
            has_date = mappings['date_col_idx'] is not None
            has_desc = mappings['desc_col_idx'] is not None
            has_amount = mappings['amount_col_idx'] is not None or (mappings['debit_col_idx'] is not None and mappings['credit_col_idx'] is not None)

            if has_date and has_desc and has_amount:
                if score > best_score:
                    best_score = score
                    best_row_idx = r_idx
                    best_mappings = mappings

        return best_row_idx, best_mappings
