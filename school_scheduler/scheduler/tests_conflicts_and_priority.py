"""Тесты для:
1. Усиления штрафа за тяжёлые предметы на поздних уроках (особенно 9 и 11 классы).
2. Страницы «Конфликты и окна».
3. Защищённого split-view дня, который доступен только через страницу конфликтов.
"""

from datetime import date, time, timedelta
from collections import defaultdict

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
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
from .services.schedule_generator.chromosome import Chromosome, Placement
from .services.schedule_generator.data_loader import load_generation_context
from .services.schedule_generator.fitness import evaluate_chromosome


def _seed_minimal_week(week_start: date) -> dict:
    User = get_user_model()
    class_obj = Class.objects.create(
        name='9A', grade=9, parallel='A', students_count=22,
        education_level=EducationLevel.BASIC,
    )
    math = Subject.objects.create(name='Mathematics', required_room_type=RoomType.ORDINARY, max_lessons_per_day=2)
    russian = Subject.objects.create(name='Russian Language', required_room_type=RoomType.ORDINARY, max_lessons_per_day=2)
    ClassSubject.objects.create(class_obj=class_obj, subject=math, weekly_hours=4)
    ClassSubject.objects.create(class_obj=class_obj, subject=russian, weekly_hours=4)

    Classroom.objects.create(name='101', capacity=30, room_type=RoomType.ORDINARY)
    Classroom.objects.create(name='102', capacity=30, room_type=RoomType.ORDINARY)

    for number, start_at, end_at in [
        (1, time(8, 30), time(9, 15)),
        (2, time(9, 25), time(10, 10)),
        (3, time(10, 30), time(11, 15)),
        (4, time(11, 25), time(12, 10)),
        (5, time(12, 20), time(13, 5)),
        (6, time(13, 15), time(14, 0)),
    ]:
        lt = LessonTime.objects.create(lesson_number=number, start_time=start_at, end_time=end_at, day_type='normal')
        for weekday in [Weekday.MONDAY, Weekday.TUESDAY, Weekday.WEDNESDAY, Weekday.THURSDAY, Weekday.FRIDAY]:
            TimeSlot.objects.create(weekday=weekday, lesson_time=lt)

    math_user = User.objects.create_user(username='math', password='x', role=UserRole.TEACHER, full_name='Math Teacher')
    rus_user = User.objects.create_user(username='rus', password='x', role=UserRole.TEACHER, full_name='Russian Teacher')
    math_teacher = Teacher.objects.create(user=math_user, qualification='Mathematics', workload_hours=30, max_lessons_per_day=5)
    rus_teacher = Teacher.objects.create(user=rus_user, qualification='Russian', workload_hours=30, max_lessons_per_day=5)
    TeachingAssignment.objects.create(teacher=math_teacher, subject=math, class_obj=class_obj, hours_per_week=4)
    TeachingAssignment.objects.create(teacher=rus_teacher, subject=russian, class_obj=class_obj, hours_per_week=4)
    for teacher in [math_teacher, rus_teacher]:
        for slot in TimeSlot.objects.all():
            TeacherAvailability.objects.create(teacher=teacher, time_slot=slot, is_available=True)

    return {
        'class_obj': class_obj,
        'math': math,
        'russian': russian,
        'math_teacher': math_teacher,
        'rus_teacher': rus_teacher,
    }


