from django.urls import path

from . import views

app_name = 'scheduler'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('timetable/', views.timetable, name='timetable'),
    path('generate/', views.start_generation, name='generate'),
    path('generate/jobs/<str:job_id>/', views.generation_progress, name='generation_progress'),
    path('generate/jobs/<str:job_id>/status/', views.generation_status, name='generation_status'),
    path('generate/jobs/<str:job_id>/events/', views.generation_events, name='generation_events'),
    path('lessons/new/', views.schedule_create, name='schedule_create'),
    path('lessons/<int:pk>/edit/', views.schedule_edit, name='schedule_edit'),
    path('lessons/<int:pk>/delete/', views.schedule_delete, name='schedule_delete'),
]
