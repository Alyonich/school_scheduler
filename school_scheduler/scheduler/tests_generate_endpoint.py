from datetime import date, time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import (
    Class,
    ClassSubject,
    Classroom,
    EducationLevel,
    IntegrationLog,
    LessonTime,
    RoomType,
    Schedule,
    Subject,
    Teacher,
    TeacherAvailability,
    TeachingAssignment,
    TimeSlot,
    UserRole,
    WeeklyClassSubjectLoad,
    Weekday,
)

@override_settings(SCHEDULER_GENERATION_RUN_INLINE=True)
class GenerateEndpointTests(TestCase):
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
        self.math_subject = math
        self.english_subject = english
        self.math_class_subject = ClassSubject.objects.create(class_obj=class_obj, subject=math, weekly_hours=4)
        self.english_class_subject = ClassSubject.objects.create(class_obj=class_obj, subject=english, weekly_hours=3)

        for room_name in ['101', '102']:
            Classroom.objects.create(name=room_name, capacity=30, room_type=RoomType.ORDINARY)

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
        math_teacher = Teacher.objects.create(user=math_user, qualification='Mathematics', workload_hours=20, max_lessons_per_day=4)
        english_teacher = Teacher.objects.create(user=english_user, qualification='English', workload_hours=20, max_lessons_per_day=4)
        TeachingAssignment.objects.create(teacher=math_teacher, subject=math, class_obj=class_obj, hours_per_week=4)
        TeachingAssignment.objects.create(teacher=english_teacher, subject=english, class_obj=class_obj, hours_per_week=3)

        for teacher in [math_teacher, english_teacher]:
            for slot in TimeSlot.objects.all():
                TeacherAvailability.objects.create(teacher=teacher, time_slot=slot, is_available=True)

    def test_generate_supports_all_classes_mode(self):
        client = Client()
        before = IntegrationLog.objects.count()
        response = client.post(
            reverse('scheduler:generate'),
            {
                'week_start': self.week_start.isoformat(),
                'generation_mode': 'balanced',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('/generate/jobs/', response.url)
        self.assertGreater(IntegrationLog.objects.count(), before)

    def test_generate_returns_redirect_when_process_lock_busy(self):
        client = Client()
        with patch('scheduler.views._acquire_generation_process_lock', return_value=None):
            response = client.post(
                reverse('scheduler:generate'),
                {
                    'week_start': self.week_start.isoformat(),
                    'generation_mode': 'balanced',
                },
            )
        self.assertEqual(response.status_code, 302)

    def test_generate_applies_weekly_workload_overrides(self):
        client = Client()
        response = client.post(
            reverse('scheduler:generate'),
            {
                'week_start': self.week_start.isoformat(),
                'generation_mode': 'balanced',
                f'load_{self.math_class_subject.id}': '1',
                f'load_{self.english_class_subject.id}': '2',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('/generate/jobs/', response.url)

        override_math = WeeklyClassSubjectLoad.objects.get(
            week_start=self.week_start,
            class_subject=self.math_class_subject,
        )
        override_english = WeeklyClassSubjectLoad.objects.get(
            week_start=self.week_start,
            class_subject=self.english_class_subject,
        )
        self.assertEqual(override_math.weekly_hours, 1)
        self.assertEqual(override_english.weekly_hours, 2)

        math_count = Schedule.objects.filter(
            class_obj=self.class_obj,
            subject=self.math_subject,
            lesson_date__gte=self.week_start,
            lesson_date__lt=self.week_start + date.resolution * 5,
        ).count()
        english_count = Schedule.objects.filter(
            class_obj=self.class_obj,
            subject=self.english_subject,
            lesson_date__gte=self.week_start,
            lesson_date__lt=self.week_start + date.resolution * 5,
        ).count()
        self.assertEqual(math_count, 1)
        self.assertEqual(english_count, 2)

    def test_generation_progress_status_returns_completed_payload(self):
        client = Client()
        response = client.post(
            reverse('scheduler:generate'),
            {
                'week_start': self.week_start.isoformat(),
                'generation_mode': 'balanced',
            },
        )
        self.assertEqual(response.status_code, 302)

        status_response = client.get(f'{response.url}status/')
        self.assertEqual(status_response.status_code, 200)
        payload = status_response.json()
        self.assertEqual(payload['state'], 'completed')
        self.assertEqual(payload['progress_percent'], 100)
        self.assertIn('created_lessons', payload)

    def test_generation_progress_events_streams_status(self):
        client = Client()
        response = client.post(
            reverse('scheduler:generate'),
            {
                'week_start': self.week_start.isoformat(),
                'generation_mode': 'balanced',
            },
        )
        self.assertEqual(response.status_code, 302)

        events_response = client.get(f'{response.url}events/')
        self.assertEqual(events_response.status_code, 200)
        self.assertIn('text/event-stream', events_response['Content-Type'])

        stream = events_response.streaming_content
        first_chunk = next(stream).decode('utf-8')
        second_chunk = next(stream).decode('utf-8')
        self.assertIn('retry: 5000', first_chunk)
        self.assertIn('event: status', second_chunk)
