from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.forms import AuthenticationForm
from .models import UserBankAccount


class SignupForm(forms.Form):
    full_name = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={'placeholder': 'Full Name', 'id': 'id_full_name'})
    )
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={'placeholder': 'Username', 'id': 'id_username'})
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={'placeholder': 'Email Address', 'id': 'id_email'})
    )
    password = forms.CharField(
        min_length=8,
        widget=forms.PasswordInput(attrs={'placeholder': 'Password (min. 8 characters)', 'id': 'id_password'})
    )
    confirm_password = forms.CharField(
        widget=forms.PasswordInput(attrs={'placeholder': 'Confirm Password', 'id': 'id_confirm_password'})
    )

    def clean_username(self):
        username = self.cleaned_data.get('username')
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("This username is already taken.")
        return username

    def clean_email(self):
        email = self.cleaned_data.get('email')
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        pw = cleaned_data.get('password')
        cpw = cleaned_data.get('confirm_password')
        if pw and cpw and pw != cpw:
            self.add_error('confirm_password', "Passwords do not match.")
        return cleaned_data


class LoginForm(forms.Form):
    username = forms.CharField(
        widget=forms.TextInput(attrs={'placeholder': 'Username', 'id': 'id_login_username'})
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={'placeholder': 'Password', 'id': 'id_login_password'})
    )


class BankAccountForm(forms.ModelForm):
    class Meta:
        model = UserBankAccount
        fields = ['account_holder_name', 'account_number', 'bank_name']
        widgets = {
            'account_holder_name': forms.TextInput(attrs={
                'placeholder': 'Account Holder Name', 'id': 'id_holder_name'
            }),
            'account_number': forms.TextInput(attrs={
                'placeholder': 'Account Number', 'id': 'id_account_number'
            }),
            'bank_name': forms.TextInput(attrs={
                'placeholder': 'Bank Name', 'id': 'id_bank_name'
            }),
        }
