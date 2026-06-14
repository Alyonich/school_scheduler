"""Тесты пользовательского лимита времени работы GA.

Инварианты:
1. Если пользователь задал короткий лимит времени, GA реально останавливается
   и в warnings появляется строка о превышении лимита.
2. Если идеальное расписание (hard_penalty=0 и нет окон) находится раньше
   лимита, GA останавливается досрочно — это видно в warnings.
3. Форма ScheduleGenerationForm.get_generator_settings возвращает
   ga_time_limit_seconds = минуты × 60.
"""

from datetime import date, time

from django.contrib.auth import get_user_model
from django.test import TestCase

from .forms import ScheduleGenerationForm
from .models import (
    Class,
    ClassSubject,
    Classroom,
    EducationLevel,
    LessonTime,
    RoomType,
    Subject,
    Teacher,
    TeacherAvailability,
    TeachingAssignment,
    TimeSlot,
    UserRole,
    Weekday,
)
from .services.schedule_generator import GeneticScheduleGenerator


def _seed_simple_school():
    User = get_user_model()
    class_obj = Class.objects.create(
        name='7A', grade=7, parallel='A', students_count=22,
        education_level=EducationLevel.BASIC,
    )
    math = Subject.objects.create(name='Math', required_room_type=RoomType.ORDINARY, max_lessons_per_day=2)
    rus = Subject.objects.create(name='Russian', required_room_type=RoomType.ORDINARY, max_lessons_per_day=2)
    ClassSubject.objects.create(class_obj=class_obj, subject=math, weekly_hours=4)
    ClassSubject.objects.create(class_obj=class_obj, subject=rus, weekly_hours=3)
    Classroom.objects.create(name='101', capacity=30, room_type=RoomType.ORDINARY)
    Classroom.objects.create(name='102', capacity=30, room_type=RoomType.ORDINARY)
    for number, st, en in [
        (1, time(8, 30), time(9, 15)),
        (2, time(9, 25), time(10, 10)),
        (3, time(10, 30), time(11, 15)),
        (4, time(11, 25), time(12, 10)),
    ]:
        lt = LessonTime.objects.create(lesson_number=number, start_time=st, end_time=en, day_type='normal')
        for weekday in [Weekday.MONDAY, Weekday.TUESDAY, Weekday.WEDNESDAY, Weekday.THURSDAY, Weekday.FRIDAY]:
            TimeSlot.objects.create(weekday=weekday, lesson_time=lt)
    mu = User.objects.create_user(username='m', password='x', role=UserRole.TEACHER, full_name='M')
    ru = User.objects.create_user(username='r', password='x', role=UserRole.TEACHER, full_name='R')
    mt = Teacher.objects.create(user=mu, qualification='Math', workload_hours=30, max_lessons_per_day=5)
    rt = Teacher.objects.create(user=ru, qualification='Rus', workload_hours=30, max_lessons_per_day=5)
    TeachingAssignment.objects.create(teacher=mt, subject=math, class_obj=class_obj, hours_per_week=4)
    TeachingAssignment.objects.create(teacher=rt, subject=rus, class_obj=class_obj, hours_per_week=3)
    for teacher in [mt, rt]:
        for slot in TimeSlot.objects.all():
            TeacherAvailability.objects.create(teacher=teacher, time_slot=slot, is_available=True)
    return {'class_obj': class_obj}


class FormReturnsTimeLimitSettings(TestCase):
    def test_get_generator_settings_returns_seconds(self):
        form = ScheduleGenerationForm(data={
            'week_start': '2026-04-06',
            'time_limit_minutes': '2.5',
        })
        self.assertTrue(form.is_valid(), msg=form.errors)
        settings = form.get_generator_settings()
        self.assertIn('ga_time_limit_seconds', settings)
        self.assertAlmostEqual(settings['ga_time_limit_seconds'], 2.5 * 60.0, places=3)

    def test_form_rejects_zero_minutes(self):
        form = ScheduleGenerationForm(data={
            'week_start': '2026-04-06',
            'time_limit_minutes': '0',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('time_limit_minutes', form.errors)


class TimeBudgetEnforcedTests(TestCase):
    def setUp(self):
        self.week_start = date(2026, 4, 6)
        _seed_simple_school()

    def test_short_time_limit_triggers_timeout_warning(self):
        # Очень короткий лимит. После CSP-сидов и стартовой популяции GA
        # моментально хитом увидит истёкший бюджет — в warnings появится
        # строка о лимите ИЛИ строка о досрочном идеале (если уже всё в порядке).
        generator = GeneticScheduleGenerator(
            population_size=24,
            generations=100_000,
            mutation_rate=0.2,
            seed=42,
            ga_time_limit_seconds=0.05,
        )
        result = generator.generate(self.week_start)

        joined = ' || '.join(result.warnings)
        self.assertTrue(
            ('GA остановлен по лимиту времени' in joined)
            or ('GA остановлен досрочно' in joined)
            or ('GA остановлен сразу' in joined),
            f'Ожидали маркер времени/раннего выхода в warnings, получили: {result.warnings}',
        )

    def test_long_time_limit_finds_ideal_solution_and_exits_early(self):
        # На простой задаче GA должен быстро найти идеальное решение и выйти
        # до истечения лимита. Берём заведомо большой лимит (60 сек).
        generator = GeneticScheduleGenerator(
            population_size=24,
            generations=100_000,
            mutation_rate=0.2,
            seed=42,
            ga_time_limit_seconds=60.0,
        )
        result = generator.generate(self.week_start)

        self.assertEqual(result.hard_penalty, 0, msg=f'Hard penalty: {result.diagnostics}')
        self.assertEqual(result.diagnostics.get('class_gap', 0), 0,
                         msg=f'Окна в результате: {result.diagnostics}')

        joined = ' || '.join(result.warnings)
        self.assertFalse(
            'GA остановлен по лимиту времени' in joined,
            f'GA должен был выйти досрочно, а не по таймауту. warnings: {result.warnings}',
        )


class EndpointAcceptsTimeLimitMinutesTests(TestCase):
    """Проверяем, что POST к scheduler:generate работает с time_limit_minutes."""

    def setUp(self):
        self.week_start = date(2026, 4, 6)
        _seed_simple_school()

    def test_endpoint_accepts_time_limit_minutes(self):
        from django.test import override_settings
        from django.urls import reverse
        from django.test import Client

        with override_settings(SCHEDULER_GENERATION_RUN_INLINE=True):
            response = Client().post(
                reverse('scheduler:generate'),
                {
                    'week_start': self.week_start.isoformat(),
                    'time_limit_minutes': '0.2',
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn('/generate/jobs/', response.url)
