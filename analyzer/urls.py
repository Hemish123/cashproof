from django.urls import path
from . import views

urlpatterns = [
    path('projects/', views.project_list_view, name='project_list'),
    path('projects/workspace/', views.project_workspace_view, name='project_workspace'),
    path('projects/<int:project_id>/setup/', views.project_setup_view, name='project_setup'),
    path('projects/<int:project_id>/', views.project_detail_view, name='project_detail'),
    path('projects/<int:project_id>/delete/', views.delete_project_view, name='delete_project'),
    path('projects/<int:project_id>/status/', views.upload_status_api, name='upload_status'),
    path('projects/<int:project_id>/delete/<int:upload_id>/', views.delete_upload_view, name='delete_upload'),
    path('projects/<int:project_id>/download/', views.download_report_view, name='download_report'),
    path('projects/<int:project_id>/account/<int:account_id>/edit/', views.edit_account_view, name='edit_account'),
    path('projects/<int:project_id>/account/<int:account_id>/delete/', views.delete_account_view, name='delete_account'),
]

