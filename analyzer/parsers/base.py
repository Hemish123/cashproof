# import datetime
# from decimal import Decimal, InvalidOperation
# import re

# class BaseParser:
#     def __init__(self, file_path):
#         self.file_path = file_path
#         self.bank_name = "Unknown Bank"
#         self.account_number = "Unknown Account"
#         self.account_holder = None
#         self.account_title = "Operating Account"
#         self.currency = "USD"
#         self.start_date = None
#         self.end_date = None
#         self.beginning_balance = None
#         self.ending_balance = None

#     def parse(self):
#         """
#         Parses the file and returns a tuple:
#         (account_metadata_dict, list_of_transaction_dicts)
        
#         transaction_dict schema:
#         {
#             'date': datetime.date,
#             'description': str,
#             'amount': Decimal,  # Inflows positive, Outflows negative
#             'balance': Decimal or None,
#             'category': str
#         }
#         """
#         raise NotImplementedError("Subclasses must implement parse()")

#     def derive_category(self, description, category_raw=None, amount=None):
#         if category_raw and str(category_raw).strip():
#             raw_str = str(category_raw).strip()
#             if raw_str.lower() not in ('uncategorized', 'nan', 'none', '-', ''):
#                 return raw_str.title()

#         desc = (str(description) if description else "").upper()

#         if re.search(r'\b(CHECK|CHK)\b', desc):
#             return 'Check'
#         if re.search(r'\b(WIRE|FEDWIRE)\b', desc):
#             return 'Wire Transfer'
#         if re.search(r'\b(ACH|DIRDEP|DIRECT\s*DEP|PAYROLL)\b', desc):
#             return 'ACH / Direct Deposit'
#         if re.search(r'\b(TRANSFER|XFER|CBUSOL\s*TRANSFER|ONLINE\s*TRANSFER|INTERNAL)\b', desc):
#             return 'Self Transfer'
#         if re.search(r'\b(FEE|SERVICE\s*CHG|CHARGE|OVERDRAFT|MAINTENANCE)\b', desc):
#             return 'Bank Fee'
#         if re.search(r'\b(INTEREST|INT\s*PAID)\b', desc):
#             return 'Interest'
#         if re.search(r'\b(TAX|TAXES|IRS|STATE\s*TAX|CORP\s*TAX)\b', desc):
#             return 'Taxes'
#         if re.search(r'\b(POS|DEBIT\s*CARD|CARD\s*PURCHASE|PURCHASE)\b', desc):
#             return 'Card Transaction'

#         if amount is not None:
#             try:
#                 amt = Decimal(str(amount))
#                 if amt > 0:
#                     return 'Deposit'
#                 elif amt < 0:
#                     return 'Payment'
#             except Exception:
#                 pass

#         return 'Uncategorized'

#     def clean_amount(self, value):
#         if value is None or (isinstance(value, float) and import_math_is_nan_helper(value)):
#             return Decimal('0.00')
#         if isinstance(value, (int, Decimal)):
#             return Decimal(str(value))
#         if isinstance(value, float):
#             return Decimal(f"{value:.2f}")
        
#         val_str = str(value).strip().replace('$', '').replace(',', '')
#         if not val_str or val_str.lower() in ('nan', 'none', '-'):
#             return Decimal('0.00')
        
#         # Handle parenthesized negatives: e.g., (1,200.00) -> -1200.00
#         is_negative = False
#         if val_str.startswith('(') and val_str.endswith(')'):
#             is_negative = True
#             val_str = val_str[1:-1]
#         elif val_str.startswith('-'):
#             is_negative = True
#             val_str = val_str[1:]
#         elif val_str.endswith('-'):
#             is_negative = True
#             val_str = val_str[:-1]

#         try:
#             amount = Decimal(val_str)
#             return -amount if is_negative else amount
#         except InvalidOperation:
#             return Decimal('0.00')

#     def parse_date(self, val, year=None):
#         if isinstance(val, (datetime.date, datetime.datetime)):
#             return val.date() if isinstance(val, datetime.datetime) else val
        
#         val_str = str(val).strip()
#         if not val_str or val_str.lower() in ('nan', 'none', '-'):
#             return None
        
#         # Normalize all delimiters (spaces, commas, dots, slashes, hyphens) to a single hyphen
#         val_str_norm = re.sub(r'[-/.,\s]+', '-', val_str)
        
#         formats = [
#             '%Y-%m-%d', '%m-%d-%Y', '%d-%m-%Y', '%d-%b-%y', '%d-%b-%Y',
#             '%b-%d-%Y', '%B-%d-%Y', '%d-%m-%y', '%m-%d-%y',
#             '%Y-%b-%d'
#         ]
        
