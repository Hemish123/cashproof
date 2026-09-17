"""
cashproof_ai/models.py

Five models implementing the full §3 schema and §6 Match IDs from the
CashProof AI system prompt.

  CashProofSession   – one analysis run per user
  ClientAccount      – Client Account Master row (FK → Session)
  ParsedStatement    – one uploaded file (FK → Session)
  LedgerTransaction  – one ledger row per transaction (FK → ParsedStatement)
  InterbankMatch     – one row per confirmed/possible Match ID
"""

from django.db import models
from django.contrib.auth.models import User
import os


# ──────────────────────────────────────────────────────────────────────────────
# 1. CashProofSession
# ──────────────────────────────────────────────────────────────────────────────

class CashProofSession(models.Model):
    STATUS_CHOICES = [
        ("PENDING",    "Pending"),
        ("PROCESSING", "Processing"),
        ("COMPLETED",  "Completed"),
        ("FAILED",     "Failed"),
    ]

    user            = models.ForeignKey(User, null=True, blank=True,
                                        on_delete=models.SET_NULL,
                                        related_name="cashproof_sessions")
    client_name     = models.CharField(max_length=255, blank=True, default="")
    status          = models.CharField(max_length=20, choices=STATUS_CHOICES,
                                       default="PENDING")
    error_message   = models.TextField(null=True, blank=True)
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    # §11 final text report (stored after processing completes)
    final_text_report = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Session #{self.pk} — {self.client_name or 'Unknown Client'} ({self.status})"


# ──────────────────────────────────────────────────────────────────────────────
# 2. ClientAccount  (Client Account Master — §1)
# ──────────────────────────────────────────────────────────────────────────────

class ClientAccount(models.Model):
    ACCOUNT_TYPE_CHOICES = [
        ("checking",     "Checking"),
        ("payroll",      "Payroll"),
        ("savings",      "Savings"),
        ("money_market", "Money Market"),
        ("escrow",       "Escrow"),
        ("other",        "Other"),
    ]

    session        = models.ForeignKey(CashProofSession, on_delete=models.CASCADE,
                                       related_name="client_accounts")
    client_name    = models.CharField(max_length=255, blank=True, default="")
    bank_name      = models.CharField(max_length=255, blank=True, default="")
    account_name   = models.CharField(max_length=255, blank=True, default="",
                                      help_text='e.g. "Operating Account"')
    account_number = models.CharField(max_length=100, blank=True, default="")
    last4          = models.CharField(max_length=10,  blank=True, default="")
    account_type   = models.CharField(max_length=20, choices=ACCOUNT_TYPE_CHOICES,
                                      blank=True, default="checking")
    currency       = models.CharField(max_length=10, blank=True, default="USD")

    # Statement period covered by the uploaded document(s) for this account
    period_start   = models.DateField(null=True, blank=True)
    period_end     = models.DateField(null=True, blank=True)

    # If the document couldn't be mapped confidently
    mapping_status = models.CharField(max_length=50, blank=True, default="MAPPED",
                                      help_text='"MAPPED" or "Account Mapping Required"')

    # Per-account aggregates (computed by calculations.py)
    total_deposits           = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_payments           = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_interbank_deposits = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_interbank_payments = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    net_deposits             = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    net_payments             = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    class Meta:
        ordering = ["bank_name", "account_number"]

    def __str__(self):
        return f"{self.bank_name} …{self.last4} ({self.client_name})"


# ──────────────────────────────────────────────────────────────────────────────
# 3. ParsedStatement  (one uploaded file — §2)
# ──────────────────────────────────────────────────────────────────────────────

