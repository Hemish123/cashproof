from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard_view, name='dashboard'),
    path('status/', views.upload_status_api, name='upload_status'),
    path('delete/<int:upload_id>/', views.delete_upload_view, name='delete_upload'),
    path('download/', views.download_report_view, name='download_report'),
]
