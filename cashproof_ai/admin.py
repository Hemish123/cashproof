from django.contrib import admin
from .models import (
    CashProofSession, ClientAccount, ParsedStatement,
    LedgerTransaction, InterbankMatch,
)


@admin.register(CashProofSession)
class CashProofSessionAdmin(admin.ModelAdmin):
    list_display  = ("id", "client_name", "user", "status", "created_at")
    list_filter   = ("status",)
    search_fields = ("client_name",)


@admin.register(ClientAccount)
class ClientAccountAdmin(admin.ModelAdmin):
    list_display  = ("id", "session", "bank_name", "account_number", "last4",
                     "account_type", "mapping_status")
    list_filter   = ("account_type", "mapping_status")
    search_fields = ("bank_name", "account_number", "last4", "client_name")


@admin.register(ParsedStatement)
class ParsedStatementAdmin(admin.ModelAdmin):
    list_display  = ("id", "session", "filename", "parse_status",
                     "bank_name_extracted", "account_number_extracted", "period_start", "period_end")
    list_filter   = ("parse_status",)
    search_fields = ("bank_name_extracted", "account_number_extracted")


@admin.register(LedgerTransaction)
class LedgerTransactionAdmin(admin.ModelAdmin):
    list_display  = ("id", "transaction_date", "bank_name", "account_number",
                     "description", "net_amount", "transaction_type",
                     "interbank_status", "interbank_confidence_score")
    list_filter   = ("transaction_type", "interbank_status")
    search_fields = ("description", "account_number", "bank_name", "reference_number")


@admin.register(InterbankMatch)
class InterbankMatchAdmin(admin.ModelAdmin):
    list_display  = ("match_id", "session", "match_date", "from_account",
                     "to_account", "amount", "confidence_score", "status")
    list_filter   = ("status",)
    search_fields = ("match_id", "from_account", "to_account")