class HardSubjectLatePositionTests(TestCase):
    """Штраф за тяжёлый предмет позже 3-го урока растёт с номером класса
    и особенно резок для 9 и 11 классов."""

    def setUp(self):
        self.week_start = date(2026, 4, 6)

    def _build_late_placement(self, grade: int) -> tuple[Chromosome, object]:
        """Создаёт класс заданного grade с одним уроком математики на 5-м уроке.

        Возвращает (chromosome, context).
        """
        User = get_user_model()
        class_obj = Class.objects.create(
            name=f'{grade}A', grade=grade, parallel='A', students_count=20,
            education_level=EducationLevel.BASIC,
        )
        math = Subject.objects.create(
            name=f'Mathematics_{grade}',
            required_room_type=RoomType.ORDINARY,
            max_lessons_per_day=2,
        )
        ClassSubject.objects.create(class_obj=class_obj, subject=math, weekly_hours=1)
        Classroom.objects.create(name=f'r{grade}', capacity=30, room_type=RoomType.ORDINARY)

        for number, start_at, end_at in [
            (1, time(8, 30), time(9, 15)),
            (2, time(9, 25), time(10, 10)),
            (3, time(10, 30), time(11, 15)),
            (4, time(11, 25), time(12, 10)),
            (5, time(12, 20), time(13, 5)),
        ]:
            lt = LessonTime.objects.create(lesson_number=number, start_time=start_at, end_time=end_at, day_type='normal')
            for weekday in [Weekday.MONDAY, Weekday.TUESDAY, Weekday.WEDNESDAY, Weekday.THURSDAY, Weekday.FRIDAY]:
                TimeSlot.objects.create(weekday=weekday, lesson_time=lt)

        user = User.objects.create_user(
            username=f'm{grade}', password='x', role=UserRole.TEACHER, full_name=f'M{grade}'
        )
        teacher = Teacher.objects.create(user=user, qualification='Mathematics', workload_hours=20, max_lessons_per_day=4)
        TeachingAssignment.objects.create(teacher=teacher, subject=math, class_obj=class_obj, hours_per_week=1)
        for slot in TimeSlot.objects.all():
            TeacherAvailability.objects.create(teacher=teacher, time_slot=slot, is_available=True)

        ctx = load_generation_context(self.week_start, class_ids=[class_obj.id])
        # Помещаем единственный урок на 5-й урок понедельника.
        target_slot = TimeSlot.objects.get(weekday=Weekday.MONDAY, lesson_time__lesson_number=5)
        target_room = Classroom.objects.get(name=f'r{grade}')
        chromo = Chromosome(placements=[Placement(time_slot_id=target_slot.id, classroom_id=target_room.id)])
        evaluate_chromosome(chromo, ctx)
        return chromo, ctx

    def test_grade_11_late_penalty_exceeds_grade_5(self):
        chromo_5, _ = self._build_late_placement(grade=5)
        chromo_11, _ = self._build_late_placement(grade=11)
        late_5 = chromo_5.diagnostics.get('hard_subject_late_position', 0)
        late_11 = chromo_11.diagnostics.get('hard_subject_late_position', 0)
        self.assertGreater(
            late_11, late_5,
            f'У 11 класса штраф за поздний тяжёлый предмет должен быть выше: '
            f'grade5={late_5}, grade11={late_11}',
        )

    def test_grade_9_late_penalty_exceeds_grade_8(self):
        chromo_8, _ = self._build_late_placement(grade=8)
        chromo_9, _ = self._build_late_placement(grade=9)
        late_8 = chromo_8.diagnostics.get('hard_subject_late_position', 0)
        late_9 = chromo_9.diagnostics.get('hard_subject_late_position', 0)
        self.assertGreater(
            late_9, late_8,
            f'9 класс должен штрафоваться сильнее 8: g8={late_8}, g9={late_9}',
        )

    def test_grade_11_early_placement_gives_bonus(self):
        # Помещаем единственный урок math на 2-й урок — должен сработать бонус.
        User = get_user_model()
        class_obj = Class.objects.create(
            name='11A', grade=11, parallel='A', students_count=20,
            education_level=EducationLevel.HIGH,
        )
        math = Subject.objects.create(name='Math11', required_room_type=RoomType.ORDINARY, max_lessons_per_day=2)
        ClassSubject.objects.create(class_obj=class_obj, subject=math, weekly_hours=1)
        Classroom.objects.create(name='r11', capacity=30, room_type=RoomType.ORDINARY)
        for number, start_at, end_at in [
            (1, time(8, 30), time(9, 15)),
            (2, time(9, 25), time(10, 10)),
            (3, time(10, 30), time(11, 15)),
        ]:
            lt = LessonTime.objects.create(lesson_number=number, start_time=start_at, end_time=end_at, day_type='normal')
            for weekday in [Weekday.MONDAY, Weekday.TUESDAY, Weekday.WEDNESDAY, Weekday.THURSDAY, Weekday.FRIDAY]:
                TimeSlot.objects.create(weekday=weekday, lesson_time=lt)
        user = User.objects.create_user(username='m11', password='x', role=UserRole.TEACHER, full_name='M11')
        teacher = Teacher.objects.create(user=user, qualification='Mathematics', workload_hours=20, max_lessons_per_day=4)
        TeachingAssignment.objects.create(teacher=teacher, subject=math, class_obj=class_obj, hours_per_week=1)
        for slot in TimeSlot.objects.all():
            TeacherAvailability.objects.create(teacher=teacher, time_slot=slot, is_available=True)

        ctx = load_generation_context(self.week_start, class_ids=[class_obj.id])
        target_slot = TimeSlot.objects.get(weekday=Weekday.MONDAY, lesson_time__lesson_number=2)
        target_room = Classroom.objects.get(name='r11')
        chromo = Chromosome(placements=[Placement(time_slot_id=target_slot.id, classroom_id=target_room.id)])
        evaluate_chromosome(chromo, ctx)
        bonus = chromo.diagnostics.get('hard_subject_early_bonus_grade_9_11', 0)
        self.assertGreaterEqual(bonus, 1, 'Должен сработать бонус за тяжёлый предмет в начале дня у 11 класса')


