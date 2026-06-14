from datetime import date, time

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


def _seed_minimal_week(week_start: date) -> dict:
    User = get_user_model()
    class_obj = Class.objects.create(
        name='9A',
        grade=9,
        parallel='A',
        students_count=22,
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
        lesson_time = LessonTime.objects.create(
            lesson_number=number,
            start_time=start_at,
            end_time=end_at,
            day_type='normal',
        )
        for weekday in [Weekday.MONDAY, Weekday.TUESDAY, Weekday.WEDNESDAY, Weekday.THURSDAY, Weekday.FRIDAY]:
            TimeSlot.objects.create(weekday=weekday, lesson_time=lesson_time)

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


def _make_lesson(*, class_obj, subject, teacher, classroom, time_slot, lesson_date):
    lesson = Schedule(
        class_obj=class_obj,
        subject=subject,
        teacher=teacher,
        classroom=classroom,
        time_slot=time_slot,
        lesson_date=lesson_date,
    )
    lesson.save()
    return lesson


class ConflictDaySubstitutionPreviewTests(TestCase):
    def setUp(self):
        self.week_start = date(2026, 4, 6)
        self.objects = _seed_minimal_week(self.week_start)
        self.class_obj = self.objects['class_obj']
        self.math = self.objects['math']
        self.russian = self.objects['russian']
        self.math_teacher = self.objects['math_teacher']
        self.rus_teacher = self.objects['rus_teacher']

        room_a = Classroom.objects.first()
        room_b = Classroom.objects.last()
        mon_slot_1 = TimeSlot.objects.get(weekday=Weekday.MONDAY, lesson_time__lesson_number=1)
        mon_slot_2 = TimeSlot.objects.get(weekday=Weekday.MONDAY, lesson_time__lesson_number=2)
        mon_slot_3 = TimeSlot.objects.get(weekday=Weekday.MONDAY, lesson_time__lesson_number=3)
        mon_slot_5 = TimeSlot.objects.get(weekday=Weekday.MONDAY, lesson_time__lesson_number=5)

        _make_lesson(
            class_obj=self.class_obj,
            subject=self.math,
            teacher=self.math_teacher,
            classroom=room_a,
            time_slot=mon_slot_3,
            lesson_date=self.week_start,
        )
        _make_lesson(
            class_obj=self.class_obj,
            subject=self.russian,
            teacher=self.rus_teacher,
            classroom=room_a,
            time_slot=mon_slot_5,
            lesson_date=self.week_start,
        )

        self.other_class = Class.objects.create(
            name='9B',
            grade=9,
            parallel='B',
            students_count=20,
            education_level=EducationLevel.BASIC,
        )
        ClassSubject.objects.create(class_obj=self.other_class, subject=self.math, weekly_hours=2)
        TeachingAssignment.objects.create(
            teacher=self.math_teacher,
            subject=self.math,
            class_obj=self.other_class,
            hours_per_week=2,
        )
        _make_lesson(
            class_obj=self.other_class,
            subject=self.math,
            teacher=self.math_teacher,
            classroom=room_b,
            time_slot=mon_slot_1,
            lesson_date=self.week_start,
        )
        _make_lesson(
            class_obj=self.other_class,
            subject=self.math,
            teacher=self.math_teacher,
            classroom=room_b,
            time_slot=mon_slot_2,
            lesson_date=self.week_start,
        )

        User = get_user_model()
        sub_user = User.objects.create_user(
            username='sub_math',
            password='x',
            role=UserRole.TEACHER,
            full_name='Sub Math',
        )
        self.sub_teacher = Teacher.objects.create(
            user=sub_user,
            qualification='Mathematics',
            workload_hours=20,
            max_lessons_per_day=5,
        )
        TeachingAssignment.objects.create(
            teacher=self.sub_teacher,
            subject=self.math,
            class_obj=self.other_class,
            hours_per_week=1,
        )
        for slot in TimeSlot.objects.all():
            TeacherAvailability.objects.create(
                teacher=self.sub_teacher,
                time_slot=slot,
                is_available=True,
            )

        self.url = reverse(
            'scheduler:conflict_day_view',
            kwargs={'class_id': self.class_obj.id, 'lesson_date': self.week_start.isoformat()},
        )

    def _unlock_conflict_day_view(self, client: Client) -> None:
        response = client.get(
            reverse('scheduler:schedule_conflicts'),
            {'week_start': self.week_start.isoformat()},
        )
        self.assertEqual(response.status_code, 200)

    def test_preview_then_apply_uses_external_substitute_and_removes_gaps(self):
        client = Client()
        self._unlock_conflict_day_view(client)

        preview_response = client.post(
            self.url,
            {'action': 'generate_substitution_preview'},
            follow=True,
        )
        self.assertEqual(preview_response.status_code, 200)
        preview_content = preview_response.content.decode('utf-8')
        self.assertIn('Предпросмотр замены на день', preview_content)
        self.assertIn('Sub Math', preview_content)

        original_numbers = list(
            Schedule.objects.filter(class_obj=self.class_obj, lesson_date=self.week_start)
            .order_by('time_slot__lesson_time__lesson_number')
            .values_list('time_slot__lesson_time__lesson_number', flat=True)
        )
        self.assertEqual(original_numbers, [3, 5])

        apply_response = client.post(
            self.url,
            {'action': 'apply_substitution_preview'},
            follow=True,
        )
        self.assertEqual(apply_response.status_code, 200)

        updated_lessons = Schedule.objects.filter(
            class_obj=self.class_obj,
            lesson_date=self.week_start,
        ).order_by('time_slot__lesson_time__lesson_number')
        updated_numbers = list(updated_lessons.values_list('time_slot__lesson_time__lesson_number', flat=True))
        self.assertEqual(updated_numbers, [1, 2])
        self.assertTrue(updated_lessons.filter(teacher=self.sub_teacher).exists())
        self.assertFalse(
            TeachingAssignment.objects.filter(
                teacher=self.sub_teacher,
                subject=self.math,
                class_obj=self.class_obj,
            ).exists()
        )

    def test_discard_preview_does_not_change_schedule(self):
        client = Client()
        self._unlock_conflict_day_view(client)
        client.post(self.url, {'action': 'generate_substitution_preview'}, follow=True)

        discard_response = client.post(
            self.url,
            {'action': 'discard_substitution_preview'},
            follow=True,
        )
        self.assertEqual(discard_response.status_code, 200)

        numbers_after_discard = list(
            Schedule.objects.filter(class_obj=self.class_obj, lesson_date=self.week_start)
            .order_by('time_slot__lesson_time__lesson_number')
            .values_list('time_slot__lesson_time__lesson_number', flat=True)
        )
        self.assertEqual(numbers_after_discard, [3, 5])