#         for fmt in formats:
#             try:
#                 return datetime.datetime.strptime(val_str_norm, fmt).date()
#             except ValueError:
#                 continue
                
#         # Last resort: try regex search for standard date strings
#         match = re.search(r'(\d{1,4}[-/.]\d{1,2}[-/.]?\d{0,4})', val_str)
#         if match:
#             date_part = re.sub(r'[-/.,\s]+', '-', match.group(1))
#             for fmt in ['%Y-%m-%d', '%m-%d-%Y', '%d-%m-%Y', '%d-%m-%y', '%m-%d-%y']:
#                 try:
#                     return datetime.datetime.strptime(date_part, fmt).date()
#                 except ValueError:
#                     continue

#         # Fallback: if year is provided and not in date string, append it
#         if year is not None:
#             if str(year) not in val_str:
#                 appended_val = f"{val_str}-{year}"
#                 return self.parse_date(appended_val)
                    
#         return None

#     def matches_header_type(self, cell_val, synonyms, substring_synonyms=None):
#         if cell_val is None:
#             return False
#         val = str(cell_val).strip().lower()
        
#         # 1. Exact match
#         if val in synonyms:
#             return True
            
#         # 2. Substring match
#         if substring_synonyms is None:
#             # Filter out generic terms like 'transaction', 'value' for substring matching
#             substring_synonyms = {s for s in synonyms if s not in ('transaction', 'value')}
            
#         for sub in substring_synonyms:
#             if sub in val:
#                 return True
                
#         # Specialized rule: if looking for date and 'date' is a substring
#         if 'date' in synonyms and 'date' in val:
#             return True
            
#         return False

# def import_math_is_nan_helper(val):
#     import math
#     return math.isnan(val)



import datetime
from decimal import Decimal, InvalidOperation
import re

