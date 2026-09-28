from django.db import models
from django.contrib.auth.models import User
import os

class Project(models.Model):
    name = models.CharField(max_length=255)
    client_name = models.CharField(max_length=255, blank=True, null=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='projects')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class StatementUpload(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('PROCESSING', 'Processing'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='uploads', null=True, blank=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='uploads')
    file = models.FileField(upload_to='statements/')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    error_message = models.TextField(null=True, blank=True)
    total_deposits = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    total_payments = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)

    def filename(self):
        return os.path.basename(self.file.name)

    def __str__(self):
        return f"Upload {self.id} - {self.filename()} ({self.status})"

class BankAccount(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='bank_accounts', null=True, blank=True)
    upload = models.ForeignKey(StatementUpload, on_delete=models.CASCADE, related_name='bank_accounts', null=True, blank=True)
    is_manual = models.BooleanField(default=False)
    bank_name = models.CharField(max_length=100)
    account_number = models.CharField(max_length=50)
    account_holder = models.CharField(max_length=255, null=True, blank=True)
    account_title = models.CharField(max_length=255, null=True, blank=True, default='Operating Account')
    currency = models.CharField(max_length=10, default='USD')
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    beginning_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    ending_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)

    # Core Summary Aggregations
    total_deposits = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    total_payments = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    total_interbank_deposits = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    total_interbank_payments = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    net_deposits = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    net_payments = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)

    def __str__(self):
        return f"{self.bank_name} - {self.account_number} ({self.account_holder or 'No Holder'})"

class Transaction(models.Model):
    account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name='transactions')
    date = models.DateField()
    description = models.TextField()
    amount = models.DecimalField(max_digits=15, decimal_places=2) # Positive for deposit, Negative for payment
    balance = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    category = models.CharField(max_length=100, default='Uncategorized')
    is_interbank = models.BooleanField(default=False)
    interbank_confidence = models.CharField(max_length=20, null=True, blank=True)
    interbank_match_status = models.CharField(max_length=50, null=True, blank=True)

    def __str__(self):
        tx_type = "Deposit" if self.amount >= 0 else "Payment"
        return f"[{self.date}] {self.account.bank_name}: {tx_type} of {self.amount} ({self.description[:30]})"
