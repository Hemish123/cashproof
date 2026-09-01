from django import forms
from .models import StatementUpload

class MultipleFileInput(forms.FileInput):
    allow_multiple_selected = True

class StatementUploadForm(forms.ModelForm):
    class Meta:
        model = StatementUpload
        fields = ['file']
        widgets = {
            'file': MultipleFileInput(attrs={
                'class': 'file-input',
                'accept': '.pdf,.csv,.xlsx,.xls',
                'id': 'file-upload-input',
                'multiple': True
            })
        }
