from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, Http404
from django.contrib import messages
from django.db.models import Sum
from decimal import Decimal
import tempfile
import os

from .models import StatementUpload, BankAccount, Transaction
from .forms import StatementUploadForm
from .parsers.manager import process_statement
from .services import detect_interbank_transactions
from .reports import generate_excel_report

def dashboard_view(request):
    if request.method == 'POST':
        files = request.FILES.getlist('file')
        if files:
            # Clear previous uploads and associated media files for fresh analysis session
            for old_upload in StatementUpload.objects.all():
                if old_upload.file:
                    try:
                        if os.path.exists(old_upload.file.path):
                            os.remove(old_upload.file.path)
                    except Exception:
                        pass
                old_upload.delete()

            success_count = 0
            for f in files:
                upload = StatementUpload.objects.create(file=f)
                try:
                    process_statement(upload.id)
                    success_count += 1
                except Exception as e:
                    messages.error(request, f"Failed to parse {getattr(f, 'name', str(f))}: {str(e)}")
            
            # Recalculate interbank transactions for current batch
            detect_interbank_transactions()
            
            if success_count > 0:
                messages.success(request, f"Successfully uploaded and analyzed {success_count} statement(s)!")
            return redirect('dashboard')
        else:
            form = StatementUploadForm(request.POST, request.FILES)
    else:
        form = StatementUploadForm()

    uploads = StatementUpload.objects.all().order_by('-uploaded_at')
    accounts = BankAccount.objects.all().order_by('bank_name')
    transactions = Transaction.objects.all().order_by('-date', '-id')[:150]

    # Calculate global totals
    global_total_deposits = accounts.aggregate(total=Sum('total_deposits'))['total'] or Decimal('0.00')
    global_total_payments = accounts.aggregate(total=Sum('total_payments'))['total'] or Decimal('0.00')
    global_total_interbank_deposits = accounts.aggregate(total=Sum('total_interbank_deposits'))['total'] or Decimal('0.00')
    global_total_interbank_payments = accounts.aggregate(total=Sum('total_interbank_payments'))['total'] or Decimal('0.00')
    global_net_deposits = accounts.aggregate(total=Sum('net_deposits'))['total'] or Decimal('0.00')
    global_net_payments = accounts.aggregate(total=Sum('net_payments'))['total'] or Decimal('0.00')
    
    global_net_cash_flow = global_net_deposits - global_net_payments

    context = {
        'form': form,
        'uploads': uploads,
        'accounts': accounts,
        'transactions': transactions,
        'global_total_deposits': global_total_deposits,
        'global_total_payments': global_total_payments,
        'global_total_interbank_deposits': global_total_interbank_deposits,
        'global_total_interbank_payments': global_total_interbank_payments,
        'global_net_deposits': global_net_deposits,
        'global_net_payments': global_net_payments,
        'global_net_cash_flow': global_net_cash_flow,
    }
    return render(request, 'analyzer/dashboard.html', context)

def delete_upload_view(request, upload_id):
    try:
        upload = StatementUpload.objects.get(id=upload_id)
        filename = upload.filename()
        
        # Delete file from storage
        if upload.file:
            try:
                if os.path.exists(upload.file.path):
                    os.remove(upload.file.path)
            except Exception:
                pass
                
        upload.delete()
        
        # Trigger recalculation of interbank pairs
        detect_interbank_transactions()
        messages.success(request, f"Deleted statement {filename} and recalculated accounts.")
    except StatementUpload.DoesNotExist:
        messages.error(request, "Statement not found.")
        
    return redirect('dashboard')

def download_report_view(request):
    accounts = BankAccount.objects.all()
    if not accounts.exists():
        messages.warning(request, "No processed bank statements available to generate report.")
        return redirect('dashboard')

    # Create temporary file to save report
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
        response['Content-Disposition'] = 'attachment; filename="CashProof_Cashflow_Report.xlsx"'
        return response
    except Exception as e:
        messages.error(request, f"Error generating Excel report: {str(e)}")
        return redirect('dashboard')
    finally:
        # Cleanup temp file
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
