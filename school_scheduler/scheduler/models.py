from django.db import models
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db.models import Sum


class EducationLevel(models.TextChoices):
    PRIMARY = 'primary', 'начальное'
    BASIC = 'basic', 'основное'
    HIGH = 'high', 'среднее'


class UserRole(models.TextChoices):
    STUDENT = 'student', 'Ученик'
    TEACHER = 'teacher', 'Преподаватель'
    DISPATCHER = 'dispatcher', 'Диспетчер'
    ADMIN = 'admin', 'Администратор'


class RoomType(models.TextChoices):
    ORDINARY = 'ordinary', 'обычный'
    LAB = 'lab', 'лабораторный'
    COMPUTER = 'computer', 'компьютерный'


class DayType(models.TextChoices):
    NORMAL = 'normal', 'обычный'
    SHORT = 'short', 'сокращённый'


class Weekday(models.IntegerChoices):
    MONDAY = 1, 'Понедельник'
    TUESDAY = 2, 'Вторник'
    WEDNESDAY = 3, 'Среда'
    THURSDAY = 4, 'Четверг'
    FRIDAY = 5, 'Пятница'
    SATURDAY = 6, 'Суббота'


class ScheduleChangeType(models.TextChoices):
    TEACHER_SUBSTITUTION = 'teacher_substitution', 'замена преподавателя'
    RESCHEDULE = 'reschedule', 'перенос'
    CANCEL = 'cancel', 'отмена'


