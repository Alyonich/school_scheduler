"""Инвариантные тесты алгоритма генерации.

Эти тесты проверяют три ключевых свойства итогового расписания:
1. Преподаватель, помеченный SICK или DAY_OFF, не получает уроков в эти слоты.
2. В расписании каждого класса нет внутренних окон между уроками внутри одного дня.
3. Количество занятий по каждому предмету соответствует ClassSubject.weekly_hours.
"""

from collections import Counter, defaultdict
from datetime import date, time

from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import (
    AvailabilityStatus,
    Class,
    ClassSubject,
    Classroom,
    EducationLevel,
    LessonTime,
    RoomType,
    Schedule,
    Subject,
    Teacher,
    TeacherAvailability,
    TeachingAssignment,
    TimeSlot,
    UserRole,
    Weekday,
)
from .services.schedule_generator import GeneticScheduleGenerator


def _build_demo_school(week_start: date, sick_days_for_math: tuple[int, ...] = ()) -> dict:
    """Создаёт минимальную школьную инфраструктуру для тестов.

    Возвращает словарь с ключевыми объектами для последующих ассертов.
    """
    User = get_user_model()
    class_obj = Class.objects.create(
        name='7A',
        grade=7,
        parallel='A',
        students_count=24,
        education_level=EducationLevel.BASIC,
    )

    math = Subject.objects.create(name='Mathematics', required_room_type=RoomType.ORDINARY, max_lessons_per_day=2)
    english = Subject.objects.create(name='English', required_room_type=RoomType.ORDINARY, max_lessons_per_day=2)
    history = Subject.objects.create(name='History', required_room_type=RoomType.ORDINARY, max_lessons_per_day=2)

    ClassSubject.objects.create(class_obj=class_obj, subject=math, weekly_hours=4)
    ClassSubject.objects.create(class_obj=class_obj, subject=english, weekly_hours=3)
    ClassSubject.objects.create(class_obj=class_obj, subject=history, weekly_hours=2)

    Classroom.objects.create(name='101', capacity=30, room_type=RoomType.ORDINARY)
    Classroom.objects.create(name='102', capacity=30, room_type=RoomType.ORDINARY)

    for number, start_at, end_at in [
        (1, time(8, 30), time(9, 15)),
        (2, time(9, 25), time(10, 10)),
        (3, time(10, 30), time(11, 15)),
        (4, time(11, 25), time(12, 10)),
        (5, time(12, 20), time(13, 5)),
    ]:
        lesson_time = LessonTime.objects.create(
            lesson_number=number,
            start_time=start_at,
            end_time=end_at,
            day_type='normal',
        )
        for weekday in [Weekday.MONDAY, Weekday.TUESDAY, Weekday.WEDNESDAY, Weekday.THURSDAY, Weekday.FRIDAY]:
            TimeSlot.objects.create(weekday=weekday, lesson_time=lesson_time)

    math_user = User.objects.create_user(username='math', password='test12345', role=UserRole.TEACHER, full_name='Math Teacher')
    english_user = User.objects.create_user(username='eng', password='test12345', role=UserRole.TEACHER, full_name='English Teacher')
    history_user = User.objects.create_user(username='hist', password='test12345', role=UserRole.TEACHER, full_name='History Teacher')

    math_teacher = Teacher.objects.create(user=math_user, qualification='Mathematics', workload_hours=20, max_lessons_per_day=4)
    english_teacher = Teacher.objects.create(user=english_user, qualification='English', workload_hours=20, max_lessons_per_day=4)
    history_teacher = Teacher.objects.create(user=history_user, qualification='History', workload_hours=20, max_lessons_per_day=4)

    TeachingAssignment.objects.create(teacher=math_teacher, subject=math, class_obj=class_obj, hours_per_week=4)
    TeachingAssignment.objects.create(teacher=english_teacher, subject=english, class_obj=class_obj, hours_per_week=3)
    TeachingAssignment.objects.create(teacher=history_teacher, subject=history, class_obj=class_obj, hours_per_week=2)

    # По умолчанию все учителя доступны во всех слотах.
    for teacher in [math_teacher, english_teacher, history_teacher]:
        for slot in TimeSlot.objects.all():
            TeacherAvailability.objects.create(teacher=teacher, time_slot=slot, is_available=True)

    # Помечаем выбранные дни недели для math_teacher как SICK на тестовую неделю.
    if sick_days_for_math:
        sick_slots = TimeSlot.objects.filter(weekday__in=list(sick_days_for_math))
        for slot in sick_slots:
            TeacherAvailability.objects.update_or_create(
                teacher=math_teacher,
                time_slot=slot,
                defaults={
                    'is_available': False,
                    'status': AvailabilityStatus.SICK,
                    'week_start': week_start,
                },
            )

    return {
        'class_obj': class_obj,
        'math': math,
        'english': english,
        'history': history,
        'math_teacher': math_teacher,
        'english_teacher': english_teacher,
        'history_teacher': history_teacher,
    }


