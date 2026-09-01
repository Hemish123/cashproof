import os
import django
from decimal import Decimal

# Setup Django Environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'statement_analyzer.settings')
django.setup()

from analyzer.models import StatementUpload, BankAccount, Transaction
from analyzer.parsers.manager import process_statement
from analyzer.services import detect_interbank_transactions
from analyzer.reports import generate_excel_report
from django.core.files import File

def run_manual_verification():
    print("--- Starting End-to-End Cashflow Verification Script ---")

    # 1. Clean Database
    print("Clearing database...")
    StatementUpload.objects.all().delete()
    BankAccount.objects.all().delete()
    Transaction.objects.all().delete()

    # 2. Check if files exist
    checking_path = "chase_checking.csv"
    savings_path = "chase_savings.xlsx"

    if not os.path.exists(checking_path) or not os.path.exists(savings_path):
        print("Error: Mock files chase_checking.csv and chase_savings.xlsx must be in the current directory.")
        return

    # 3. Process Chase Checking CSV
    print(f"Uploading and parsing {checking_path}...")
    upload_checking = StatementUpload()
    with open(checking_path, 'rb') as f:
        upload_checking.file.save(checking_path, File(f), save=True)
    
    # Process statement
    process_statement(upload_checking.id)
    upload_checking.refresh_from_db()
    print(f"Checking Status: {upload_checking.status}")
    if upload_checking.status == 'FAILED':
        print(f"Error logic: {upload_checking.error_message}")
        return

    # 4. Process Chase Savings XLSX
    print(f"Uploading and parsing {savings_path}...")
    upload_savings = StatementUpload()
    with open(savings_path, 'rb') as f:
        upload_savings.file.save(savings_path, File(f), save=True)
    
    # Process statement
    process_statement(upload_savings.id)
    upload_savings.refresh_from_db()
    print(f"Savings Status: {upload_savings.status}")
    if upload_savings.status == 'FAILED':
        print(f"Error logic: {upload_savings.error_message}")
        return

    # 5. Run Interbank Detection
    print("Running interbank transfer detection algorithm...")
    detect_interbank_transactions()

    # 6. Verify assertions
    accounts = BankAccount.objects.all()
    print(f"\nSuccessfully extracted {accounts.count()} accounts:")
    
    for acc in accounts:
        print(f"\nAccount: {acc.bank_name} (A/C: {acc.account_number})")
        print(f"  Total Deposits: ${acc.total_deposits:.2f}")
        print(f"  Total Payments: ${acc.total_payments:.2f}")
        print(f"  Total Interbank Deposits: ${acc.total_interbank_deposits:.2f}")
        print(f"  Total Interbank Payments: ${acc.total_interbank_payments:.2f}")
        print(f"  Net Deposits: ${acc.net_deposits:.2f}")
        print(f"  Net Payments: ${acc.net_payments:.2f}")

    # 7. Print paired interbank transactions
    print("\nPaired Interbank Self-Transfers:")
    interbanks = Transaction.objects.filter(is_interbank=True).order_by('date')
    for tx in interbanks:
        print(f"  [{tx.date}] {tx.account.bank_name} | {tx.description} | ${tx.amount:.2f}")

    # 8. Generate Excel report in workspace
    report_output_path = "FDD_Statement_Cashflow_Report.xlsx"
    print(f"\nGenerating standardized Excel report to {report_output_path}...")
    generate_excel_report(accounts, report_output_path)
    print("Report generated successfully!")

if __name__ == "__main__":
    run_manual_verification()
