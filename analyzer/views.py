from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, Http404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from decimal import Decimal
import tempfile
import os
import threading
from django.db import connections
from django.http import JsonResponse
from django.contrib.auth.models import User
from django.conf import settings
from .models import StatementUpload, BankAccount, Transaction, Project
from .forms import StatementUploadForm, ProjectForm, BankAccountForm
from .parsers.manager import process_statement
from .services import detect_interbank_transactions
from .reports import generate_excel_report
import concurrent.futures

def process_batch_in_background(upload_ids, project_id=None):
    try:
        

        def _process_one(uid):
            try:
                upload = StatementUpload.objects.get(id=uid)
                upload.status = 'PROCESSING'
                upload.save(update_fields=['status'])

                process_statement(upload.id, project_id=project_id)

                upload.status = 'COMPLETED'
                upload.save(update_fields=['status'])
            except Exception as e:
                try:
                    upload = StatementUpload.objects.get(id=uid)
                    upload.status = 'FAILED'
                    upload.error_message = str(e)
                    upload.save(update_fields=['status', 'error_message'])
                except Exception:
                    pass
            finally:
                connections.close_all()

        max_concurrent_files = getattr(settings, 'MAX_CONCURRENT_FILES', 1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent_files) as executor:
            list(executor.map(_process_one, upload_ids))

        # Recalculate interbank pairs after all are processed
        if not project_id and upload_ids:
            project_id = StatementUpload.objects.get(id=upload_ids[0]).project_id
            
        detect_interbank_transactions(project_id=project_id)
    finally:
        connections.close_all()


@login_required(login_url='login')
def project_list_view(request):
    from django.db.models import Prefetch
    projects = Project.objects.filter(user=request.user).order_by('-created_at').prefetch_related(
        Prefetch('bank_accounts', queryset=BankAccount.objects.filter(is_manual=True))
    )
    
    if request.method == 'POST':
        form = ProjectForm(request.POST)
        if form.is_valid():
            project = form.save(commit=False)
            project.user = request.user
            project.save()
            messages.success(request, f"Project '{project.name}' created successfully.")
            return redirect('project_setup', project_id=project.id)
    else:
        form = ProjectForm()

    total_accounts = sum(len(p.bank_accounts.all()) for p in projects)
    no_account_projects = sum(1 for p in projects if len(p.bank_accounts.all()) == 0)

    return render(request, 'analyzer/project_list.html', {
        'projects': projects,
        'form': form,
        'total_accounts': total_accounts,
        'no_account_projects': no_account_projects,
    })


@login_required(login_url='login')
def project_workspace_view(request):
    from django.db.models import Prefetch
    projects = Project.objects.filter(user=request.user).order_by('-created_at').prefetch_related(
        Prefetch('bank_accounts', queryset=BankAccount.objects.filter(is_manual=True))
    )

    if request.method == 'POST':
        form = ProjectForm(request.POST)
        if form.is_valid():
            project = form.save(commit=False)
            project.user = request.user
            project.save()
            messages.success(request, f"Project '{project.name}' created successfully.")
            return redirect('project_setup', project_id=project.id)
    else:
        form = ProjectForm()

    return render(request, 'analyzer/project_workspace.html', {
        'projects': projects,
        'form': form,
    })

@login_required(login_url='login')
def project_setup_view(request, project_id):
    project = get_object_or_404(Project, id=project_id, user=request.user)
    account_form = BankAccountForm()
    
    if request.method == 'POST':
        if 'add_account' in request.POST:
            account_form = BankAccountForm(request.POST)
            if account_form.is_valid():
                account = account_form.save(commit=False)
                account.project = project
                account.is_manual = True
                account.save()
                messages.success(request, "Bank account registered successfully.")
                return redirect('project_setup', project_id=project.id)
        elif 'finish_setup' in request.POST:
            if BankAccount.objects.filter(project=project, is_manual=True).exists():
                return redirect('project_detail', project_id=project.id)
            else:
                messages.error(request, "You must add at least one bank account to continue to the analyzer.")
    accounts = BankAccount.objects.filter(project=project, is_manual=True).order_by('bank_name')
    
    return render(request, 'analyzer/project_setup.html', {
        'project': project,
        'account_form': account_form,
        'accounts': accounts
    })

