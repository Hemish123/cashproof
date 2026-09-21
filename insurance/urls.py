from django.urls import path
from . import views

urlpatterns = [
    path('upload/', views.upload_file, name='insurance_upload'),
    path('dashboard/<int:analysis_id>/', views.dashboard, name='insurance_dashboard'),
]
