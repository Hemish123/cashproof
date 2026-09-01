import datetime
from decimal import Decimal, InvalidOperation
import re

class BaseParser:
    def __init__(self, file_path):
        self.file_path = file_path
        self.bank_name = "Unknown Bank"
        self.account_number = "Unknown Account"
        self.account_holder = None
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