@login_required(login_url='login')
def project_detail_view(request, project_id):
    project = get_object_or_404(Project, id=project_id, user=request.user)

    if request.method == 'POST':
        if 'update_project' in request.POST:
            form = ProjectForm(request.POST, instance=project)
            if form.is_valid():
                form.save()
                messages.success(request, f"Project '{project.name}' updated successfully.")
            else:
                messages.error(request, "Error updating project.")
            return redirect('project_detail', project_id=project.id)
        elif 'add_account' in request.POST:
            account_form = BankAccountForm(request.POST)
            if account_form.is_valid():
                account = account_form.save(commit=False)
                account.project = project
                account.is_manual = True
                account.save()
                messages.success(request, "Bank account registered successfully.")
                return redirect('project_detail', project_id=project.id)
            else:
                messages.error(request, "Error adding bank account.")
        elif 'upload_statements' in request.POST:
            upload_form = StatementUploadForm(request.POST, request.FILES)
            if upload_form.is_valid():
                files = request.FILES.getlist('file')
                if files:
                    upload_ids = []
                    for f in files:
                        upload = StatementUpload.objects.create(file=f, project=project, user=request.user, status='PENDING')
                        upload_ids.append(upload.id)
                    
                    if upload_ids:
                        threading.Thread(
                            target=process_batch_in_background,
                            args=(upload_ids, project.id)
                        ).start()
                        messages.success(request, f"Started processing {len(upload_ids)} statement(s) for this project.")
                return redirect('project_detail', project_id=project.id)
            else:
                messages.error(request, f"Form is invalid: {upload_form.errors}")
    
    account_form = BankAccountForm()
    upload_form = StatementUploadForm(initial={'project': project})
    upload_form.fields['project'].widget = upload_form.fields['project'].hidden_widget()

    uploads = StatementUpload.objects.filter(project=project).order_by('-uploaded_at')
    manual_accounts = BankAccount.objects.filter(project=project, is_manual=True).order_by('bank_name')
    raw_accounts = BankAccount.objects.filter(project=project, upload__isnull=False).order_by('bank_name')
    all_transactions = Transaction.objects.filter(account__project=project).order_by('-date', '-id')
    interbank_transactions = all_transactions.filter(is_interbank=True)
    regular_transactions = all_transactions.filter(is_interbank=False)

    aggregated_accounts = {}
    for acc in raw_accounts:
        key = (acc.bank_name, acc.account_number)
        if key not in aggregated_accounts:
            aggregated_accounts[key] = {
                'bank_name': acc.bank_name,
                'account_number': acc.account_number,
                'account_holder': acc.account_holder,
                'start_date': acc.start_date,
                'end_date': acc.end_date,
                'total_deposits': Decimal('0.00'),
                'total_payments': Decimal('0.00'),
                'total_interbank_deposits': Decimal('0.00'),
                'total_interbank_payments': Decimal('0.00'),
                'net_deposits': Decimal('0.00'),
                'net_payments': Decimal('0.00'),
            }
        grp = aggregated_accounts[key]
        grp['total_deposits'] += acc.total_deposits
        grp['total_payments'] += acc.total_payments
        grp['total_interbank_deposits'] += acc.total_interbank_deposits
        grp['total_interbank_payments'] += acc.total_interbank_payments
        grp['net_deposits'] += acc.net_deposits
        grp['net_payments'] += acc.net_payments
        if acc.start_date and (not grp['start_date'] or acc.start_date < grp['start_date']):
            grp['start_date'] = acc.start_date
        if acc.end_date and (not grp['end_date'] or acc.end_date > grp['end_date']):
            grp['end_date'] = acc.end_date

    accounts = sorted(aggregated_accounts.values(), key=lambda x: x['bank_name'])

    global_total_deposits = raw_accounts.aggregate(total=Sum('total_deposits'))['total'] or Decimal('0.00')
    global_total_payments = raw_accounts.aggregate(total=Sum('total_payments'))['total'] or Decimal('0.00')
    global_total_interbank_deposits = raw_accounts.aggregate(total=Sum('total_interbank_deposits'))['total'] or Decimal('0.00')
    global_total_interbank_payments = raw_accounts.aggregate(total=Sum('total_interbank_payments'))['total'] or Decimal('0.00')
    global_net_deposits = raw_accounts.aggregate(total=Sum('net_deposits'))['total'] or Decimal('0.00')
    global_net_payments = raw_accounts.aggregate(total=Sum('net_payments'))['total'] or Decimal('0.00')
    global_net_cash_flow = global_net_deposits - global_net_payments

    # Monthly cash flow for chart
    import json as _json
    from django.db.models.functions import TruncMonth
    from django.db.models import Q as DQ
    monthly_qs = regular_transactions.annotate(
        month=TruncMonth('date')
    ).values('month').annotate(
        inflow=Sum('amount', filter=DQ(amount__gt=0)),
        outflow=Sum('amount', filter=DQ(amount__lt=0))
    ).order_by('month')

    monthly_labels = []
    monthly_inflow = []
    monthly_outflow = []
    for row in monthly_qs:
        if row['month']:
            monthly_labels.append(row['month'].strftime('%b %Y'))
            monthly_inflow.append(float(row['inflow'] or 0))
            monthly_outflow.append(float(abs(row['outflow'] or 0)))

    interbank_count = interbank_transactions.count()
    failed_uploads = uploads.filter(status='FAILED')
    exceptions_count = failed_uploads.count()

    context = {
        'project': project,
        'account_form': account_form,
        'upload_form': upload_form,
        'uploads': uploads,
        'manual_accounts': manual_accounts,
        'accounts': accounts,
        'all_transactions': all_transactions[:500],
        'interbank_transactions': interbank_transactions[:300],
        'failed_uploads': failed_uploads,
        'global_total_deposits': global_total_deposits,
        'global_total_payments': global_total_payments,
        'global_total_interbank_deposits': global_total_interbank_deposits,
        'global_total_interbank_payments': global_total_interbank_payments,
        'global_net_deposits': global_net_deposits,
        'global_net_payments': global_net_payments,
        'global_net_cash_flow': global_net_cash_flow,
        'monthly_labels': _json.dumps(monthly_labels),
        'monthly_inflow': _json.dumps(monthly_inflow),
        'monthly_outflow': _json.dumps(monthly_outflow),
        'interbank_count': interbank_count,
        'exceptions_count': exceptions_count,
        'uploads_count': uploads.count(),
    }
    return render(request, 'analyzer/project_detail.html', context)