class TeacherUnavailabilityInvariantTests(TestCase):
    """Учитель в статусе SICK/DAY_OFF не должен получать уроков."""

    def setUp(self):
        self.week_start = date(2026, 4, 6)
        self.objects = _build_demo_school(
            week_start=self.week_start,
            sick_days_for_math=(Weekday.MONDAY, Weekday.FRIDAY),
        )

    def test_sick_teacher_gets_no_lessons_on_sick_days(self):
        result = GeneticScheduleGenerator(
            population_size=24,
            generations=20,
            mutation_rate=0.2,
            seed=42,
        ).generate(self.week_start, class_ids=[self.objects['class_obj'].id])

        self.assertEqual(result.hard_penalty, 0, f'Жёсткий штраф ненулевой, диагностика: {result.diagnostics}')

        math_teacher = self.objects['math_teacher']
        math_lessons = Schedule.objects.filter(
            teacher=math_teacher,
            lesson_date__gte=self.week_start,
        )
        for lesson in math_lessons:
            self.assertNotIn(
                lesson.time_slot.weekday,
                {Weekday.MONDAY, Weekday.FRIDAY},
                f'Урок преподавателя в больничный день: {lesson} ({lesson.lesson_date})',
            )

    def test_unavailability_set_includes_sick_pairs(self):
        from .models import teacher_unavailability_pairs_for_week

        slot_ids_mon_fri = list(
            TimeSlot.objects.filter(weekday__in=[Weekday.MONDAY, Weekday.FRIDAY]).values_list('id', flat=True)
        )
        unavailable = teacher_unavailability_pairs_for_week(
            week_start=self.week_start,
            teacher_ids=[self.objects['math_teacher'].id],
            time_slot_ids=slot_ids_mon_fri,
        )
        for slot_id in slot_ids_mon_fri:
            self.assertIn(
                (self.objects['math_teacher'].id, slot_id),
                unavailable,
                f'Пара (учитель, слот) должна быть в недоступности: slot_id={slot_id}',
            )


class DayOffTeacherInvariantTests(TestCase):
    """То же самое для статуса DAY_OFF (выходной)."""

    def setUp(self):
        self.week_start = date(2026, 4, 6)
        self.objects = _build_demo_school(week_start=self.week_start)
        # Помечаем среду как DAY_OFF для english_teacher.
        english_teacher = self.objects['english_teacher']
        for slot in TimeSlot.objects.filter(weekday=Weekday.WEDNESDAY):
            TeacherAvailability.objects.update_or_create(
                teacher=english_teacher,
                time_slot=slot,
                defaults={
                    'is_available': False,
                    'status': AvailabilityStatus.DAY_OFF,
                    'week_start': self.week_start,
                },
            )

    def test_day_off_teacher_gets_no_lessons_on_day_off(self):
        result = GeneticScheduleGenerator(
            population_size=24,
            generations=20,
            mutation_rate=0.2,
            seed=99,
        ).generate(self.week_start, class_ids=[self.objects['class_obj'].id])

        self.assertEqual(result.hard_penalty, 0, f'Жёсткий штраф: {result.diagnostics}')

        english_lessons = Schedule.objects.filter(
            teacher=self.objects['english_teacher'],
            lesson_date__gte=self.week_start,
        )
        for lesson in english_lessons:
            self.assertNotEqual(
                lesson.time_slot.weekday,
                Weekday.WEDNESDAY,
                f'Урок выпал на выходной преподавателя: {lesson} ({lesson.lesson_date})',
            )


class NoWindowsInvariantTests(TestCase):
    """В каждом дне у каждого класса не должно быть окон между уроками."""

    def setUp(self):
        self.week_start = date(2026, 4, 6)
        self.objects = _build_demo_school(week_start=self.week_start)

    def test_no_internal_gaps_in_class_schedule(self):
        result = GeneticScheduleGenerator(
            population_size=24,
            generations=25,
            mutation_rate=0.2,
            seed=123,
        ).generate(self.week_start, class_ids=[self.objects['class_obj'].id])

        self.assertEqual(result.hard_penalty, 0, f'Жёсткий штраф: {result.diagnostics}')

        lessons_by_day: dict[date, list[int]] = defaultdict(list)
        for lesson in Schedule.objects.filter(
            class_obj=self.objects['class_obj'],
            lesson_date__gte=self.week_start,
        ).select_related('time_slot__lesson_time'):
            lessons_by_day[lesson.lesson_date].append(lesson.time_slot.lesson_time.lesson_number)

        for lesson_date, numbers in lessons_by_day.items():
            ordered = sorted(set(numbers))
            if len(ordered) < 2:
                continue
            full_span = ordered[-1] - ordered[0] + 1
            self.assertEqual(
                full_span,
                len(ordered),
                f'Найдено окно в дне {lesson_date}: занятые номера {ordered}',
            )