def _make_lesson(*, class_obj, subject, teacher, classroom, time_slot, lesson_date):
    """Создаёт Schedule в обход .clean()/.full_clean() — нам нужны окна
    и поздние старты, которые модель в нормальном flow не пропустит."""
    schedule = Schedule(
        class_obj=class_obj,
        subject=subject,
        teacher=teacher,
        classroom=classroom,
        time_slot=time_slot,
        lesson_date=lesson_date,
    )
    schedule.save()
    return schedule


class ConflictsPageTests(TestCase):
    def setUp(self):
        self.week_start = date(2026, 4, 6)
        self.objects = _seed_minimal_week(self.week_start)

    def test_conflicts_page_lists_class_with_gap(self):
        # Создадим день с окном: уроки на №1, №3, №5 (нет №2 и №4)
        class_obj = self.objects['class_obj']
        room = Classroom.objects.first()
        mon_slot_1 = TimeSlot.objects.get(weekday=Weekday.MONDAY, lesson_time__lesson_number=1)
        mon_slot_3 = TimeSlot.objects.get(weekday=Weekday.MONDAY, lesson_time__lesson_number=3)
        mon_slot_5 = TimeSlot.objects.get(weekday=Weekday.MONDAY, lesson_time__lesson_number=5)
        _make_lesson(class_obj=class_obj, subject=self.objects['math'], teacher=self.objects['math_teacher'],
                     classroom=room, time_slot=mon_slot_1, lesson_date=self.week_start)
        _make_lesson(class_obj=class_obj, subject=self.objects['russian'], teacher=self.objects['rus_teacher'],
                     classroom=room, time_slot=mon_slot_3, lesson_date=self.week_start)
        _make_lesson(class_obj=class_obj, subject=self.objects['math'], teacher=self.objects['math_teacher'],
                     classroom=room, time_slot=mon_slot_5, lesson_date=self.week_start)

        response = Client().get(
            reverse('scheduler:schedule_conflicts'),
            {'week_start': self.week_start.isoformat()},
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('Конфликты и окна', content)
        self.assertIn(class_obj.name, content)
        self.assertIn('Окна:', content)

    def test_conflicts_page_lists_class_with_late_start(self):
        class_obj = self.objects['class_obj']
        room = Classroom.objects.first()
        tue_slot_3 = TimeSlot.objects.get(weekday=Weekday.TUESDAY, lesson_time__lesson_number=3)
        tue_slot_4 = TimeSlot.objects.get(weekday=Weekday.TUESDAY, lesson_time__lesson_number=4)
        tue_date = self.week_start + timedelta(days=1)
        _make_lesson(class_obj=class_obj, subject=self.objects['math'], teacher=self.objects['math_teacher'],
                     classroom=room, time_slot=tue_slot_3, lesson_date=tue_date)
        _make_lesson(class_obj=class_obj, subject=self.objects['russian'], teacher=self.objects['rus_teacher'],
                     classroom=room, time_slot=tue_slot_4, lesson_date=tue_date)

        response = Client().get(
            reverse('scheduler:schedule_conflicts'),
            {'week_start': self.week_start.isoformat()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('Старт с 3 урока', response.content.decode('utf-8'))

    def test_conflicts_page_empty_state_when_clean(self):
        # Без созданных расписаний с проблемами — должен быть «всё в порядке».
        response = Client().get(
            reverse('scheduler:schedule_conflicts'),
            {'week_start': self.week_start.isoformat()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('нет классов с окнами', response.content.decode('utf-8'))


class ConflictDaySplitViewAccessTests(TestCase):
    def setUp(self):
        self.week_start = date(2026, 4, 6)
        self.objects = _seed_minimal_week(self.week_start)
        # Создаём один урок, чтобы было что показать.
        room = Classroom.objects.first()
        mon_slot_1 = TimeSlot.objects.get(weekday=Weekday.MONDAY, lesson_time__lesson_number=1)
        _make_lesson(
            class_obj=self.objects['class_obj'],
            subject=self.objects['math'],
            teacher=self.objects['math_teacher'],
            classroom=room,
            time_slot=mon_slot_1,
            lesson_date=self.week_start,
        )

    def test_direct_access_to_split_view_redirects_to_conflicts(self):
        """Прямой переход без посещения /conflicts/ должен редиректить."""
        client = Client()
        url = reverse(
            'scheduler:conflict_day_view',
            kwargs={'class_id': self.objects['class_obj'].id, 'lesson_date': self.week_start.isoformat()},
        )
        response = client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('scheduler:schedule_conflicts'), response.url)

    def test_access_via_conflicts_page_grants_split_view(self):
        client = Client()
        # Сначала открываем /conflicts/ — это ставит маркер в сессию.
        prep = client.get(
            reverse('scheduler:schedule_conflicts'),
            {'week_start': self.week_start.isoformat()},
        )
        self.assertEqual(prep.status_code, 200)
        # Теперь идём в сплит-вид того же класса/недели.
        url = reverse(
            'scheduler:conflict_day_view',
            kwargs={'class_id': self.objects['class_obj'].id, 'lesson_date': self.week_start.isoformat()},
        )
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        # Слева — день класса.
        self.assertIn(self.objects['class_obj'].name, content)
        # Справа — учителя класса.
        self.assertIn(str(self.objects['math_teacher']), content)
        self.assertIn(str(self.objects['rus_teacher']), content)

    def test_split_view_marker_is_per_week(self):
        """Маркер сессии должен быть привязан к конкретной неделе."""
        client = Client()
        # Открываем conflicts для другой недели.
        other_week = date(2026, 4, 13)
        client.get(reverse('scheduler:schedule_conflicts'), {'week_start': other_week.isoformat()})
        # Пытаемся открыть сплит-вид нашей оригинальной недели — должен редиректить.
        url = reverse(
            'scheduler:conflict_day_view',
            kwargs={'class_id': self.objects['class_obj'].id, 'lesson_date': self.week_start.isoformat()},
        )
        response = client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('scheduler:schedule_conflicts'), response.url)

    def test_split_view_shows_teacher_lessons_in_other_classes(self):
        """В правой колонке учитель показан со ВСЕМИ его уроками этого дня,
        не только с этим классом."""
        # Создаём второй класс и урок math_teacher там же.
        other_class = Class.objects.create(
            name='8B', grade=8, parallel='B', students_count=21,
            education_level=EducationLevel.BASIC,
        )
        ClassSubject.objects.create(class_obj=other_class, subject=self.objects['math'], weekly_hours=1)
        TeachingAssignment.objects.create(
            teacher=self.objects['math_teacher'], subject=self.objects['math'], class_obj=other_class, hours_per_week=1,
        )
        room = Classroom.objects.first()
        mon_slot_2 = TimeSlot.objects.get(weekday=Weekday.MONDAY, lesson_time__lesson_number=2)
        _make_lesson(
            class_obj=other_class,
            subject=self.objects['math'],
            teacher=self.objects['math_teacher'],
            classroom=room,
            time_slot=mon_slot_2,
            lesson_date=self.week_start,
        )

        client = Client()
        client.get(reverse('scheduler:schedule_conflicts'), {'week_start': self.week_start.isoformat()})
        url = reverse(
            'scheduler:conflict_day_view',
            kwargs={'class_id': self.objects['class_obj'].id, 'lesson_date': self.week_start.isoformat()},
        )
        response = client.get(url)
        content = response.content.decode('utf-8')
        self.assertIn(other_class.name, content,
                      f'В правой колонке должен быть виден соседний класс ({other_class.name}), '
                      'у которого тоже занятие у math_teacher в этот день.')
