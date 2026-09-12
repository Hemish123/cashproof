from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.messages import constants as msg_constants
from .models import UserProfile, UserBankAccount
from .forms import SignupForm, LoginForm, BankAccountForm


def signup_view(request):
    if request.user.is_authenticated:
        return redirect('bank_accounts') 

    form = SignupForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = User.objects.create_user(
            username=form.cleaned_data['username'],
            email=form.cleaned_data['email'],
            password=form.cleaned_data['password'],
        )
        UserProfile.objects.create(
            user=user,
            full_name=form.cleaned_data['full_name']
        )
        # Do NOT auto-login — send user to login page with a success prompt
        messages.success(request, f"Account created! Please sign in, {form.cleaned_data['full_name']}.")
        return redirect('login')

    return render(request, 'user_auth/signup.html', {'form': form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect('bank_accounts')

    form = LoginForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = authenticate(
            request,
            username=form.cleaned_data['username'],
            password=form.cleaned_data['password']
        )
        if user:
            login(request, user)
            messages.success(request, f"Welcome back, {user.username}!")
            return redirect('bank_accounts')  # Always go to bank accounts after login
        else:
            form.add_error(None, "Invalid username or password.")

    return render(request, 'user_auth/login.html', {'form': form})


def logout_view(request):
    logout(request)
    messages.info(request, "You have been logged out.")
    return redirect('login')


@login_required(login_url='login')
def bank_accounts_view(request):
    accounts = UserBankAccount.objects.filter(user=request.user)
    form = BankAccountForm()

    if request.method == 'POST':
        form = BankAccountForm(request.POST)
        if form.is_valid():
            bank_acc = form.save(commit=False)
            bank_acc.user = request.user
            bank_acc.save()
            messages.success(request, "Bank account added successfully!")
            return redirect('bank_accounts')

    return render(request, 'user_auth/bank_accounts.html', {
        'accounts': accounts,
        'form': form,
    })


@login_required(login_url='login')
def delete_bank_account_view(request, pk):
    account = get_object_or_404(UserBankAccount, pk=pk, user=request.user)
    if request.method == 'POST':
        account.delete()
        messages.success(request, "Bank account removed.")
    return redirect('bank_accounts')
