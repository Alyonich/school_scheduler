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
    Subject,
    Teacher,
    TeacherAvailability,
    TeachingAssignment,
    TimeSlot,
    UserRole,
    Weekday,
)
from .services.schedule_generator import GeneticScheduleGenerator
from .services.schedule_generator.data_loader import load_generation_context


class SchedulerSmokeTests(TestCase):
    def setUp(self):
        self.week_start = date(2026, 4, 6)
        User = get_user_model()
        class_obj = Class.objects.create(
            name='7A',
            grade=7,
            parallel='A',
            students_count=24,
            education_level=EducationLevel.BASIC,
        )
        self.class_obj = class_obj
        math = Subject.objects.create(name='Mathematics', required_room_type=RoomType.ORDINARY, max_lessons_per_day=2)
        english = Subject.objects.create(name='English', required_room_type=RoomType.ORDINARY, max_lessons_per_day=2)
        self.math = math
        self.english = english
        ClassSubject.objects.create(class_obj=class_obj, subject=math, weekly_hours=4)
        ClassSubject.objects.create(class_obj=class_obj, subject=english, weekly_hours=3)

        room = Classroom.objects.create(name='101', capacity=30, room_type=RoomType.ORDINARY)
        second_room = Classroom.objects.create(name='102', capacity=30, room_type=RoomType.ORDINARY)
        self.room_ids = {room.id, second_room.id}

        for number, start_at, end_at in [
            (1, time(8, 30), time(9, 15)),
            (2, time(9, 25), time(10, 10)),
            (3, time(10, 30), time(11, 15)),
            (4, time(11, 25), time(12, 10)),
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
        self.math_teacher = Teacher.objects.create(user=math_user, qualification='Mathematics', workload_hours=20, max_lessons_per_day=4)
        self.english_teacher = Teacher.objects.create(user=english_user, qualification='English', workload_hours=20, max_lessons_per_day=4)
        TeachingAssignment.objects.create(teacher=self.math_teacher, subject=math, class_obj=class_obj, hours_per_week=4)
        TeachingAssignment.objects.create(teacher=self.english_teacher, subject=english, class_obj=class_obj, hours_per_week=3)

        for teacher in [self.math_teacher, self.english_teacher]:
            for slot in TimeSlot.objects.all():
                TeacherAvailability.objects.create(teacher=teacher, time_slot=slot, is_available=True)

    def test_generator_creates_conflict_free_schedule(self):
        result = GeneticScheduleGenerator(population_size=40, generations=70, mutation_rate=0.18, seed=7).generate(
            self.week_start,
            class_ids=[self.class_obj.id],
        )
        self.assertEqual(result.hard_penalty, 0)
        self.assertEqual(result.diagnostics.get('class_gap', 0), 0)
        self.assertEqual(result.diagnostics.get('class_daily_overload', 0), 0)
        self.assertEqual(result.diagnostics.get('forbidden_double_lesson', 0), 0)

        seen_slots = set()
        subject_daily = {}
        daily_lessons = {}
        for lesson in self.class_obj.schedules.all():
            marker = (lesson.lesson_date, lesson.time_slot_id)
            self.assertNotIn(marker, seen_slots)
            seen_slots.add(marker)
            key = (lesson.subject_id, lesson.lesson_date)
            subject_daily[key] = subject_daily.get(key, 0) + 1
            daily_lessons.setdefault(lesson.lesson_date, []).append(lesson.time_slot.lesson_time.lesson_number)

        self.assertLessEqual(max(subject_daily.values()), 2)
        for lesson_numbers in daily_lessons.values():
            ordered = sorted(set(lesson_numbers))
            self.assertEqual((ordered[-1] - ordered[0] + 1) - len(ordered), 0)

    def test_generation_context_applies_weekly_grade_limits(self):
        User = get_user_model()
        class_obj = Class.objects.create(
            name='10A',
            grade=10,
            parallel='A',
            students_count=22,
            education_level=EducationLevel.HIGH,
        )
        subject = Subject.objects.create(name='Physics', required_room_type=RoomType.ORDINARY, max_lessons_per_day=2)
        ClassSubject.objects.create(class_obj=class_obj, subject=subject, weekly_hours=30)

        user = User.objects.create_user(
            username='physics_10',
            password='test12345',
            role=UserRole.TEACHER,
            full_name='Physics Teacher 10A',
        )
        teacher = Teacher.objects.create(user=user, qualification='Physics', workload_hours=40, max_lessons_per_day=7)
        TeachingAssignment.objects.create(teacher=teacher, subject=subject, class_obj=class_obj, hours_per_week=30)
        for slot in TimeSlot.objects.all():
            TeacherAvailability.objects.create(teacher=teacher, time_slot=slot, is_available=True)

        context = load_generation_context(self.week_start, class_ids=[class_obj.id])
        self.assertEqual(len(context.lesson_requirements), 20)
        self.assertTrue(
            any('СанПиН' in warning or 'емкости недельной сетки' in warning for warning in context.warnings)
        )

    def test_timetable_page_renders(self):
        GeneticScheduleGenerator(population_size=30, generations=50, mutation_rate=0.2, seed=3).generate(
            self.week_start,
            class_ids=[self.class_obj.id],
        )
        response = Client().get(reverse('scheduler:timetable'), {'class_obj': self.class_obj.id, 'week_start': self.week_start})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Сетка расписания')
        self.assertContains(response, 'Показано расписание класса 7A.')
