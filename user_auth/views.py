from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.messages import constants as msg_constants
from .models import UserProfile
from .forms import SignupForm, LoginForm


def signup_view(request):
    if request.user.is_authenticated:
        return redirect('project_list') 

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
        messages.success(request, f"Account created! Please sign in, {form.cleaned_data['full_name']}.")
        return redirect('login')

    return render(request, 'user_auth/signup.html', {'form': form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect('project_list')

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
            return redirect('project_list')
        else:
            form.add_error(None, "Invalid username or password.")

    return render(request, 'user_auth/login.html', {'form': form})


def logout_view(request):
    logout(request)
    messages.info(request, "You have been logged out.")
    return redirect('login')
