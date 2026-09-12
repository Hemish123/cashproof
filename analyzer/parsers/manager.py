import os
from django.db import transaction as db_transaction
from ..models import StatementUpload, BankAccount, Transaction
from .csv_excel import CSVExcelParser
from .pdf_parser import PDFStatementParser

def process_statement(upload_id, user=None):
    """
    Processes an uploaded statement, saving accounts and transactions.
    """
    upload = StatementUpload.objects.get(id=upload_id)
    upload.status = 'PROCESSING'
    upload.save()

    file_path = upload.file.path
    ext = os.path.splitext(file_path)[1].lower()

    try:
        if ext == '.pdf':
            parser = PDFStatementParser(file_path)
        elif ext in ('.csv', '.xlsx', '.xls'):
            parser = CSVExcelParser(file_path)
        else:
            raise ValueError(f"Unsupported file format: {ext}")

        account_meta, transactions_data = parser.parse()

        # Database transaction to write results atomically
        with db_transaction.atomic():
            # Deduplicate: Remove any existing account for the exact same bank, account number & period
            start_d = account_meta.get('start_date')
            end_d = account_meta.get('end_date')
            if start_d and end_d:
                existing_accs = list(BankAccount.objects.filter(
                    bank_name=account_meta.get('bank_name', 'Unknown Bank'),
                    account_number=account_meta.get('account_number', 'Unknown Account'),
                    start_date=start_d,
                    end_date=end_d
                ))
                for old_acc in existing_accs:
                    old_upload = old_acc.upload
                    old_acc.delete()
                    if old_upload and old_upload.id != upload.id:
                        try:
                            old_upload.delete()
                        except Exception:
                            pass

            # Create Bank Account
            bank_account = BankAccount.objects.create(
                upload=upload,
                bank_name=account_meta.get('bank_name', 'Unknown Bank'),
                account_number=account_meta.get('account_number', 'Unknown Account'),
                account_holder=account_meta.get('account_holder'),
                currency=account_meta.get('currency', 'USD'),
                start_date=start_d,
                end_date=end_d,
                beginning_balance=account_meta.get('beginning_balance', 0.00),
                ending_balance=account_meta.get('ending_balance', 0.00)
            )

            # Create Transactions
            tx_instances = []
            for tx_data in transactions_data:
                tx_instances.append(
                    Transaction(
                        account=bank_account,
                        date=tx_data['date'],
                        description=tx_data['description'],
                        amount=tx_data['amount'],
                        balance=tx_data.get('balance'),
                        category=tx_data.get('category', 'Uncategorized'),
                        is_interbank=False
                    )
                )
            Transaction.objects.bulk_create(tx_instances)

            # Calculate and update aggregates (temporary values before interbank processing)
            update_account_aggregates(bank_account)

        upload.status = 'COMPLETED'
        upload.error_message = None
        upload.save()
        
        # Trigger cross-account interbank matching and recalculate all summaries
        run_interbank_detection_for_upload(user=user)
        
        return bank_account

    except Exception as e:
        import traceback
        upload.status = 'FAILED'
        upload.error_message = f"{str(e)}\n\n{traceback.format_exc()}"
        upload.save()
        raise e

def update_account_aggregates(bank_account):
    """
    Recalculates metrics for a single bank account and saves them.
    """
    from decimal import Decimal
    from django.db.models import Sum

    txs = bank_account.transactions.all()
    
    # Deposits (amount > 0)
    total_deposits = txs.filter(amount__gt=0).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    
    # Payments (amount < 0) - convert to positive absolute value
    raw_payments_sum = txs.filter(amount__lt=0).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    total_payments = abs(raw_payments_sum)

    # Interbank deposits and payments
    total_interbank_deposits = txs.filter(amount__gt=0, is_interbank=True).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    raw_interbank_payments_sum = txs.filter(amount__lt=0, is_interbank=True).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    total_interbank_payments = abs(raw_interbank_payments_sum)

    # Net values
    net_deposits = total_deposits - total_interbank_deposits
    net_payments = total_payments - total_interbank_payments

    # Save to BankAccount
    bank_account.total_deposits = total_deposits
    bank_account.total_payments = total_payments
    bank_account.total_interbank_deposits = total_interbank_deposits
    bank_account.total_interbank_payments = total_interbank_payments
    bank_account.net_deposits = net_deposits
    bank_account.net_payments = net_payments

    # Fallback to transaction lines for beginning/ending balance if not extracted by metadata
    if bank_account.beginning_balance == Decimal('0.00') and bank_account.ending_balance == Decimal('0.00'):
        txs_sorted = list(txs.order_by('date', 'id'))
        if txs_sorted:
            first_tx = txs_sorted[0]
            last_tx = txs_sorted[-1]
            if first_tx.balance is not None:
                bank_account.beginning_balance = first_tx.balance - first_tx.amount
            if last_tx.balance is not None:
                bank_account.ending_balance = last_tx.balance

    bank_account.save()

def run_interbank_detection_for_upload(user=None):
    """
    Trigger interbank matching and update all accounts scoped to the given user.
    """
    from ..services import detect_interbank_transactions
    detect_interbank_transactions(user=user)
