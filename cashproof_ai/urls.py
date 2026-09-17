from django.urls import path
from . import views

app_name = 'cashproof_ai'

urlpatterns = [
    path('',                              views.session_list,   name='session_list'),
    path('new/',                          views.new_session,    name='new_session'),
    path('<int:session_id>/',             views.session_detail, name='session_detail'),
    path('<int:session_id>/status/',      views.session_status_api, name='session_status_api'),
    path('<int:session_id>/report/',      views.download_report,    name='download_report'),
    path('<int:session_id>/delete/',      views.delete_session,     name='delete_session'),
]
