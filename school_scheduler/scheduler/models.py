from django.db import models
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError


class Class(models.Model):
    EDUCATION_LEVEL_CHOICES = [
        ('primary', 'начальное'),
        ('basic', 'основное'),
        ('high', 'среднее'),
    ]

    name = models.CharField(max_length=10, unique=True)
    students_count = models.PositiveIntegerField()
    education_level = models.CharField(
        max_length=20,
        choices=EDUCATION_LEVEL_CHOICES
    )

    subjects = models.ManyToManyField(
        'Subject',
        through='ClassSubject',
        related_name='classes'
    )

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = 'класс'
        verbose_name_plural = 'классы'


class User(AbstractUser):
    ROLE_CHOICES = [
        ('student', 'Ученик'),
        ('teacher', 'Преподаватель'),
        ('dispatcher', 'Диспетчер'),
        ('admin', 'Администратор'),
    ]

    class_obj = models.ForeignKey(
        'Class',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='users',
        verbose_name='класс'
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    full_name = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.full_name or self.username

    class Meta:
        verbose_name = 'пользователь'
        verbose_name_plural = 'пользователи'
        ordering = ['full_name', 'username']


class Subject(models.Model):
    ROOM_TYPE_CHOICES = [
        ('ordinary', 'обычный'),
        ('lab', 'лабораторный'),
        ('computer', 'компьютерный'),
    ]

    name = models.CharField(max_length=100, unique=True)
    required_room_type = models.CharField(
        max_length=50,
        choices=ROOM_TYPE_CHOICES,
        default='ordinary'
    )

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = 'предмет'
        verbose_name_plural = 'предметы'


class ClassSubject(models.Model):
    class_obj = models.ForeignKey(
        Class,
        on_delete=models.CASCADE,
        related_name='class_subjects'
    )
    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name='class_subjects'
    )
    weekly_hours = models.PositiveIntegerField()

    def __str__(self):
        return f"{self.class_obj} - {self.subject} ({self.weekly_hours} ч/нед)"

    class Meta:
        verbose_name = 'класс-предмет'
        verbose_name_plural = 'классы-предметы'
        constraints = [
            models.UniqueConstraint(
                fields=['class_obj', 'subject'],
                name='class_subjects_unique'
            )
        ]


class Teacher(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='teacher_profile'
    )
    qualification = models.CharField(max_length=100)
    workload_hours = models.PositiveIntegerField()

    def clean(self):
        if self.user and self.user.role != 'teacher':
            raise ValidationError(
                'Профиль преподавателя можно создать только для пользователя с ролью teacher.'
            )

    def __str__(self):
        return self.user.full_name or self.user.username

    class Meta:
        verbose_name = 'преподаватель'
        verbose_name_plural = 'преподаватели'


class Classroom(models.Model):
    ROOM_TYPE_CHOICES = [
        ('ordinary', 'обычный'),
        ('lab', 'лабораторный'),
        ('computer', 'компьютерный'),
    ]

    name = models.CharField(max_length=50, unique=True)
    capacity = models.PositiveIntegerField()
    room_type = models.CharField(
        max_length=50,
        choices=ROOM_TYPE_CHOICES
    )

    def __str__(self):
        return f"{self.name} ({self.room_type})"

    class Meta:
        verbose_name = 'кабинет'
        verbose_name_plural = 'кабинеты'
        ordering = ['name']


class LessonTime(models.Model):
    DAY_TYPE_CHOICES = [
        ('normal', 'обычный'),
        ('short', 'сокращённый')
    ]

    lesson_number = models.PositiveSmallIntegerField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    day_type = models.CharField(
        max_length=20,
        choices=DAY_TYPE_CHOICES
    )

    def __str__(self):
        return f"{self.lesson_number}-й урок: {self.start_time}–{self.end_time} ({self.day_type})"

    class Meta:
        verbose_name = 'время урока'
        verbose_name_plural = 'времена уроков'
        ordering = ['lesson_number', 'day_type']
        constraints = [
            models.UniqueConstraint(
                fields=['lesson_number', 'day_type'],
                name='unique_lesson_number_day_type'
            )
        ]


