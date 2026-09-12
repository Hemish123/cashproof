from django.urls import path
from django.views.generic import RedirectView
from . import views

urlpatterns = [
    path('', RedirectView.as_view(pattern_name='login', permanent=False)),  # / → login
    path('signup/', views.signup_view, name='signup'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('bank-accounts/', views.bank_accounts_view, name='bank_accounts'),
    path('bank-accounts/delete/<int:pk>/', views.delete_bank_account_view, name='delete_bank_account'),
]