@login_required(login_url='login')
def delete_project_view(request, project_id):
    project = get_object_or_404(Project, id=project_id, user=request.user)
    
    if request.method == 'POST':
        project_name = project.name
        project.delete()
        messages.success(request, f"Project '{project_name}' deleted successfully.")
        
    referer = request.META.get('HTTP_REFERER')
    if referer:
        return redirect(referer)
    return redirect('project_list')


@login_required(login_url='login')
def delete_upload_view(request, project_id, upload_id):
    try:
        project = get_object_or_404(Project, id=project_id, user=request.user)
        upload = StatementUpload.objects.get(id=upload_id, project=project)
        filename = upload.filename()

        if upload.file:
            try:
                if os.path.exists(upload.file.path):
                    os.remove(upload.file.path)
            except Exception:
                pass

        upload.delete()

        detect_interbank_transactions(project_id=project.id)
        messages.success(request, f"Deleted statement {filename} and recalculated accounts.")
    except StatementUpload.DoesNotExist:
        messages.error(request, "Statement not found.")

    return redirect('project_detail', project_id=project_id)
@login_required(login_url='login')
def delete_account_view(request, project_id, account_id):
    project = get_object_or_404(Project, id=project_id, user=request.user)
    account = get_object_or_404(BankAccount, id=account_id, project=project)
    
    if request.method == 'POST':
        account.delete()
        detect_interbank_transactions(project_id=project.id)
        messages.success(request, "Bank account deleted successfully.")
        
    return redirect('project_setup', project_id=project_id)