class Class(models.Model):
    name = models.CharField(max_length=10, unique=True)
    students_count = models.PositiveIntegerField()
    education_level = models.CharField(
        max_length=20,
        choices=EducationLevel.choices
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
        ordering = ['name']


class User(AbstractUser):
    class_obj = models.ForeignKey(
        'Class',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='users',
        verbose_name='класс'
    )
    role = models.CharField(
        max_length=20,
        choices=UserRole.choices
    )
    full_name = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.full_name or self.username

    class Meta:
        verbose_name = 'пользователь'
        verbose_name_plural = 'пользователи'
        ordering = ['full_name', 'username']


class Subject(models.Model):
    name = models.CharField(max_length=100, unique=True)
    required_room_type = models.CharField(
        max_length=50,
        choices=RoomType.choices,
        default=RoomType.ORDINARY
    )

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = 'предмет'
        verbose_name_plural = 'предметы'
        ordering = ['name']


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
    weekly_hours = models.PositiveIntegerField(verbose_name='часов в неделю')

    def clean(self):
        if self.weekly_hours < 1:
            raise ValidationError('Количество часов в неделю должно быть больше 0.')

    def __str__(self):
        return f'{self.class_obj} - {self.subject} ({self.weekly_hours} ч/нед)'

    class Meta:
        verbose_name = 'класс-предмет'
        verbose_name_plural = 'классы-предметы'
        constraints = [
            models.UniqueConstraint(
                fields=['class_obj', 'subject'],
                name='unique_class_subject'
            )
        ]


class Teacher(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='teacher_profile'
    )
    qualification = models.CharField(max_length=100)
    workload_hours = models.PositiveIntegerField(
        verbose_name='максимальная нагрузка в неделю'
    )

    def clean(self):
        if self.user and self.user.role != UserRole.TEACHER:
            raise ValidationError(
                'Профиль преподавателя можно создать только для пользователя с ролью teacher.'
            )

    def __str__(self):
        return self.user.full_name or self.user.username

    class Meta:
        verbose_name = 'преподаватель'
        verbose_name_plural = 'преподаватели'
        ordering = ['user__full_name', 'user__username']


class Classroom(models.Model):
    name = models.CharField(max_length=50, unique=True)
    capacity = models.PositiveIntegerField()
    room_type = models.CharField(
        max_length=50,
        choices=RoomType.choices
    )

    def clean(self):
        if self.capacity < 1:
            raise ValidationError('Вместимость кабинета должна быть больше 0.')

    def __str__(self):
        return f'{self.name} ({self.get_room_type_display()})'

    class Meta:
        verbose_name = 'кабинет'
        verbose_name_plural = 'кабинеты'
        ordering = ['name']


class LessonTime(models.Model):
    lesson_number = models.PositiveSmallIntegerField(verbose_name='номер урока')
    start_time = models.TimeField()
    end_time = models.TimeField()
    day_type = models.CharField(
        max_length=20,
        choices=DayType.choices
    )

    def clean(self):
        if self.end_time <= self.start_time:
            raise ValidationError('Время окончания должно быть позже времени начала.')

    def __str__(self):
        return (
            f'{self.lesson_number}-й урок: '
            f'{self.start_time.strftime("%H:%M")}–{self.end_time.strftime("%H:%M")} '
            f'({self.get_day_type_display()})'
        )

    class Meta:
        verbose_name = 'время урока'
        verbose_name_plural = 'времена уроков'
        ordering = ['day_type', 'lesson_number']
        constraints = [
            models.UniqueConstraint(
                fields=['lesson_number', 'day_type'],
                name='unique_lesson_number_day_type'
            )
        ]


class TimeSlot(models.Model):
    weekday = models.PositiveSmallIntegerField(choices=Weekday.choices)
    lesson_time = models.ForeignKey(
        LessonTime,
        on_delete=models.CASCADE,
        related_name='time_slots'
    )

    def __str__(self):
        return f'{self.get_weekday_display()} / {self.lesson_time}'

    class Meta:
        verbose_name = 'временной слот'
        verbose_name_plural = 'временные слоты'
        ordering = ['weekday', 'lesson_time__day_type', 'lesson_time__lesson_number']
        constraints = [
            models.UniqueConstraint(
                fields=['weekday', 'lesson_time'],
                name='unique_weekday_lesson_time_slot'
            )
        ]


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
        errors = {}

        class_subject = ClassSubject.objects.filter(
            class_obj=self.class_obj,
            subject=self.subject
        ).first()

        if not class_subject:
            errors['subject'] = (
                'Нельзя назначить преподавателя: предмет не привязан к данному классу.'
            )

        if self.hours_per_week is not None and self.hours_per_week < 1:
            errors['hours_per_week'] = 'Количество часов должно быть больше 0.'

        if class_subject and self.hours_per_week is not None:
            if self.hours_per_week > class_subject.weekly_hours:
                errors['hours_per_week'] = (
                    'Часы преподавателя не могут превышать общую недельную нагрузку '
                    'по предмету у класса.'
                )

            qs = TeachingAssignment.objects.filter(
                class_obj=self.class_obj,
                subject=self.subject
            )
            if self.pk:
                qs = qs.exclude(pk=self.pk)

            current_sum = qs.aggregate(total=Sum('hours_per_week'))['total'] or 0
            new_total = current_sum + self.hours_per_week

            if new_total > class_subject.weekly_hours:
                errors['hours_per_week'] = (
                    'Суммарные часы всех преподавателей по этому предмету и классу '
                    'не могут превышать weekly_hours в ClassSubject.'
                )

        teacher_total_qs = TeachingAssignment.objects.filter(teacher=self.teacher)
        if self.pk:
            teacher_total_qs = teacher_total_qs.exclude(pk=self.pk)

        teacher_current_sum = teacher_total_qs.aggregate(total=Sum('hours_per_week'))['total'] or 0
        teacher_new_sum = teacher_current_sum + (self.hours_per_week or 0)

        if self.teacher_id and self.hours_per_week is not None:
            if teacher_new_sum > self.teacher.workload_hours:
                errors['hours_per_week'] = (
                    'Суммарная недельная нагрузка преподавателя по всем назначениям '
                    'не может превышать его workload_hours.'
                )

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        extra = f' ({self.hours_per_week} ч/нед)' if self.hours_per_week else ''
        return f'{self.teacher} → {self.subject} / {self.class_obj}{extra}'

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
    teacher = models.ForeignKey(
        Teacher,
        on_delete=models.CASCADE,
        related_name='availabilities'
    )
    time_slot = models.ForeignKey(
        TimeSlot,
        on_delete=models.CASCADE,
        related_name='teacher_availabilities'
    )
    is_available = models.BooleanField(default=True)

    def __str__(self):
        status = 'доступен' if self.is_available else 'недоступен'
        return f'{self.teacher} / {self.time_slot} / {status}'

    class Meta:
        verbose_name = 'доступность преподавателя'
        verbose_name_plural = 'доступность преподавателей'
        constraints = [
            models.UniqueConstraint(
                fields=['teacher', 'time_slot'],
                name='unique_teacher_time_slot'
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
    time_slot = models.ForeignKey(
        TimeSlot,
        on_delete=models.CASCADE,
        related_name='schedules'
    )
    lesson_date = models.DateField()

    def clean(self):
        errors = {}

        assignment_exists = TeachingAssignment.objects.filter(
            teacher=self.teacher,
            subject=self.subject,
            class_obj=self.class_obj
        ).exists()
        if not assignment_exists:
            errors['teacher'] = (
                'Этот преподаватель не назначен на данный предмет у данного класса.'
            )

        class_subject_exists = ClassSubject.objects.filter(
            class_obj=self.class_obj,
            subject=self.subject
        ).exists()
        if not class_subject_exists:
            errors['subject'] = 'Этот предмет не привязан к данному классу.'

        if self.classroom.room_type != self.subject.required_room_type:
            errors['classroom'] = 'Тип кабинета не подходит для данного предмета.'

        if self.classroom.capacity < self.class_obj.students_count:
            errors['classroom'] = 'Вместимость кабинета меньше количества учеников в классе.'

        availability = TeacherAvailability.objects.filter(
            teacher=self.teacher,
            time_slot=self.time_slot
        ).first()

        if availability and not availability.is_available:
            errors['time_slot'] = 'Преподаватель недоступен в данный временной слот.'

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return (
            f'{self.class_obj} - {self.subject} - '
            f'{self.lesson_date} - {self.time_slot}'
        )

    class Meta:
        verbose_name = 'занятие'
        verbose_name_plural = 'занятия'
        ordering = ['lesson_date', 'time_slot__weekday', 'time_slot__lesson_time__lesson_number']
        constraints = [
            models.UniqueConstraint(
                fields=['class_obj', 'lesson_date', 'time_slot'],
                name='unique_class_date_time_slot'
            ),
            models.UniqueConstraint(
                fields=['teacher', 'lesson_date', 'time_slot'],
                name='unique_teacher_date_time_slot'
            ),
            models.UniqueConstraint(
                fields=['classroom', 'lesson_date', 'time_slot'],
                name='unique_classroom_date_time_slot'
            )
        ]


class ScheduleChange(models.Model):
    schedule = models.ForeignKey(
        Schedule,
        on_delete=models.CASCADE,
        related_name='changes'
    )
    change_type = models.CharField(
        max_length=50,
        choices=ScheduleChangeType.choices
    )
    description = models.TextField()
    changed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Изм. {self.schedule} - {self.get_change_type_display()}'

    class Meta:
        verbose_name = 'изменение расписания'
        verbose_name_plural = 'изменения расписания'
        ordering = ['-changed_at']


class IntegrationLog(models.Model):
    system_name = models.CharField(max_length=100)
    operation = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.system_name}: {self.operation[:20]}...'

    class Meta:
        verbose_name = 'лог интеграции'
        verbose_name_plural = 'логи интеграции'
        ordering = ['-created_at']