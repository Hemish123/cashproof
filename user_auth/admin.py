from django.contrib import admin
from .models import UserProfile, UserBankAccount


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'user', 'get_email')
    search_fields = ('full_name', 'user__username', 'user__email')

    @admin.display(description='Email')
    def get_email(self, obj):
        return obj.user.email


@admin.register(UserBankAccount)
class UserBankAccountAdmin(admin.ModelAdmin):
    list_display = ('user', 'bank_name', 'account_number', 'account_holder_name', 'added_at')
    search_fields = ('user__username', 'bank_name', 'account_number')
    list_filter = ('bank_name',)