class ParsedStatement(models.Model):
    PARSE_STATUS_CHOICES = [
        ("PENDING",    "Pending"),
        ("PROCESSING", "Processing"),
        ("COMPLETED",  "Completed"),
        ("FAILED",     "Failed"),
    ]

    session         = models.ForeignKey(CashProofSession, on_delete=models.CASCADE,
                                        related_name="statements")
    client_account  = models.ForeignKey(ClientAccount, null=True, blank=True,
                                        on_delete=models.SET_NULL,
                                        related_name="statements",
                                        help_text="Matched ClientAccount; null = not yet mapped")
    file            = models.FileField(upload_to="cashproof_ai/statements/")
    parse_status    = models.CharField(max_length=20, choices=PARSE_STATUS_CHOICES,
                                       default="PENDING")
    error_message   = models.TextField(null=True, blank=True)
    uploaded_at     = models.DateTimeField(auto_now_add=True)

    # Extracted header fields (§2)
    client_name_extracted  = models.CharField(max_length=255, blank=True, default="")
    bank_name_extracted    = models.CharField(max_length=255, blank=True, default="")
    account_number_extracted = models.CharField(max_length=100, blank=True, default="")
    last4_extracted        = models.CharField(max_length=10,  blank=True, default="")
    account_title_extracted = models.CharField(max_length=255, blank=True, default="")
    account_type_extracted  = models.CharField(max_length=100, blank=True, default="")
    currency_extracted     = models.CharField(max_length=10,  blank=True, default="USD")
    period_start           = models.DateField(null=True, blank=True)
    period_end             = models.DateField(null=True, blank=True)
    beginning_balance      = models.DecimalField(max_digits=18, decimal_places=2,
                                                 null=True, blank=True)
    ending_balance         = models.DecimalField(max_digits=18, decimal_places=2,
                                                 null=True, blank=True)
    statement_total_credits = models.DecimalField(max_digits=18, decimal_places=2,
                                                   null=True, blank=True)
    statement_total_debits  = models.DecimalField(max_digits=18, decimal_places=2,
                                                   null=True, blank=True)

    def filename(self):
        return os.path.basename(self.file.name)

    def __str__(self):
        return f"Statement #{self.pk} ({self.filename()}) [{self.parse_status}]"


# ──────────────────────────────────────────────────────────────────────────────
# 4. LedgerTransaction  (§3 full schema)
# ──────────────────────────────────────────────────────────────────────────────