class Schedule(models.Model):
    class_obj = models.ForeignKey(
        Class,
        on_delete=models.CASCADE,
        related_name='schedules'
    )
    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name='schedules'
    )
    teacher = models.ForeignKey(
        Teacher,
        on_delete=models.CASCADE,
        related_name='schedules'
    )
    classroom = models.ForeignKey(
        Classroom,
        on_delete=models.CASCADE,
        related_name='schedules'
    )
    lesson_time = models.ForeignKey(
        LessonTime,
        on_delete=models.CASCADE,
        related_name='schedules'
    )
    lesson_date = models.DateField()

    def __str__(self):
        return f"{self.class_obj} - {self.subject} ({self.lesson_date})"

    class Meta:
        verbose_name = 'занятие'
        verbose_name_plural = 'занятия'
        constraints = [
            models.UniqueConstraint(
                fields=['class_obj', 'lesson_date', 'lesson_time'],
                name='unique_class_lesson_time_date'
            ),
            models.UniqueConstraint(
                fields=['teacher', 'lesson_date', 'lesson_time'],
                name='unique_teacher_lesson_time_date'
            ),
            models.UniqueConstraint(
                fields=['classroom', 'lesson_date', 'lesson_time'],
                name='unique_classroom_lesson_time_date'
            )
        ]


class ScheduleChange(models.Model):
    CHANGE_TYPE_CHOICES = [
        ('teacher_substitution', 'замена преподавателя'),
        ('reschedule', 'перенос'),
        ('cancel', 'отмена')
    ]

    schedule = models.ForeignKey(
        Schedule,
        on_delete=models.CASCADE,
        related_name='changes'
    )
    change_type = models.CharField(
        max_length=50,
        choices=CHANGE_TYPE_CHOICES
    )
    description = models.TextField()
    changed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Изм. {self.schedule} - {self.change_type}"

    class Meta:
        verbose_name = 'изменение расписания'
        verbose_name_plural = 'изменения расписания'
        ordering = ['-changed_at']


class IntegrationLog(models.Model):
    system_name = models.CharField(max_length=100)
    operation = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.system_name}: {self.operation[:20]}..."

    class Meta:
        verbose_name = 'лог интеграции'
        verbose_name_plural = 'логи интеграции'
        ordering = ['-created_at']


class TeachingAssignment(models.Model):
    teacher = models.ForeignKey(
        Teacher,
        on_delete=models.CASCADE,
        related_name='teaching_assignments'
    )
    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name='teaching_assignments'
    )
    class_obj = models.ForeignKey(
        Class,
        on_delete=models.CASCADE,
        related_name='teaching_assignments'
    )
    hours_per_week = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name='часов в неделю для этого преподавателя'
    )

    def clean(self):
        class_subject = ClassSubject.objects.filter(
            class_obj=self.class_obj,
            subject=self.subject
        ).first()

        if not class_subject:
            raise ValidationError(
                'Нельзя назначить преподавателя: предмет не привязан к данному классу.'
            )

        if self.hours_per_week is not None and self.hours_per_week > class_subject.weekly_hours:
            raise ValidationError(
                'Часы преподавателя не могут превышать общую недельную нагрузку по предмету у класса.'
            )

    def __str__(self):
        extra = f" ({self.hours_per_week} ч/нед)" if self.hours_per_week else ""
        return f"{self.teacher} → {self.subject} / {self.class_obj}{extra}"

    class Meta:
        verbose_name = 'назначение преподавателя'
        verbose_name_plural = 'назначения преподавателей'
        constraints = [
            models.UniqueConstraint(
                fields=['teacher', 'subject', 'class_obj'],
                name='unique_teaching_assignment'
            )
        ]


class TeacherAvailability(models.Model):
    WEEKDAY_CHOICES = [
        (1, 'Понедельник'),
        (2, 'Вторник'),
        (3, 'Среда'),
        (4, 'Четверг'),
        (5, 'Пятница'),
        (6, 'Суббота'),
    ]

    teacher = models.ForeignKey(
        Teacher,
        on_delete=models.CASCADE,
        related_name='availabilities'
    )
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAY_CHOICES)
    lesson_time = models.ForeignKey(
        LessonTime,
        on_delete=models.CASCADE,
        related_name='teacher_availabilities'
    )
    is_available = models.BooleanField(default=True)

    def __str__(self):
        weekday_display = dict(self.WEEKDAY_CHOICES).get(self.weekday, self.weekday)
        status = 'доступен' if self.is_available else 'недоступен'
        return f"{self.teacher} / {weekday_display} / {self.lesson_time} / {status}"

    class Meta:
        verbose_name = 'доступность преподавателя'
        verbose_name_plural = 'доступность преподавателей'
        constraints = [
            models.UniqueConstraint(
                fields=['teacher', 'weekday', 'lesson_time'],
                name='unique_teacher_weekday_lesson_time'
            )
        ]