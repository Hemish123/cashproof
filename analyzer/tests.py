from django.test import TestCase
from decimal import Decimal
import datetime
from django.core.files.uploadedfile import SimpleUploadedFile

from .models import StatementUpload, BankAccount, Transaction
from .parsers.base import BaseParser
from .services import detect_interbank_transactions
from .parsers.csv_excel import CSVExcelParser

class ParserUtilityTests(TestCase):
    def setUp(self):
        self.parser = BaseParser(None)

    def test_clean_amount(self):
        # Normal positive and negative numbers
        self.assertEqual(self.parser.clean_amount("123.45"), Decimal("123.45"))
        self.assertEqual(self.parser.clean_amount("-123.45"), Decimal("-123.45"))
        
        # Dollar signs and commas
        self.assertEqual(self.parser.clean_amount("$1,234.56"), Decimal("1234.56"))
        self.assertEqual(self.parser.clean_amount("-$1,234.56"), Decimal("-1234.56"))

        # Parenthesized negatives
        self.assertEqual(self.parser.clean_amount("(500.00)"), Decimal("-500.00"))
        self.assertEqual(self.parser.clean_amount("($1,200.50)"), Decimal("-1200.50"))

        # End negatives
        self.assertEqual(self.parser.clean_amount("450.00-"), Decimal("-450.00"))

        # Empty/NaN values
        self.assertEqual(self.parser.clean_amount(""), Decimal("0.00"))
        self.assertEqual(self.parser.clean_amount("-"), Decimal("0.00"))
        self.assertEqual(self.parser.clean_amount("nan"), Decimal("0.00"))

    def test_parse_date(self):
        expected = datetime.date(2026, 8, 11)
        
        # Try different formats
        self.assertEqual(self.parser.parse_date("2026-08-11"), expected)
        self.assertEqual(self.parser.parse_date("08/11/2026"), expected)
        self.assertEqual(self.parser.parse_date("15/08/2026"), datetime.date(2026, 8, 15)) # DD/MM/YYYY fallback
        self.assertEqual(self.parser.parse_date("11-Aug-26"), expected)
        self.assertEqual(self.parser.parse_date("11-Aug-2026"), expected)
        self.assertEqual(self.parser.parse_date("Aug 11, 2026"), expected)
        self.assertEqual(self.parser.parse_date("2026.08.11"), expected)
        self.assertEqual(self.parser.parse_date("08/01", 2025), datetime.date(2025, 8, 1)) # Yearless fallback

    def test_matches_header_type(self):
        date_syn = {'date', 'transaction date'}
        desc_syn = {'description', 'details'}
        debit_syn = {'debit', 'withdrawals'}

        # Exact matches
        self.assertTrue(self.parser.matches_header_type("date", date_syn))
        # Substring matches
        self.assertTrue(self.parser.matches_header_type("Transaction Date", date_syn))
        self.assertTrue(self.parser.matches_header_type("Withdrawals (-)", debit_syn))
        self.assertTrue(self.parser.matches_header_type("date of post", date_syn))
        
        # Negative checks
        self.assertFalse(self.parser.matches_header_type("Transaction Date", desc_syn))
        self.assertFalse(self.parser.matches_header_type("amount column", date_syn))

    def test_borderless_line_regex(self):
        import re
        tx_line_re = re.compile(
            r'\b((?:\d{1,2}[-/.]\d{1,2}(?:[-/.]\d{2,4})?|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-zA-Z]*(?:\s*,\s*\d{2,4})?|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-zA-Z]*\s+\d{1,2}(?:\s*,\s*\d{2,4})?))\b'
            r'[\s,]+(.*?)'
            r'\s+(-?\$?\d[\d,]*\.\d{2}(?:\s*-)?|\(\$?\d[\d,]*\.\d{2}\))',
            re.IGNORECASE
        )
        bal_re = re.compile(r'(-?\$?\d[\d,]*\.\d{2}|\(\$?\d[\d,]*\.\d{2}\))')
        
        # Test line with date, description, amount, balance
        line1 = "12/31/2025 Opening Balance 5000.00 5000.00"
        match1 = tx_line_re.search(line1)
        self.assertIsNotNone(match1)
        groups1 = match1.groups()
        self.assertEqual(groups1[0], "12/31/2025")
        self.assertEqual(groups1[1], "Opening Balance")
        self.assertEqual(groups1[2], "5000.00")
        
        remainder1 = line1[match1.end():]
        bal_match1 = bal_re.search(remainder1)
        self.assertIsNotNone(bal_match1)
        self.assertEqual(bal_match1.group(1), "5000.00")

        # Test line without balance column
        line2 = "   * 08/15 Online Transfer to acct 5678 -500.00  ref: 00921"
        match2 = tx_line_re.search(line2)
        self.assertIsNotNone(match2)
        groups2 = match2.groups()
        self.assertEqual(groups2[0], "08/15")
        self.assertEqual(groups2[1], "Online Transfer to acct 5678")
        self.assertEqual(groups2[2], "-500.00")
        
        remainder2 = line2[match2.end():]
        bal_match2 = bal_re.search(remainder2)
        # reference code '00921' is not matched because it lacks a dot and decimal digits
        self.assertIsNone(bal_match2)

        # Test line with comma in date and separation
        line3 = "Dec 31, 2025,  Interest payment  $0.05"
        match3 = tx_line_re.search(line3)
        self.assertIsNotNone(match3)
        groups3 = match3.groups()
        self.assertEqual(groups3[0], "Dec 31, 2025")
        self.assertEqual(groups3[1], "Interest payment")
        self.assertEqual(groups3[2], "$0.05")

    def test_metadata_regex_rules(self):
        import re
        from decimal import Decimal
        text = """
        Citibank Relationship Summary
        ACCOUNT AS OF DECEMBER 31, 2025
        STREAMLINED CHECKING # 801464408
        Beginning Balance: $44,180.32
        """
        
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
            
        account_number = max(valid_candidates, key=len) if valid_candidates else None
        self.assertEqual(account_number, "801464408")
        
        beg_match = re.search(r'beginning\s+balance\s*[\s:]\s*([-\$]?\d[\d,]*\.\d{2})', text, re.IGNORECASE)
        self.assertIsNotNone(beg_match)
        beg_bal = Decimal(beg_match.group(1).replace('$', '').replace(',', ''))
        self.assertEqual(beg_bal, Decimal("44180.32"))

