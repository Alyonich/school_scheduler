from django.urls import path

from . import views

app_name = 'scheduler'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('timetable/', views.timetable, name='timetable'),
    path('generate/', views.generate_timetable, name='generate'),
    path('lessons/new/', views.schedule_create, name='schedule_create'),
    path('lessons/<int:pk>/edit/', views.schedule_edit, name='schedule_edit'),
    path('lessons/<int:pk>/delete/', views.schedule_delete, name='schedule_delete'),
]
