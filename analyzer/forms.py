from django import forms
from .models import StatementUpload, Project

class MultipleFileInput(forms.FileInput):
    allow_multiple_selected = True

class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            result = [single_file_clean(d, initial) for d in data]
        else:
            result = single_file_clean(data, initial)
        return result

class StatementUploadForm(forms.ModelForm):
    file = MultipleFileField(
        widget=MultipleFileInput(attrs={
            'class': 'file-input',
            'accept': '.pdf,.csv,.xlsx,.xls',
            'id': 'file-upload-input',
            'multiple': True
        })
    )

    class Meta:
        model = StatementUpload
        fields = ['project', 'file']
    
    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super(StatementUploadForm, self).__init__(*args, **kwargs)
        if user:
            from .models import Project
            self.fields['project'].queryset = Project.objects.filter(user=user)
        # Add basic class to project select
        self.fields['project'].widget.attrs.update({'class': 'form-select', 'style': 'width: 100%; padding: 0.5rem; margin-bottom: 1rem; border: 1px solid var(--border-card); border-radius: 6px;'})


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ['name', 'client_name']
        widgets = {
            'name': forms.TextInput(attrs={
                'placeholder': 'Project Name (e.g. FY25 Due Diligence)',
                'class': 'form-control',
                'style': 'width: 100%; padding: 0.5rem; margin-bottom: 1rem; border: 1px solid var(--border-card); border-radius: 6px;'
            }),
            'client_name': forms.TextInput(attrs={
                'placeholder': 'Client Name (e.g. Meridian Capital)',
                'class': 'form-control',
                'style': 'width: 100%; padding: 0.5rem; margin-bottom: 1rem; border: 1px solid var(--border-card); border-radius: 6px;'
            }),
        }

class BankAccountForm(forms.ModelForm):
    class Meta:
        from .models import BankAccount
        model = BankAccount
        fields = ['bank_name', 'account_number', 'account_holder']
        widgets = {
            'bank_name': forms.TextInput(attrs={
                'placeholder': 'Bank Name (e.g. QNB)',
                'class': 'form-control',
                'style': 'width: 100%; padding: 0.5rem; margin-bottom: 1rem; border: 1px solid var(--border-card); border-radius: 6px;'
            }),
            'account_number': forms.TextInput(attrs={
                'placeholder': 'Account Number',
                'class': 'form-control',
                'style': 'width: 100%; padding: 0.5rem; margin-bottom: 1rem; border: 1px solid var(--border-card); border-radius: 6px;'
            }),
            'account_holder': forms.TextInput(attrs={
                'placeholder': 'Account Holder Name',
                'class': 'form-control',
                'style': 'width: 100%; padding: 0.5rem; margin-bottom: 1rem; border: 1px solid var(--border-card); border-radius: 6px;'
            }),
        }