class InterbankAndAggregationTests(TestCase):
    def setUp(self):
        # Create a mock upload
        self.upload = StatementUpload.objects.create(status='COMPLETED')

        # Create two bank accounts (e.g. Checking and Savings)
        self.checking = BankAccount.objects.create(
            upload=self.upload,
            bank_name="Chase Checking",
            account_number="1234",
            currency="USD"
        )
        self.savings = BankAccount.objects.create(
            upload=self.upload,
            bank_name="Chase Savings",
            account_number="5678",
            currency="USD"
        )

    def test_interbank_detection_and_aggregates(self):
        # 1. Add normal transactions (non-interbank)
        # Checking: Salary deposit
        Transaction.objects.create(
            account=self.checking,
            date=datetime.date(2026, 8, 10),
            description="Employer payroll deposit",
            amount=Decimal("3000.00")
        )
        # Checking: Groceries payment
        Transaction.objects.create(
            account=self.checking,
            date=datetime.date(2026, 8, 12),
            description="Whole Foods market",
            amount=Decimal("-150.00")
        )

        # 2. Add matching interbank transactions (self-transfers)
        # checking has transfer out of 500 on Aug 15
        checking_outflow = Transaction.objects.create(
            account=self.checking,
            date=datetime.date(2026, 8, 15),
            description="Online transfer to acct 5678",
            amount=Decimal("-500.00")
        )
        # savings has matching transfer in of 500 on Aug 16 (within 2-day window)
        savings_inflow = Transaction.objects.create(
            account=self.savings,
            date=datetime.date(2026, 8, 16),
            description="Online transfer from checking",
            amount=Decimal("500.00")
        )

        # 3. Add text-keyword-based interbank transaction
        # savings has a credit card payment outflow
        cc_payment = Transaction.objects.create(
            account=self.savings,
            date=datetime.date(2026, 8, 20),
            description="Credit Card Payment AutoPay",
            amount=Decimal("-250.00")
        )

        # Execute interbank detection logic
        detect_interbank_transactions()

        # Refetch from DB to assert
        checking_outflow.refresh_from_db()
        savings_inflow.refresh_from_db()
        cc_payment.refresh_from_db()

        # Check interbank flags
        self.assertTrue(checking_outflow.is_interbank)
        self.assertTrue(savings_inflow.is_interbank)
        self.assertTrue(cc_payment.is_interbank)

        # Assert other transactions are NOT flagged
        self.assertFalse(Transaction.objects.get(description="Whole Foods market").is_interbank)
        self.assertFalse(Transaction.objects.get(description="Employer payroll deposit").is_interbank)

        # Refetch accounts to assert recalculated metrics
        self.checking.refresh_from_db()
        self.savings.refresh_from_db()

        # Checking aggregates check
        # Deposits = 3000.00, Interbank Deposits = 0.00 -> Net Deposits = 3000.00
        # Payments = 650.00 (150 + 500), Interbank Payments = 500.00 -> Net Payments = 150.00
        self.assertEqual(self.checking.total_deposits, Decimal("3000.00"))
        self.assertEqual(self.checking.total_payments, Decimal("650.00"))
        self.assertEqual(self.checking.total_interbank_deposits, Decimal("0.00"))
        self.assertEqual(self.checking.total_interbank_payments, Decimal("500.00"))
        self.assertEqual(self.checking.net_deposits, Decimal("3000.00"))
        self.assertEqual(self.checking.net_payments, Decimal("150.00"))

        # Savings aggregates check
        # Deposits = 500.00, Interbank Deposits = 500.00 -> Net Deposits = 0.00
        # Payments = 250.00, Interbank Payments = 250.00 -> Net Payments = 0.00
        self.assertEqual(self.savings.total_deposits, Decimal("500.00"))
        self.assertEqual(self.savings.total_payments, Decimal("250.00"))
        self.assertEqual(self.savings.total_interbank_deposits, Decimal("500.00"))
        self.assertEqual(self.savings.total_interbank_payments, Decimal("250.00"))
        self.assertEqual(self.savings.net_deposits, Decimal("0.00"))
        self.assertEqual(self.savings.net_payments, Decimal("0.00"))
