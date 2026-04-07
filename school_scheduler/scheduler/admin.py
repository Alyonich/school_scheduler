from django.contrib import admin
from .models import (
    Class,
    User,
    Subject,
    ClassSubject,
    Teacher,
    Classroom,
    LessonTime,
    TimeSlot,
    TeachingAssignment,
    TeacherAvailability,
    Schedule,
    ScheduleChange,
    IntegrationLog,
)


class ClassSubjectInline(admin.TabularInline):
    model = ClassSubject
    extra = 1


class TeachingAssignmentInline(admin.TabularInline):
    model = TeachingAssignment
    extra = 1


class TeacherAvailabilityInline(admin.TabularInline):
    model = TeacherAvailability
    extra = 1


class ScheduleChangeInline(admin.TabularInline):
    model = ScheduleChange
    extra = 0
    readonly_fields = ('changed_at',)


@admin.register(Class)
class ClassAdmin(admin.ModelAdmin):
    list_display = ('name', 'students_count', 'education_level')
    search_fields = ('name',)
    list_filter = ('education_level',)
    inlines = [ClassSubjectInline, TeachingAssignmentInline]


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('username', 'full_name', 'role', 'class_obj', 'is_staff', 'is_active')
    search_fields = ('username', 'full_name', 'email')
    list_filter = ('role', 'is_staff', 'is_active')
    autocomplete_fields = ('class_obj',)


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'required_room_type')
    search_fields = ('name',)
    list_filter = ('required_room_type',)


@admin.register(ClassSubject)
class ClassSubjectAdmin(admin.ModelAdmin):
    list_display = ('class_obj', 'subject', 'weekly_hours')
    search_fields = ('class_obj__name', 'subject__name')
    list_filter = ('class_obj', 'subject')


@admin.register(Teacher)
class TeacherAdmin(admin.ModelAdmin):
    list_display = ('user', 'qualification', 'workload_hours')
    search_fields = ('user__username', 'user__full_name', 'qualification')
    list_filter = ('qualification',)
    autocomplete_fields = ('user',)
    inlines = [TeachingAssignmentInline, TeacherAvailabilityInline]


@admin.register(Classroom)
class ClassroomAdmin(admin.ModelAdmin):
    list_display = ('name', 'capacity', 'room_type')
    search_fields = ('name',)
    list_filter = ('room_type',)


@admin.register(LessonTime)
class LessonTimeAdmin(admin.ModelAdmin):
    list_display = ('lesson_number', 'start_time', 'end_time', 'day_type')
    list_filter = ('day_type',)
    ordering = ('day_type', 'lesson_number')


@admin.register(TimeSlot)
class TimeSlotAdmin(admin.ModelAdmin):
    list_display = ('weekday', 'lesson_time')
    list_filter = ('weekday', 'lesson_time__day_type')
    autocomplete_fields = ('lesson_time',)


@admin.register(TeachingAssignment)
class TeachingAssignmentAdmin(admin.ModelAdmin):
    list_display = ('teacher', 'subject', 'class_obj', 'hours_per_week')
    search_fields = (
        'teacher__user__username',
        'teacher__user__full_name',
        'subject__name',
        'class_obj__name',
    )
    list_filter = ('subject', 'class_obj')
    autocomplete_fields = ('teacher', 'subject', 'class_obj')


@admin.register(TeacherAvailability)
class TeacherAvailabilityAdmin(admin.ModelAdmin):
    list_display = ('teacher', 'time_slot', 'is_available')
    list_filter = ('is_available', 'time_slot__weekday')
    search_fields = ('teacher__user__username', 'teacher__user__full_name')
    autocomplete_fields = ('teacher', 'time_slot')


@admin.register(Schedule)
class ScheduleAdmin(admin.ModelAdmin):
    list_display = ('class_obj', 'subject', 'teacher', 'classroom', 'lesson_date', 'time_slot')
    search_fields = (
        'class_obj__name',
        'subject__name',
        'teacher__user__username',
        'teacher__user__full_name',
        'classroom__name',
    )
    list_filter = ('lesson_date', 'subject', 'class_obj')
    autocomplete_fields = ('class_obj', 'subject', 'teacher', 'classroom', 'time_slot')
    inlines = [ScheduleChangeInline]


@admin.register(ScheduleChange)
class ScheduleChangeAdmin(admin.ModelAdmin):
    list_display = ('schedule', 'change_type', 'changed_at')
    list_filter = ('change_type', 'changed_at')
    search_fields = (
        'schedule__class_obj__name',
        'schedule__subject__name',
        'description',
    )
    autocomplete_fields = ('schedule',)
    readonly_fields = ('changed_at',)


@admin.register(IntegrationLog)
class IntegrationLogAdmin(admin.ModelAdmin):
    list_display = ('system_name', 'operation', 'created_at')
    search_fields = ('system_name', 'operation')
    readonly_fields = ('created_at',)