class BaseParser:
    def __init__(self, file_path):
        self.file_path = file_path
        self.bank_name = "Unknown Bank"
        self.account_number = "Unknown Account"
        self.account_holder = None
        self.account_title = "Operating Account"
        self.currency = "USD"
        self.start_date = None
        self.end_date = None
        self.beginning_balance = None
        self.ending_balance = None

    def parse(self):
        """
        Parses the file and returns a tuple:
        (account_metadata_dict, list_of_transaction_dicts)
        
        transaction_dict schema:
        {
            'date': datetime.date,
            'description': str,
            'amount': Decimal,  # Inflows positive, Outflows negative
            'balance': Decimal or None,
            'category': str
        }
        """
        raise NotImplementedError("Subclasses must implement parse()")

    def derive_category(self, description, category_raw=None, amount=None):
        if category_raw and str(category_raw).strip():
            raw_str = str(category_raw).strip()
            if raw_str.lower() not in ('uncategorized', 'nan', 'none', '-', ''):
                return raw_str.title()

        desc = (str(description) if description else "").upper()

        if re.search(r'\b(CHECK|CHK)\b', desc):
            return 'Check'
        if re.search(r'\b(WIRE|FEDWIRE)\b', desc):
            return 'Wire Transfer'
        if re.search(r'\b(ACH|DIRDEP|DIRECT\s*DEP|PAYROLL)\b', desc):
            return 'ACH / Direct Deposit'
        if re.search(r'\b(TRANSFER|XFER|CBUSOL\s*TRANSFER|ONLINE\s*TRANSFER|INTERNAL)\b', desc):
            return 'Self Transfer'
        if re.search(r'\b(FEE|SERVICE\s*CHG|CHARGE|OVERDRAFT|MAINTENANCE)\b', desc):
            return 'Bank Fee'
        if re.search(r'\b(INTEREST|INT\s*PAID)\b', desc):
            return 'Interest'
        if re.search(r'\b(TAX|TAXES|IRS|STATE\s*TAX|CORP\s*TAX)\b', desc):
            return 'Taxes'
        if re.search(r'\b(POS|DEBIT\s*CARD|CARD\s*PURCHASE|PURCHASE)\b', desc):
            return 'Card Transaction'

        if amount is not None:
            try:
                amt = Decimal(str(amount))
                if amt > 0:
                    return 'Deposit'
                elif amt < 0:
                    return 'Payment'
            except Exception:
                pass

        return 'Uncategorized'

    def clean_amount(self, value):
        if value is None or (isinstance(value, float) and import_math_is_nan_helper(value)):
            return Decimal('0.00')
        if isinstance(value, (int, Decimal)):
            return Decimal(str(value))
        if isinstance(value, float):
            return Decimal(f"{value:.2f}")
        
        val_str = str(value).strip().replace('$', '').replace(',', '')
        if not val_str or val_str.lower() in ('nan', 'none', '-'):
            return Decimal('0.00')
        
        # Handle parenthesized negatives: e.g., (1,200.00) -> -1200.00
        is_negative = False
        if val_str.startswith('(') and val_str.endswith(')'):
            is_negative = True
            val_str = val_str[1:-1]
        elif val_str.startswith('-'):
            is_negative = True
            val_str = val_str[1:]
        elif val_str.endswith('-'):
            is_negative = True
            val_str = val_str[:-1]

        try:
            amount = Decimal(val_str)
            return -amount if is_negative else amount
        except InvalidOperation:
            return Decimal('0.00')

    def parse_date(self, val, year=None):
        if isinstance(val, (datetime.date, datetime.datetime)):
            return val.date() if isinstance(val, datetime.datetime) else val
        
        val_str = str(val).strip()
        if not val_str or val_str.lower() in ('nan', 'none', '-'):
            return None
        
        # Normalize all delimiters (spaces, commas, dots, slashes, hyphens) to a single hyphen
        val_str_norm = re.sub(r'[-/.,\s]+', '-', val_str)
        
        formats = [
            '%Y-%m-%d', '%m-%d-%Y', '%d-%m-%Y', '%d-%b-%y', '%d-%b-%Y',
            '%b-%d-%Y', '%B-%d-%Y', '%d-%m-%y', '%m-%d-%y',
            '%Y-%b-%d'
        ]
        
        for fmt in formats:
            try:
                return datetime.datetime.strptime(val_str_norm, fmt).date()
            except ValueError:
                continue
                
        # Last resort: try regex search for standard date strings
        match = re.search(r'(\d{1,4}[-/.]\d{1,2}[-/.]?\d{0,4})', val_str)
        if match:
            date_part = re.sub(r'[-/.,\s]+', '-', match.group(1))
            for fmt in ['%Y-%m-%d', '%m-%d-%Y', '%d-%m-%Y', '%d-%m-%y', '%m-%d-%y']:
                try:
                    return datetime.datetime.strptime(date_part, fmt).date()
                except ValueError:
                    continue

        # Fallback: if year is provided and not in date string, append it
        if year is not None:
            if str(year) not in val_str:
                appended_val = f"{val_str}-{year}"
                return self.parse_date(appended_val)
                    
        return None

    # Generic fallback for detecting a bank/institution name when it isn't
    # one of the handful of major brands in the hardcoded keyword lists used
    # by the PDF and CSV/Excel parsers. Looks for a line near the top of the
    # statement that reads like an institution name (ends in "Bank",
    # "Credit Union", "N.A.", "Savings", "Trust", etc.) rather than relying
    # on an exact brand match.
    _INSTITUTION_NAME_RE = re.compile(
        r"([A-Z][A-Za-z&.,'\- ]{1,45}?\s"
        r"(?:Bank(?:ing)?|N\.?A\.?|Credit\s+Union|Savings(?:\s+Bank)?|"
        r"Trust(?:\s+Company)?|Financial(?:\s+Group)?|Bancorp|Bankshares))\b"
    )

    def guess_bank_name_generic(self, text, max_lines=15):
        """
        Fallback bank-name detector for statements whose bank isn't in the
        known-brand keyword dictionary. Scans the first `max_lines` lines
        (the header area, where the institution name normally appears) for
        something that looks like "<Name> Bank" / "<Name> Credit Union" /
        "<Name> N.A." etc., rather than matching an exact brand list.

        Returns the matched name, or None if nothing plausible is found.
        """
        if not text:
            return None

        for line in text.splitlines()[:max_lines]:
            line = line.strip()
            if not line:
                continue
            # Skip obvious column-header / label lines, not institution names
            if re.match(r'^(date|description|amount|balance|account|statement)\b', line, re.IGNORECASE):
                continue
            m = self._INSTITUTION_NAME_RE.search(line)
            if m:
                candidate = re.sub(r'\s+', ' ', m.group(1)).strip(" .,-")
                # Guard against grabbing a full sentence ("...visit our Bank")
                if 1 <= len(candidate.split()) <= 6:
                    return candidate
        return None

    def matches_header_type(self, cell_val, synonyms, substring_synonyms=None):
        if cell_val is None:
            return False
        val = str(cell_val).strip().lower()
        
        # 1. Exact match
        if val in synonyms:
            return True
            
        # 2. Substring match
        if substring_synonyms is None:
            # Filter out generic terms like 'transaction', 'value' for substring matching
            substring_synonyms = {s for s in synonyms if s not in ('transaction', 'value')}
            
        for sub in substring_synonyms:
            if sub in val:
                return True
                
        # Specialized rule: if looking for date and 'date' is a substring
        if 'date' in synonyms and 'date' in val:
            return True
            
        return False

def import_math_is_nan_helper(val):
    import math
    return math.isnan(val)
