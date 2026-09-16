import os
import django
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'statement_analyzer.settings')
django.setup()

from analyzer.models import BankAccount, Transaction
from analyzer.services import detect_interbank_transactions

# Print initial state
accounts = BankAccount.objects.all()
print("Accounts:")
for acc in accounts:
    print(f"ID: {acc.id} | Bank: {acc.bank_name} | Acc: {acc.account_number} | Holder: {acc.account_holder}")

print("\n--- Running Interbank Detection ---")
detect_interbank_transactions()

print("\n--- Interbank Results ---")
for acc in accounts:
    interbanks = acc.transactions.filter(is_interbank=True)
    possible = acc.transactions.filter(category='Possible Interbank (Unmatched)')
    print(f"\nAccount {acc.account_number} ({acc.bank_name}):")
    print(f"  Confirmed Interbank: {interbanks.count()}")
    for t in interbanks:
        print(f"    - {t.date} | {t.amount} | {t.description}")
    print(f"  Possible Interbank: {possible.count()}")
    for t in possible:
        print(f"    - {t.date} | {t.amount} | {t.description}")
