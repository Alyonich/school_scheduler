from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = 'scheduler'

urlpatterns = [
    # --- Аутентификация ---
    path(
        'login/',
        auth_views.LoginView.as_view(
            template_name='scheduler/login.html',
            redirect_authenticated_user=True,
        ),
        name='login',
    ),
    path(
        'logout/',
        auth_views.LogoutView.as_view(next_page='scheduler:login'),
        name='logout',
    ),

    # --- Основные экраны ---
    path('', views.dashboard, name='dashboard'),
    path('timetable/', views.timetable, name='timetable'),
    path('timetable/export/', views.timetable_export, name='timetable_export'),
    path('teachers/<int:pk>/', views.teacher_detail, name='teacher_detail'),
    path('generate/', views.start_generation, name='generate'),
    path('generate/jobs/<str:job_id>/', views.generation_progress, name='generation_progress'),
    path('generate/jobs/<str:job_id>/status/', views.generation_status, name='generation_status'),
    path('generate/jobs/<str:job_id>/events/', views.generation_events, name='generation_events'),
    path('lessons/new/', views.schedule_create, name='schedule_create'),
    path('lessons/<int:pk>/edit/', views.schedule_edit, name='schedule_edit'),
    path('lessons/<int:pk>/delete/', views.schedule_delete, name='schedule_delete'),
    path('conflicts/', views.schedule_conflicts, name='schedule_conflicts'),
    path(
        'conflicts/day/<int:class_id>/<str:lesson_date>/',
        views.conflict_day_view,
        name='conflict_day_view',
    ),
]
