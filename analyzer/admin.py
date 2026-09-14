from django.contrib import admin
from .models import StatementUpload, BankAccount, Transaction
# Register your models here.

admin.site.register(StatementUpload)
admin.site.register(BankAccount)
admin.site.register(Transaction)
