"""Заполнение БД демо-данными школы по реальной спецификации.

Источник данных: «данные.docx» (16 преподавателей, 22 класса, 26 кабинетов,
типовая нагрузка по СанПиН).

Запуск:
    python manage.py seed_demo_data           — только данные, без генерации
    python manage.py seed_demo_data --generate — данные + одна неделя расписания
"""

from datetime import date, time, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from scheduler.models import (
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
from scheduler.services.schedule_generator import GeneticScheduleGenerator


# -----------------------------------------------------------------------------
# Спецификация школы (см. данные.docx, разделы 1–7).
# -----------------------------------------------------------------------------

# Раздел 1: классы.
CLASS_SPEC: list[tuple[str, int, str, str, int]] = [
    # имя, grade, parallel, уровень, число учеников
    *[(f'{g}{p}', g, p, EducationLevel.BASIC, students)
      for g, students in [(5, 28), (6, 27), (7, 26), (8, 25), (9, 24)]
      for p in ('А', 'Б', 'В', 'Г')],
    *[(f'10{p}', 10, p, EducationLevel.HIGH, 22) for p in ('А', 'Б')],
    *[(f'11{p}', 11, p, EducationLevel.HIGH, 20) for p in ('А', 'Б')],
]

# Раздел 2: предметы и тип кабинета.
# В модели RoomType всего три типа: ordinary / lab / computer.
# Спортзал, музыка, ИЗО, мастерские, ОБЗР, актовый зал — все обычные.
SUBJECT_SPEC: list[tuple[str, str, int]] = [
    # имя, тип кабинета, max_lessons_per_day
    ('Математика',        RoomType.ORDINARY, 2),
    ('Русский язык',      RoomType.ORDINARY, 2),
    ('Литература',        RoomType.ORDINARY, 2),
    ('Алгебра',           RoomType.ORDINARY, 2),
    ('Геометрия',         RoomType.ORDINARY, 2),
    ('Информатика',       RoomType.COMPUTER, 2),
    ('Физика',            RoomType.LAB,      2),
    ('Химия',             RoomType.LAB,      2),
    ('Биология',          RoomType.LAB,      2),
    ('География',         RoomType.ORDINARY, 2),
    ('История',           RoomType.ORDINARY, 2),
    ('Обществознание',    RoomType.ORDINARY, 2),
    ('Английский язык',   RoomType.ORDINARY, 2),
    ('Физическая культура', RoomType.ORDINARY, 1),
    ('ОБЗР',              RoomType.ORDINARY, 1),
    ('Музыка',            RoomType.ORDINARY, 1),
    ('ИЗО',               RoomType.ORDINARY, 1),
    ('Технология',        RoomType.ORDINARY, 2),
]

# Раздел 6: кабинеты.
CLASSROOM_SPEC: list[tuple[str, int, str]] = [
    ('201', 30, RoomType.ORDINARY),
    ('202', 28, RoomType.ORDINARY),
    ('203', 28, RoomType.ORDINARY),
    ('204', 25, RoomType.COMPUTER),  # компьютерный (15 ПК)
    ('205', 26, RoomType.LAB),       # физика
    ('206', 26, RoomType.LAB),       # химия
    ('207', 28, RoomType.LAB),       # биология
    ('208', 30, RoomType.ORDINARY),  # география
    ('209', 30, RoomType.ORDINARY),  # история
    ('210', 28, RoomType.ORDINARY),  # английский
    ('211', 20, RoomType.ORDINARY),  # музыка
    ('212', 25, RoomType.ORDINARY),  # ИЗО
    ('Спортзал',     30, RoomType.ORDINARY),
    ('Мастерская №1', 20, RoomType.ORDINARY),
    ('Мастерская №2', 20, RoomType.ORDINARY),
    ('Актовый зал',  50, RoomType.ORDINARY),
    *[(str(num), 28, RoomType.ORDINARY) for num in range(214, 226)],
]

# Раздел 7: сетка уроков.
LESSON_TIMES: list[tuple[int, time, time]] = [
    (1, time(8, 30),  time(9, 10)),
    (2, time(9, 20),  time(10, 0)),
    (3, time(10, 10), time(10, 50)),
    (4, time(11, 0),  time(11, 40)),  # после большая перемена 20 мин
    (5, time(11, 50), time(12, 30)),
    (6, time(12, 40), time(13, 20)),
    (7, time(13, 30), time(14, 10)),
]
SCHOOL_WEEKDAYS = [Weekday.MONDAY, Weekday.TUESDAY, Weekday.WEDNESDAY, Weekday.THURSDAY, Weekday.FRIDAY]

# Раздел 4: преподаватели.
# (username, ФИО, квалификация, workload_hours, max_lessons_per_day, какие предметы умеет вести)
TEACHER_SPEC: list[tuple[str, str, str, int, int, list[str]]] = [
    ('belova',    'Белова Анна Викторовна',         'высшая, математика',                22, 6, ['Математика', 'Алгебра', 'Геометрия']),
    ('soloviev',  'Соловьёв Игорь Петрович',        'первая, физика и информатика',      24, 6, ['Физика', 'Информатика']),
    ('morozova',  'Морозова Елена Дмитриевна',      'высшая, русский и литература',      20, 5, ['Русский язык', 'Литература']),
    ('grishina',  'Гришина Татьяна Олеговна',       'первая, химия и биология',          22, 6, ['Химия', 'Биология']),
    ('kuznetsov', 'Кузнецов Алексей Андреевич',     'вторая, история и обществознание',  24, 6, ['История', 'Обществознание']),
    ('novikova',  'Новикова Ирина Сергеевна',       'первая, английский',                24, 6, ['Английский язык']),
    ('petrova',   'Петрова Людмила Васильевна',     'высшая, география',                 20, 5, ['География']),
    ('vasiliev',  'Васильев Дмитрий Николаевич',    'первая, физкультура и ОБЗР',        24, 6, ['Физическая культура', 'ОБЗР']),
    ('kovaleva',  'Ковалёва Наталья Юрьевна',       'первая, музыка',                    16, 4, ['Музыка']),
    ('sorokina',  'Сорокина Ольга Викторовна',      'вторая, ИЗО',                       16, 4, ['ИЗО']),
    ('timofeev',  'Тимофеев Сергей Борисович',      'первая, технология',                22, 6, ['Технология']),
    ('volkova',   'Волкова Мария Андреевна',        'высшая, алгебра 7-11',              22, 6, ['Алгебра', 'Геометрия', 'Математика']),
    ('zaitsev',   'Зайцев Роман Викторович',        'вторая, информатика',               18, 6, ['Информатика']),
    ('lebedeva',  'Лебедева Оксана Павловна',       'первая, биология',                  20, 5, ['Биология']),
    ('semenova',  'Семёнова Анна Геннадьевна',      'первая, история и обществознание',  22, 6, ['История', 'Обществознание']),
    ('denisova',  'Денисова Екатерина Валерьевна',  'первая, русский для 9-11',          20, 5, ['Русский язык', 'Литература']),
]


def _build_class_subject_plan(class_name: str, grade: int) -> dict[str, int]:
    """Возвращает план «предмет → часов в неделю» для одного класса.

    Цифры приведены к примерам из документа (5А = 27 ч, 7Б = 29 ч, 10А = 32 ч).
    Для каждой параллели — одна типовая раскладка.
    """
    if grade == 5:
        return {
            'Математика': 5, 'Русский язык': 5, 'Литература': 3,
            'Биология': 1, 'География': 1, 'История': 2,
            'Английский язык': 3, 'Физическая культура': 3,
            'Музыка': 1, 'ИЗО': 1, 'Технология': 2,
        }
    if grade == 6:
        return {
            'Математика': 5, 'Русский язык': 5, 'Литература': 3,
            'Биология': 2, 'География': 1, 'История': 2, 'Обществознание': 1,
            'Английский язык': 3, 'Физическая культура': 3, 'ОБЗР': 1,
            'Музыка': 1, 'ИЗО': 1, 'Технология': 1,
        }
    if grade == 7:
        return {
            'Русский язык': 4, 'Литература': 2, 'Алгебра': 3, 'Геометрия': 2,
            'Информатика': 1, 'Физика': 2, 'Биология': 2, 'География': 2,
            'История': 2, 'Обществознание': 1, 'Английский язык': 3,
            'Физическая культура': 3, 'ОБЗР': 1, 'Музыка': 1, 'ИЗО': 1, 'Технология': 1,
        }
    if grade == 8:
        return {
            'Русский язык': 3, 'Литература': 2, 'Алгебра': 3, 'Геометрия': 2,
            'Информатика': 1, 'Физика': 2, 'Химия': 2, 'Биология': 2, 'География': 2,
            'История': 2, 'Обществознание': 1, 'Английский язык': 3,
            'Физическая культура': 3, 'ОБЗР': 1, 'Технология': 1,
        }
    if grade == 9:
        return {
            'Русский язык': 2, 'Литература': 3, 'Алгебра': 3, 'Геометрия': 2,
            'Информатика': 1, 'Физика': 2, 'Химия': 2, 'Биология': 2, 'География': 2,
            'История': 2, 'Обществознание': 1, 'Английский язык': 3,
            'Физическая культура': 3, 'ОБЗР': 1,
        }
    # grades 10 и 11 — старшая школа, непрофильная раскладка из документа.
    return {
        'Русский язык': 2, 'Литература': 3, 'Алгебра': 4, 'Геометрия': 2,
        'Информатика': 2, 'Физика': 3, 'Химия': 2, 'Биология': 2, 'География': 1,
        'История': 2, 'Обществознание': 2, 'Английский язык': 3,
        'Физическая культура': 3, 'ОБЗР': 1,
    }


def _assign_teachers_to_class_subjects(
    *,
    class_subjects: list[ClassSubject],
    teachers: dict[str, Teacher],
    teacher_subjects: dict[str, list[str]],
) -> tuple[list[TeachingAssignment], list[str]]:
    """Раскидывает учителей по ClassSubject, не превышая workload каждого.

    Алгоритм: для каждого ClassSubject (отсортирован по убыванию редкости
    предмета) — выбираем учителя, который умеет вести этот предмет, имеет
    свободную квоту workload, и у которого пока меньше всего часов. Тогда
    нагрузка распределяется равномерно среди тех, кто может вести предмет.
    """
    used_hours: dict[str, int] = {username: 0 for username in teachers}
    candidates_by_subject: dict[str, list[str]] = {}
    for username, subject_list in teacher_subjects.items():
        for subject_name in subject_list:
            candidates_by_subject.setdefault(subject_name, []).append(username)

    # Сортируем ClassSubject так, чтобы редкие предметы получали учителя первыми
    # (иначе общие предметы съедают квоту учителей-«многостаночников»).
    subject_frequency = {
        subject_name: sum(1 for cs in class_subjects if cs.subject.name == subject_name)
        for subject_name in {cs.subject.name for cs in class_subjects}
    }
    candidate_count = {
        subject_name: len(candidates_by_subject.get(subject_name, []))
        for subject_name in subject_frequency
    }
    # Сначала — где меньше всего кандидатов; среди равных — где меньше всего часов.
    class_subjects_sorted = sorted(
        class_subjects,
        key=lambda cs: (
            candidate_count.get(cs.subject.name, 999),
            -cs.weekly_hours,
            cs.class_obj.grade,
            cs.class_obj.parallel,
        ),
    )

    assignments: list[TeachingAssignment] = []
    warnings: list[str] = []

    for cs in class_subjects_sorted:
        subject_name = cs.subject.name
        candidates = candidates_by_subject.get(subject_name, [])
        if not candidates:
            warnings.append(f'Нет ни одного учителя для предмета «{subject_name}».')
            continue
        # Лучший: тот, кому хватит квоты и у кого меньше часов.
        best_username: str | None = None
        best_load = None
        for username in candidates:
            teacher = teachers[username]
            projected = used_hours[username] + cs.weekly_hours
            if projected > teacher.workload_hours:
                continue
            if best_username is None or used_hours[username] < best_load:
                best_username = username
                best_load = used_hours[username]
        if best_username is None:
            warnings.append(
                f'Все учителя предмета «{subject_name}» переполнены — '
                f'не назначен {cs.class_obj.name} ({cs.weekly_hours} ч).'
            )
            continue
        teacher = teachers[best_username]
        assignment, _ = TeachingAssignment.objects.update_or_create(
            teacher=teacher,
            subject=cs.subject,
            class_obj=cs.class_obj,
            defaults={'hours_per_week': cs.weekly_hours},
        )
        assignments.append(assignment)
        used_hours[best_username] = best_load + cs.weekly_hours

    return assignments, warnings


class Command(BaseCommand):
    help = 'Заполнить БД реальными данными школы из «данные.docx» (16 учителей, 22 класса, 26 кабинетов).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--generate',
            action='store_true',
            help='После сидинга запустить генерацию расписания на текущую неделю.',
        )
        parser.add_argument(
            '--wipe',
            action='store_true',
            help='Перед сидингом удалить существующих учителей, классы, предметы и расписание.',
        )
        parser.add_argument(
            '--time-limit-minutes',
            type=float,
            default=3.0,
            help='Лимит времени GA в минутах (только при --generate). По умолчанию 3.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        User = get_user_model()

        if options['wipe']:
            self.stdout.write('Удаляю существующие демо-данные…')
            Schedule.objects.all().delete()
            TeacherAvailability.objects.all().delete()
            TeachingAssignment.objects.all().delete()
            ClassSubject.objects.all().delete()
            Subject.objects.all().delete()
            Class.objects.all().delete()
            Classroom.objects.all().delete()
            TimeSlot.objects.all().delete()
            LessonTime.objects.all().delete()
            Teacher.objects.all().delete()
            User.objects.filter(role=UserRole.TEACHER).delete()

        # 0. Админ.
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

        # 1. Классы.
        classes: dict[str, Class] = {}
        for name, grade, parallel, level, students in CLASS_SPEC:
            classes[name], _ = Class.objects.update_or_create(
                name=name,
                defaults={
                    'grade': grade,
                    'parallel': parallel,
                    'education_level': level,
                    'students_count': students,
                },
            )

        # 2. Предметы.
        subjects: dict[str, Subject] = {}
        for name, room_type, daily_limit in SUBJECT_SPEC:
            subjects[name], _ = Subject.objects.update_or_create(
                name=name,
                defaults={
                    'required_room_type': room_type,
                    'max_lessons_per_day': daily_limit,
                },
            )

        # 3. Кабинеты.
        for name, capacity, room_type in CLASSROOM_SPEC:
            Classroom.objects.update_or_create(
                name=name,
                defaults={'capacity': capacity, 'room_type': room_type},
            )

        # 4. Сетка уроков.
        slots_by_weekday: dict[int, list[TimeSlot]] = {}
        for lesson_number, start_at, end_at in LESSON_TIMES:
            lesson_time, _ = LessonTime.objects.update_or_create(
                lesson_number=lesson_number,
                day_type='normal',
                defaults={'start_time': start_at, 'end_time': end_at},
            )
            for weekday in SCHOOL_WEEKDAYS:
                slot, _ = TimeSlot.objects.update_or_create(weekday=weekday, lesson_time=lesson_time)
                slots_by_weekday.setdefault(weekday, []).append(slot)

        # 5. Учителя.
        teachers: dict[str, Teacher] = {}
        teacher_subjects: dict[str, list[str]] = {}
        for username, full_name, qualification, workload, daily_limit, subject_list in TEACHER_SPEC:
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
            teacher, _ = Teacher.objects.update_or_create(
                user=user,
                defaults={
                    'qualification': qualification,
                    'workload_hours': workload,
                    'max_lessons_per_day': daily_limit,
                },
            )
            teachers[username] = teacher
            teacher_subjects[username] = subject_list

        # 6. ClassSubject — нагрузка по плану.
        class_subjects: list[ClassSubject] = []
        for class_name, class_obj in classes.items():
            plan = _build_class_subject_plan(class_name, class_obj.grade)
            for subject_name, weekly_hours in plan.items():
                cs, _ = ClassSubject.objects.update_or_create(
                    class_obj=class_obj,
                    subject=subjects[subject_name],
                    defaults={'weekly_hours': weekly_hours},
                )
                class_subjects.append(cs)

        # 7. TeachingAssignment — раскидываем учителей с учётом квоты.
        # Удаляем старые, чтобы не накопились дубликаты после переsiding.
        TeachingAssignment.objects.all().delete()
        _, warnings = _assign_teachers_to_class_subjects(
            class_subjects=class_subjects,
            teachers=teachers,
            teacher_subjects=teacher_subjects,
        )
        for warning in warnings:
            self.stdout.write(self.style.WARNING(warning))

        # 8. Доступность учителей: по умолчанию доступны на всех слотах.
        TeacherAvailability.objects.all().delete()
        all_slots = list(TimeSlot.objects.all())
        ta_objects = [
            TeacherAvailability(teacher=teacher, time_slot=slot, is_available=True)
            for teacher in teachers.values()
            for slot in all_slots
        ]
        TeacherAvailability.objects.bulk_create(ta_objects, batch_size=500)

        self.stdout.write(self.style.SUCCESS(
            f'Готово: классов {len(classes)}, предметов {len(subjects)}, '
            f'учителей {len(teachers)}, кабинетов {Classroom.objects.count()}, '
            f'ClassSubject {ClassSubject.objects.count()}, '
            f'TeachingAssignment {TeachingAssignment.objects.count()}.'
        ))
        self.stdout.write('Логин администратора: admin / admin12345')
        self.stdout.write('Логин учителей:        belova, soloviev, … / teacher12345')

        if options['generate']:
            week_start = date.today() - timedelta(days=date.today().weekday())
            self.stdout.write(self.style.NOTICE(
                f'Запускаю генерацию расписания на неделю {week_start} '
                f'с лимитом времени GA {options["time_limit_minutes"]} мин.'
            ))
            generator = GeneticScheduleGenerator(
                population_size=120,
                generations=100_000,
                mutation_rate=0.18,
                ga_time_limit_seconds=options['time_limit_minutes'] * 60.0,
            )
            result = generator.generate(week_start)
            self.stdout.write(self.style.SUCCESS(
                f'Сгенерировано {result.created_lessons} занятий, '
                f'hard={result.hard_penalty}, soft={result.soft_penalty}.'
            ))
            for warning in result.warnings[:8]:
                self.stdout.write(f'  • {warning}')
