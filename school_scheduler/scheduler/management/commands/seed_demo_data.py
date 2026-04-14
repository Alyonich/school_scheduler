from datetime import date, time, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from scheduler.models import (
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
from scheduler.services.schedule_generator import GeneticScheduleGenerator


class Command(BaseCommand):
    help = 'Populate the database with a realistic demo school and generate a sample timetable.'

    def handle(self, *args, **options):
        User = get_user_model()

        admin, created = User.objects.get_or_create(
            username='admin',
            defaults={
                'role': UserRole.ADMIN,
                'full_name': 'Demo Administrator',
                'is_staff': True,
                'is_superuser': True,
            },
        )
        if created:
            admin.set_password('admin12345')
            admin.save()

        classes = {}
        for class_name, grade, parallel, level, students in [
            ('5A', 5, 'A', EducationLevel.BASIC, 26),
            ('5B', 5, 'B', EducationLevel.BASIC, 25),
            ('6A', 6, 'A', EducationLevel.BASIC, 27),
            ('7A', 7, 'A', EducationLevel.BASIC, 24),
            ('9A', 9, 'A', EducationLevel.BASIC, 23),
            ('10A', 10, 'A', EducationLevel.HIGH, 21),
        ]:
            classes[class_name], _ = Class.objects.update_or_create(
                name=class_name,
                defaults={
                    'grade': grade,
                    'parallel': parallel,
                    'education_level': level,
                    'students_count': students,
                },
            )

        subjects = {}
        for name, room_type, daily_limit in [
            ('Mathematics', RoomType.ORDINARY, 2),
            ('Russian Language', RoomType.ORDINARY, 2),
            ('Literature', RoomType.ORDINARY, 2),
            ('English', RoomType.ORDINARY, 2),
            ('History', RoomType.ORDINARY, 1),
            ('Biology', RoomType.LAB, 1),
            ('Physics', RoomType.LAB, 1),
            ('Chemistry', RoomType.LAB, 1),
            ('Computer Science', RoomType.COMPUTER, 1),
            ('Physical Education', RoomType.ORDINARY, 1),
        ]:
            subjects[name], _ = Subject.objects.update_or_create(
                name=name,
                defaults={
                    'required_room_type': room_type,
                    'max_lessons_per_day': daily_limit,
                },
            )

        for room_name, capacity, room_type in [
            ('101', 30, RoomType.ORDINARY),
            ('102', 30, RoomType.ORDINARY),
            ('103', 30, RoomType.ORDINARY),
            ('104', 30, RoomType.ORDINARY),
            ('201', 30, RoomType.LAB),
            ('202', 28, RoomType.LAB),
            ('301', 32, RoomType.COMPUTER),
        ]:
            Classroom.objects.update_or_create(
                name=room_name,
                defaults={'capacity': capacity, 'room_type': room_type},
            )

        lesson_times = [
            (1, time(8, 30), time(9, 15)),
            (2, time(9, 25), time(10, 10)),
            (3, time(10, 30), time(11, 15)),
            (4, time(11, 25), time(12, 10)),
            (5, time(12, 20), time(13, 5)),
            (6, time(13, 15), time(14, 0)),
        ]
        slots_by_weekday = {}
        for lesson_number, start_at, end_at in lesson_times:
            lesson_time, _ = LessonTime.objects.update_or_create(
                lesson_number=lesson_number,
                day_type='normal',
                defaults={'start_time': start_at, 'end_time': end_at},
            )
            for weekday in [Weekday.MONDAY, Weekday.TUESDAY, Weekday.WEDNESDAY, Weekday.THURSDAY, Weekday.FRIDAY]:
                slot, _ = TimeSlot.objects.update_or_create(weekday=weekday, lesson_time=lesson_time)
                slots_by_weekday.setdefault(weekday, []).append(slot)

        teachers = {}
        teacher_specs = [
            ('petrova', 'Elena Petrova', 'Mathematics', 32, 6),
            ('ivanov', 'Sergey Ivanov', 'Russian Language', 24, 5),
            ('smirnova', 'Irina Smirnova', 'Literature', 22, 5),
            ('johnson', 'Anna Johnson', 'English', 24, 5),
            ('volkov', 'Dmitry Volkov', 'History', 18, 4),
            ('egorova', 'Maria Egorova', 'Biology', 18, 4),
            ('orlov', 'Pavel Orlov', 'Physics', 20, 4),
            ('vasilieva', 'Olga Vasilieva', 'Chemistry', 18, 4),
            ('sokolov', 'Nikita Sokolov', 'Computer Science', 18, 4),
            ('fedorov', 'Ivan Fedorov', 'Physical Education', 20, 5),
        ]

        for username, full_name, qualification, workload, daily_limit in teacher_specs:
            user, _ = User.objects.update_or_create(
                username=username,
                defaults={
                    'full_name': full_name,
                    'role': UserRole.TEACHER,
                    'is_staff': True,
                },
            )
            if not user.has_usable_password():
                user.set_password('teacher12345')
                user.save()
            teachers[qualification], _ = Teacher.objects.update_or_create(
                user=user,
                defaults={
                    'qualification': qualification,
                    'workload_hours': workload,
                    'max_lessons_per_day': daily_limit,
                },
            )

        class_plan = {
            '5A': {'Mathematics': 5, 'Russian Language': 4, 'Literature': 3, 'English': 3, 'History': 2, 'Biology': 1, 'Computer Science': 1, 'Physical Education': 2},
            '5B': {'Mathematics': 5, 'Russian Language': 4, 'Literature': 3, 'English': 3, 'History': 2, 'Biology': 1, 'Computer Science': 1, 'Physical Education': 2},
            '6A': {'Mathematics': 5, 'Russian Language': 4, 'Literature': 3, 'English': 3, 'History': 2, 'Biology': 2, 'Computer Science': 1, 'Physical Education': 2},
            '7A': {'Mathematics': 5, 'Russian Language': 3, 'Literature': 3, 'English': 3, 'History': 2, 'Biology': 2, 'Physics': 2, 'Computer Science': 1, 'Physical Education': 2},
            '9A': {'Mathematics': 5, 'Russian Language': 3, 'Literature': 2, 'English': 3, 'History': 2, 'Biology': 2, 'Physics': 2, 'Chemistry': 2, 'Computer Science': 1, 'Physical Education': 2},
            '10A': {'Mathematics': 5, 'Russian Language': 2, 'Literature': 2, 'English': 3, 'History': 2, 'Biology': 1, 'Physics': 3, 'Chemistry': 2, 'Computer Science': 2, 'Physical Education': 2},
        }

        for class_name, plan in class_plan.items():
            for subject_name, weekly_hours in plan.items():
                class_subject, _ = ClassSubject.objects.update_or_create(
                    class_obj=classes[class_name],
                    subject=subjects[subject_name],
                    defaults={'weekly_hours': weekly_hours},
                )
                teacher = teachers[subject_name]
                TeachingAssignment.objects.update_or_create(
                    teacher=teacher,
                    subject=class_subject.subject,
                    class_obj=class_subject.class_obj,
                    defaults={'hours_per_week': weekly_hours},
                )

        TeacherAvailability.objects.all().delete()
        for teacher in Teacher.objects.all():
            for slot in TimeSlot.objects.all():
                TeacherAvailability.objects.create(teacher=teacher, time_slot=slot, is_available=True)

        restricted = [
            ('Computer Science', Weekday.WEDNESDAY, 5),
            ('Chemistry', Weekday.THURSDAY, 1),
        ]
        for qualification, weekday, lesson_number in restricted:
            slot = next(item for item in slots_by_weekday[weekday] if item.lesson_time.lesson_number == lesson_number)
            TeacherAvailability.objects.filter(
                teacher=teachers[qualification],
                time_slot=slot,
            ).update(is_available=False)

        week_start = date.today() - timedelta(days=date.today().weekday())
        generator = GeneticScheduleGenerator(population_size=90, generations=180, mutation_rate=0.2)
        result = generator.generate(week_start)

        self.stdout.write(self.style.SUCCESS('Demo school data seeded successfully.'))
        self.stdout.write(self.style.SUCCESS(f'Generated {result.created_lessons} lessons for week {week_start}.'))
        self.stdout.write('Admin credentials: admin / admin12345')
