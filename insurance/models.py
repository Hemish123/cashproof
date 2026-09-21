from django.db import models

class InsuranceAnalysis(models.Model):
    file_name = models.CharField(max_length=255)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    summary = models.TextField()
    insights = models.JSONField(default=list)
    metrics = models.JSONField(default=list)
    chart_data = models.JSONField(default=dict)

    def __str__(self):
        return f"{self.file_name} - {self.uploaded_at.strftime('%Y-%m-%d %H:%M')}"
