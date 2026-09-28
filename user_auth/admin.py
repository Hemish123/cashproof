from django.contrib import admin
from .models import UserProfile


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'user', 'get_email')
    search_fields = ('full_name', 'user__username', 'user__email')

    @admin.display(description='Email')
    def get_email(self, obj):
        return obj.user.email