@login_required(login_url='login')
def edit_account_view(request, project_id, account_id):
    project = get_object_or_404(Project, id=project_id, user=request.user)
    account = get_object_or_404(BankAccount, id=account_id, project=project)
    
    if request.method == 'POST':
        form = BankAccountForm(request.POST, instance=account)
        if form.is_valid():
            form.save()
            detect_interbank_transactions(project_id=project.id)
            messages.success(request, "Bank account updated successfully.")
        else:
            messages.error(request, "Error updating bank account.")
            
    return redirect('project_setup', project_id=project_id)


@login_required(login_url='login')
def download_report_view(request, project_id):
    project = get_object_or_404(Project, id=project_id, user=request.user)
    accounts = BankAccount.objects.filter(project=project, upload__isnull=False).order_by('bank_name')

    if not accounts.exists():
        messages.warning(request, "No processed bank statements available to generate report.")
        return redirect('project_detail', project_id=project_id)

    with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp:
        tmp_path = tmp.name

    try:
        generate_excel_report(accounts, tmp_path)
        with open(tmp_path, 'rb') as f:
            file_data = f.read()

        response = HttpResponse(
            file_data,
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename="{project.name}_Cashflow_Report.xlsx"'
        return response
    except Exception as e:
        messages.error(request, f"Error generating Excel report: {str(e)}")
        return redirect('project_detail', project_id=project_id)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


@login_required(login_url='login')
def upload_status_api(request, project_id):
    project = get_object_or_404(Project, id=project_id, user=request.user)
    is_processing = StatementUpload.objects.filter(
        project=project,
        status__in=['PENDING', 'PROCESSING']
    ).exists()
    return JsonResponse({'is_processing': is_processing})

@login_required(login_url='login')
def bank_account_list_view(request):
    projects = Project.objects.filter(user=request.user)
    
    if request.method == 'POST':
        if 'add_account' in request.POST:
            project_id = request.POST.get('project_id')
            if not project_id:
                messages.error(request, "You must select a project.")
                return redirect('bank_account_list')
                
            project = get_object_or_404(Project, id=project_id, user=request.user)
            form = BankAccountForm(request.POST)
            if form.is_valid():
                account = form.save(commit=False)
                account.project = project
                account.is_manual = True
                account.save()
                messages.success(request, "Bank account added successfully.")
            else:
                messages.error(request, "Error adding bank account.")
            return redirect('bank_account_list')
            
        elif 'edit_account' in request.POST:
            account_id = request.POST.get('account_id')
            account = get_object_or_404(BankAccount, id=account_id, project__user=request.user)
            form = BankAccountForm(request.POST, instance=account)
            if form.is_valid():
                form.save()
                messages.success(request, "Bank account updated successfully.")
            else:
                messages.error(request, "Error updating bank account.")
            return redirect('bank_account_list')
            
        elif 'delete_account' in request.POST:
            account_id = request.POST.get('account_id')
            account = get_object_or_404(BankAccount, id=account_id, project__user=request.user)
            account.delete()
            messages.success(request, "Bank account deleted successfully.")
            return redirect('bank_account_list')

    accounts = BankAccount.objects.filter(project__user=request.user, is_manual=True).select_related('project').order_by('bank_name')
    
    return render(request, 'analyzer/bank_account_list.html', {
        'accounts': accounts,
        'projects': projects,
    })