class WeeklyLoadMatchInvariantTests(TestCase):
    """Сколько часов было в ClassSubject.weekly_hours — столько уроков должно быть в расписании."""

    def setUp(self):
        self.week_start = date(2026, 4, 6)
        self.objects = _build_demo_school(week_start=self.week_start)

    def test_weekly_lesson_count_matches_class_subject(self):
        result = GeneticScheduleGenerator(
            population_size=24,
            generations=25,
            mutation_rate=0.2,
            seed=2024,
        ).generate(self.week_start, class_ids=[self.objects['class_obj'].id])

        self.assertEqual(result.hard_penalty, 0, f'Жёсткий штраф: {result.diagnostics}')

        expected = {
            self.objects['math'].id: 4,
            self.objects['english'].id: 3,
            self.objects['history'].id: 2,
        }
        actual_counts = Counter(
            Schedule.objects.filter(
                class_obj=self.objects['class_obj'],
                lesson_date__gte=self.week_start,
            ).values_list('subject_id', flat=True)
        )
        for subject_id, expected_count in expected.items():
            self.assertEqual(
                actual_counts.get(subject_id, 0),
                expected_count,
                f'Нагрузка по предмету {subject_id} не совпадает: '
                f'ожидалось {expected_count}, получено {actual_counts.get(subject_id, 0)}',
            )

    def test_no_lessons_skipped_when_feasible(self):
        result = GeneticScheduleGenerator(
            population_size=24,
            generations=25,
            mutation_rate=0.2,
            seed=2025,
        ).generate(self.week_start, class_ids=[self.objects['class_obj'].id])

        # 4 + 3 + 2 = 9 уроков, никаких skipped не ожидается.
        total = Schedule.objects.filter(
            class_obj=self.objects['class_obj'],
            lesson_date__gte=self.week_start,
        ).count()
        self.assertEqual(total, 9, f'Ожидалось 9 уроков, создано {total}. Предупреждения: {result.warnings}')


class CombinedInvariantTests(TestCase):
    """Все три инварианта вместе: учитель болеет, нет окон, нагрузка совпадает."""

    def setUp(self):
        self.week_start = date(2026, 4, 6)
        self.objects = _build_demo_school(
            week_start=self.week_start,
            sick_days_for_math=(Weekday.TUESDAY,),
        )

    def test_all_invariants_hold_simultaneously(self):
        result = GeneticScheduleGenerator(
            population_size=28,
            generations=30,
            mutation_rate=0.2,
            seed=7777,
        ).generate(self.week_start, class_ids=[self.objects['class_obj'].id])

        self.assertEqual(result.hard_penalty, 0, f'Жёсткий штраф: {result.diagnostics}')

        # Инвариант 1: math_teacher не работает во вторник.
        math_tue = Schedule.objects.filter(
            teacher=self.objects['math_teacher'],
            lesson_date__gte=self.week_start,
            time_slot__weekday=Weekday.TUESDAY,
        ).count()
        self.assertEqual(math_tue, 0, 'Math teacher получил уроки во вторник, хотя болеет.')

        # Инвариант 2: нет окон у класса.
        lessons_by_day: dict[date, list[int]] = defaultdict(list)
        for lesson in Schedule.objects.filter(
            class_obj=self.objects['class_obj'],
            lesson_date__gte=self.week_start,
        ).select_related('time_slot__lesson_time'):
            lessons_by_day[lesson.lesson_date].append(lesson.time_slot.lesson_time.lesson_number)
        for lesson_date, numbers in lessons_by_day.items():
            ordered = sorted(set(numbers))
            if len(ordered) < 2:
                continue
            self.assertEqual(
                ordered[-1] - ordered[0] + 1,
                len(ordered),
                f'Окно в дне {lesson_date}: {ordered}',
            )

        # Инвариант 3: количество уроков по каждому предмету == weekly_hours.
        actual_counts = Counter(
            Schedule.objects.filter(
                class_obj=self.objects['class_obj'],
                lesson_date__gte=self.week_start,
            ).values_list('subject_id', flat=True)
        )
        self.assertEqual(actual_counts.get(self.objects['math'].id, 0), 4)
        self.assertEqual(actual_counts.get(self.objects['english'].id, 0), 3)
        self.assertEqual(actual_counts.get(self.objects['history'].id, 0), 2)