class LedgerTransaction(models.Model):
    # Transaction type codes from §4
    TX_TYPE_CHOICES = [
        ("A", "External Deposit"),
        ("B", "External Payment"),
        ("C", "Interbank / Self-Transfer"),
        ("D", "Interest"),
        ("E", "Bank Fee / Charge"),
        ("F", "Check"),
        ("G", "ACH"),
        ("H", "Card / Merchant Settlement"),
        ("I", "Other"),
    ]

    INTERBANK_STATUS_CHOICES = [
        ("CONFIRMED_INTERBANK",        "Confirmed Interbank"),
        ("HIGH_PROBABILITY_INTERBANK", "High-Probability Interbank"),
        ("POSSIBLE_INTERBANK",         "Possible Interbank"),
        ("POSSIBLE_UNMATCHED",         "Possible / Unmatched — Review Required"),
        ("NOT_INTERBANK",              "Not Interbank"),
        ("EXCLUDED_FEE_INTEREST",      "Excluded — Fee/Interest"),
        ("EXCLUDED_CUSTOMER_ACH",      "Excluded — Customer ACH"),
        ("AMBIGUOUS",                  "Ambiguous — Multiple Candidates"),
    ]

    # Source
    statement    = models.ForeignKey(ParsedStatement, on_delete=models.CASCADE,
                                     related_name="transactions")
    client_account = models.ForeignKey(ClientAccount, null=True, blank=True,
                                       on_delete=models.SET_NULL,
                                       related_name="transactions")

    # §3 schema fields
    client_name     = models.CharField(max_length=255, blank=True, default="")
    bank_name       = models.CharField(max_length=255, blank=True, default="")
    account_number  = models.CharField(max_length=100, blank=True, default="")
    last4           = models.CharField(max_length=10,  blank=True, default="")
    account_title   = models.CharField(max_length=255, blank=True, default="")
    statement_period = models.CharField(max_length=100, blank=True, default="")

    transaction_date  = models.DateField()
    value_date        = models.DateField(null=True, blank=True)
    description       = models.TextField(blank=True, default="")
    reference_number  = models.CharField(max_length=255, blank=True, default="")
    check_number      = models.CharField(max_length=50,  blank=True, default="")

    debit_amount   = models.DecimalField(max_digits=18, decimal_places=2,
                                         null=True, blank=True)
    credit_amount  = models.DecimalField(max_digits=18, decimal_places=2,
                                         null=True, blank=True)

    # Signed net amount: positive = credit, negative = debit
    net_amount     = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    transaction_type    = models.CharField(max_length=2, choices=TX_TYPE_CHOICES,
                                           blank=True, default="I")
    counterparty_info   = models.CharField(max_length=500, blank=True, default="")
    source_file         = models.CharField(max_length=500, blank=True, default="")
    source_page         = models.CharField(max_length=20,  blank=True, default="")

    # Interbank fields
    interbank_status          = models.CharField(max_length=50,
                                                  choices=INTERBANK_STATUS_CHOICES,
                                                  default="NOT_INTERBANK")
    interbank_confidence_score = models.IntegerField(null=True, blank=True,
                                                      help_text="0–100 per §6 Step 3")
    interbank_matched_account  = models.CharField(max_length=255, blank=True, default="")
    interbank_match            = models.ForeignKey("InterbankMatch", null=True, blank=True,
                                                    on_delete=models.SET_NULL,
                                                    related_name="transactions")
    reason_for_classification  = models.TextField(blank=True, default="")

    # Deduplication fingerprint (§7)
    fingerprint = models.CharField(max_length=64, blank=True, default="",
                                   db_index=True)

    class Meta:
        ordering = ["-transaction_date", "-id"]

    def __str__(self):
        direction = "CR" if (self.credit_amount or 0) > 0 else "DR"
        amt = self.credit_amount or self.debit_amount or 0
        return f"[{self.transaction_date}] {self.bank_name} {direction} {amt}"


# ──────────────────────────────────────────────────────────────────────────────
# 5. InterbankMatch  (§6 Step 6 — one row per Match ID)
# ──────────────────────────────────────────────────────────────────────────────

class InterbankMatch(models.Model):
    MATCH_STATUS_CHOICES = [
        ("CONFIRMED_INTERBANK",    "Confirmed Interbank"),
        ("HIGH_PROBABILITY",       "High-Probability Interbank"),
        ("POSSIBLE_INTERBANK",     "Possible Interbank"),
        ("POSSIBLE_UNMATCHED",     "Possible / Unmatched — Review Required"),
        ("AMBIGUOUS",              "Ambiguous — Multiple Candidates"),
    ]

    session        = models.ForeignKey(CashProofSession, on_delete=models.CASCADE,
                                       related_name="interbank_matches")
    match_id       = models.CharField(max_length=20, blank=True, default="",
                                      help_text="e.g. IB-001")
    match_date     = models.DateField(null=True, blank=True)
    client_name    = models.CharField(max_length=255, blank=True, default="")

    from_bank      = models.CharField(max_length=255, blank=True, default="")
    from_account   = models.CharField(max_length=100, blank=True, default="")
    to_bank        = models.CharField(max_length=255, blank=True, default="")
    to_account     = models.CharField(max_length=100, blank=True, default="")

    amount              = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    debit_description   = models.TextField(blank=True, default="")
    credit_description  = models.TextField(blank=True, default="")
    match_type          = models.CharField(max_length=100, blank=True, default="")
    confidence_score    = models.IntegerField(default=0)
    status              = models.CharField(max_length=50, choices=MATCH_STATUS_CHOICES,
                                           default="CONFIRMED_INTERBANK")
    reason              = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["match_id"]

    def __str__(self):
        return f"{self.match_id} — {self.from_account} → {self.to_account} (${self.amount})"
