from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, time, timedelta

from scheduler.models import (
    Class,
    Classroom,
    ClassSubject,
    Schedule,
    TeacherAvailability,
    TeachingAssignment,
    TimeSlot,
    WeeklyClassSubjectLoad,
)
from .configuration import SchedulerSettings, load_scheduler_settings
from .input_models import SchoolInputModel
from .sanpin_validator import SanPinValidator, TimeGridEntry, is_hard_subject, is_pe_subject
from .school_rules import alternation_group, allows_double_lesson


@dataclass(frozen=True)
class TeacherPreferenceData:
    avoid_first_lesson: bool = False
    avoid_last_lesson: bool = False
    preferred_weekdays: tuple[int, ...] = ()
    avoid_weekdays: tuple[int, ...] = ()
    preferred_lesson_numbers: tuple[int, ...] = ()
    avoid_lesson_numbers: tuple[int, ...] = ()


@dataclass(frozen=True)
class TimeSlotData:
    id: int
    weekday: int
    weekday_index: int
    lesson_number: int
    label: str
    start_time: time
    end_time: time


@dataclass(frozen=True)
class ClassroomData:
    id: int
    name: str
    room_type: str
    capacity: int


@dataclass(frozen=True)
class LessonRequirement:
    lesson_id: str
    class_id: int
    class_name: str
    class_grade: int
    class_daily_limit: int
    class_weekly_limit: int
    subject_id: int
    subject_name: str
    difficulty_score: int
    is_pe_lesson: bool
    is_hard_subject: bool
    alternation_group: str
    allows_double_lesson: bool
    teacher_id: int
    teacher_name: str
    teacher_preferences: TeacherPreferenceData
    required_room_type: str
    min_capacity: int
    daily_limit: int
    teacher_daily_limit: int


@dataclass(frozen=True)
class FixedLesson:
    class_id: int
    class_grade: int
    subject_id: int
    subject_name: str
    difficulty_score: int
    required_room_type: str
    teacher_id: int
    classroom_id: int
    time_slot_id: int
    lesson_date: date


@dataclass(frozen=True)
class GenerationContext:
    week_start: date
    class_ids: list[int]
    class_names: dict[int, str]
    class_grades: dict[int, int]
    class_daily_limits: dict[int, int]
    class_weekly_limits: dict[int, int]
    class_daily_targets: dict[int, float]
    time_slots: list[TimeSlotData]
    classrooms: dict[int, ClassroomData]
    lesson_requirements: list[LessonRequirement]
    fixed_lessons: list[FixedLesson]
    teacher_unavailability: set[tuple[int, int]]
    warnings: list[str]
    settings: SchedulerSettings
    sanpin_validator: SanPinValidator
    weekday_numbers: tuple[int, ...]
    slot_id_by_weekday_and_number: dict[tuple[int, int], int]
    class_index_map: dict[int, int]
    teacher_index_map: dict[int, int]
    room_index_map: dict[int, int]
    subject_index_map: dict[int, int]


def load_generation_context(
    week_start: date,
    class_ids: list[int] | None = None,
    settings: SchedulerSettings | None = None,
) -> GenerationContext:
    settings = settings or load_scheduler_settings()
    sanpin_validator = SanPinValidator(settings.school, settings.sanpin)
    weekday_numbers = tuple(settings.school.weekdays)

    target_classes = list(class_ids or [])
    if not target_classes:
        target_classes = list(ClassSubject.objects.values_list('class_obj_id', flat=True).distinct())
    if not target_classes:
        target_classes = list(Class.objects.values_list('id', flat=True))

    class_objects = list(Class.objects.filter(id__in=target_classes).order_by('grade', 'parallel'))
    target_classes = [item.id for item in class_objects]
    class_names = {item.id: item.name for item in class_objects}
    class_grades = {item.id: item.grade for item in class_objects}
    class_daily_limits = {
        item.id: min(
            sanpin_validator.daily_lesson_limit(item.grade, study_days=len(weekday_numbers)),
            settings.school.max_lessons_per_day,
        )
        for item in class_objects
    }
    class_weekly_limits = {
        item.id: sanpin_validator.weekly_lesson_limit(item.grade, study_days=len(weekday_numbers))
        for item in class_objects
    }

    time_slots = [
        TimeSlotData(
            id=slot.id,
            weekday=slot.weekday,
            weekday_index=weekday_numbers.index(slot.weekday),
            lesson_number=slot.lesson_time.lesson_number,
            start_time=slot.lesson_time.start_time,
            end_time=slot.lesson_time.end_time,
            label=f'{slot.get_weekday_display()} · урок {slot.lesson_time.lesson_number}',
        )
        for slot in TimeSlot.objects.select_related('lesson_time').filter(
            weekday__in=list(weekday_numbers)
        ).order_by('weekday', 'lesson_time__lesson_number')
    ]
    warnings = list(
        sanpin_validator.validate_time_grid(
            [
                TimeGridEntry(
                    weekday=slot.weekday,
                    lesson_number=slot.lesson_number,
                    start_time=slot.start_time,
                    end_time=slot.end_time,
                )
                for slot in time_slots
            ]
        )
    )
    warnings.extend(_build_duration_warnings(time_slots))

    slot_id_by_weekday_and_number = {
        (slot.weekday, slot.lesson_number): slot.id
        for slot in time_slots
    }

    classrooms = {
        room.id: ClassroomData(
            id=room.id,
            name=room.name,
            room_type=room.room_type,
            capacity=room.capacity,
        )
        for room in Classroom.objects.order_by('name')
    }

    fixed_schedules = list(
        Schedule.objects.select_related('class_obj', 'subject', 'teacher', 'classroom', 'time_slot')
        .filter(
            class_obj_id__in=target_classes,
            lesson_date__gte=week_start,
            lesson_date__lt=week_start + timedelta(days=len(weekday_numbers)),
            is_locked=True,
        )
        .order_by('lesson_date', 'time_slot__lesson_time__lesson_number')
    )

    fixed_lessons = [
        FixedLesson(
            class_id=item.class_obj_id,
            class_grade=item.class_obj.grade,
            subject_id=item.subject_id,
            subject_name=item.subject.name,
            difficulty_score=sanpin_validator.difficulty_score(item.subject.name, item.class_obj.grade),
            required_room_type=item.subject.required_room_type,
            teacher_id=item.teacher_id,
            classroom_id=item.classroom_id,
            time_slot_id=item.time_slot_id,
            lesson_date=item.lesson_date,
        )
        for item in fixed_schedules
    ]

    teacher_unavailability = set(
        TeacherAvailability.objects.filter(is_available=False).values_list('teacher_id', 'time_slot_id')
    )

    assignments = list(
        TeachingAssignment.objects.select_related(
            'teacher__user',
            'class_obj',
            'subject',
        )
        .filter(class_obj_id__in=target_classes)
        .order_by('class_obj__name', 'subject__name', 'teacher__user__full_name')
    )
    assignments_by_pair: dict[tuple[int, int], list[TeachingAssignment]] = defaultdict(list)
    for assignment in assignments:
        assignments_by_pair[(assignment.class_obj_id, assignment.subject_id)].append(assignment)

    class_subjects = list(
        ClassSubject.objects.select_related('class_obj', 'subject')
        .filter(class_obj_id__in=target_classes)
        .order_by('class_obj__name', 'subject__name')
    )
    weekly_overrides = dict(
        WeeklyClassSubjectLoad.objects.filter(
            week_start=week_start,
            class_subject_id__in=[item.id for item in class_subjects],
        ).values_list('class_subject_id', 'weekly_hours')
    )
    class_subject_hours = {
        item.id: weekly_overrides.get(item.id, item.weekly_hours)
        for item in class_subjects
    }

    _validate_school_input_with_pydantic(
        settings=settings,
        class_objects=class_objects,
        class_subjects=class_subjects,
        assignments=assignments,
        classrooms=classrooms,
    )

    fixed_class_totals: dict[int, int] = defaultdict(int)
    for fixed in fixed_lessons:
        fixed_class_totals[fixed.class_id] += 1

    _apply_weekly_caps_by_class(
        class_subjects=class_subjects,
        class_subject_hours=class_subject_hours,
        class_weekly_limits=class_weekly_limits,
        fixed_class_totals=fixed_class_totals,
        warnings=warnings,
    )
    _apply_slot_capacity_caps_by_class(
        class_subjects=class_subjects,
        class_subject_hours=class_subject_hours,
        weekly_slot_capacity=len(time_slots),
        fixed_class_totals=fixed_class_totals,
        warnings=warnings,
    )

    fixed_counts: dict[tuple[int, int, int], int] = defaultdict(int)
    for fixed in fixed_lessons:
        fixed_counts[(fixed.class_id, fixed.subject_id, fixed.teacher_id)] += 1

    lesson_requirements: list[LessonRequirement] = []
    teacher_preferences = defaultdict(TeacherPreferenceData)

    for class_subject in class_subjects:
        target_weekly_hours = class_subject_hours.get(class_subject.id, class_subject.weekly_hours)
        if target_weekly_hours <= 0:
            continue

        key = (class_subject.class_obj_id, class_subject.subject_id)
        assignment_group = assignments_by_pair.get(key, [])
        if not assignment_group:
            warnings.append(
                f'Нет назначения преподавателя для {class_subject.class_obj.name} / {class_subject.subject.name}.'
            )
            continue

        normalized_hours = _normalize_assignment_hours(
            assignment_group=assignment_group,
            target_weekly_hours=target_weekly_hours,
            warnings=warnings,
        )

        for assignment, assigned_hours in normalized_hours:
            remaining = max(
                0,
                assigned_hours - fixed_counts.get((assignment.class_obj_id, assignment.subject_id, assignment.teacher_id), 0),
            )
            for index in range(remaining):
                lesson_requirements.append(
                    LessonRequirement(
                        lesson_id=f'{assignment.class_obj_id}:{assignment.subject_id}:{assignment.teacher_id}:{index}',
                        class_id=assignment.class_obj_id,
                        class_name=assignment.class_obj.name,
                        class_grade=assignment.class_obj.grade,
                        class_daily_limit=class_daily_limits.get(assignment.class_obj_id, settings.school.max_lessons_per_day),
                        class_weekly_limit=class_weekly_limits.get(assignment.class_obj_id, 0),
                        subject_id=assignment.subject_id,
                        subject_name=assignment.subject.name,
                        difficulty_score=sanpin_validator.difficulty_score(assignment.subject.name, assignment.class_obj.grade),
                        is_pe_lesson=is_pe_subject(assignment.subject.name),
                        is_hard_subject=is_hard_subject(assignment.subject.name, assignment.class_obj.grade),
                        alternation_group=alternation_group(assignment.subject.name, assignment.class_obj.grade),
                        allows_double_lesson=allows_double_lesson(
                            assignment.class_obj.grade,
                            assignment.subject.name,
                            assignment.subject.required_room_type,
                        ),
                        teacher_id=assignment.teacher_id,
                        teacher_name=str(assignment.teacher),
                        teacher_preferences=teacher_preferences[assignment.teacher_id],
                        required_room_type=assignment.subject.required_room_type,
                        min_capacity=assignment.class_obj.students_count,
                        daily_limit=assignment.subject.max_lessons_per_day,
                        teacher_daily_limit=assignment.teacher.max_lessons_per_day,
                    )
                )

    class_weekly_targets = {class_id: fixed_class_totals.get(class_id, 0) for class_id in target_classes}
    for requirement in lesson_requirements:
        class_weekly_targets[requirement.class_id] = class_weekly_targets.get(requirement.class_id, 0) + 1
    school_day_count = max(1, len(weekday_numbers))
    class_daily_targets = {
        class_id: class_weekly_targets.get(class_id, 0) / school_day_count
        for class_id in target_classes
    }

    teacher_ids = sorted({requirement.teacher_id for requirement in lesson_requirements} | {item.teacher_id for item in fixed_lessons})
    subject_ids = sorted({requirement.subject_id for requirement in lesson_requirements})

    return GenerationContext(
        week_start=week_start,
        class_ids=target_classes,
        class_names=class_names,
        class_grades=class_grades,
        class_daily_limits=class_daily_limits,
        class_weekly_limits=class_weekly_limits,
        class_daily_targets=class_daily_targets,
        time_slots=time_slots,
        classrooms=classrooms,
        lesson_requirements=lesson_requirements,
        fixed_lessons=fixed_lessons,
        teacher_unavailability=teacher_unavailability,
        warnings=warnings,
        settings=settings,
        sanpin_validator=sanpin_validator,
        weekday_numbers=weekday_numbers,
        slot_id_by_weekday_and_number=slot_id_by_weekday_and_number,
        class_index_map={class_id: index for index, class_id in enumerate(target_classes)},
        teacher_index_map={teacher_id: index for index, teacher_id in enumerate(teacher_ids)},
        room_index_map={room_id: index for index, room_id in enumerate(sorted(classrooms))},
        subject_index_map={subject_id: index for index, subject_id in enumerate(subject_ids)},
    )


def _normalize_assignment_hours(
    assignment_group: list[TeachingAssignment],
    target_weekly_hours: int,
    warnings: list[str],
) -> list[tuple[TeachingAssignment, int]]:
    normalized_hours: list[tuple[TeachingAssignment, int]] = []
    total_assigned_hours = 0
    for assignment in assignment_group:
        hours = assignment.hours_per_week or 0
        total_assigned_hours += hours
        normalized_hours.append((assignment, hours))

    if total_assigned_hours == 0:
        normalized_hours[0] = (normalized_hours[0][0], target_weekly_hours)
        total_assigned_hours = target_weekly_hours

    if total_assigned_hours < target_weekly_hours:
        lead_assignment, lead_hours = normalized_hours[0]
        normalized_hours[0] = (lead_assignment, lead_hours + (target_weekly_hours - total_assigned_hours))
    elif total_assigned_hours > target_weekly_hours:
        warnings.append(
            f'Назначения преподавателей превышают недельную нагрузку предмета; лишние часы будут отброшены.'
        )
        remaining_hours = target_weekly_hours
        trimmed_hours: list[tuple[TeachingAssignment, int]] = []
        for assignment, assigned_hours in normalized_hours:
            if remaining_hours <= 0:
                break
            allocated_hours = min(assigned_hours, remaining_hours)
            if allocated_hours > 0:
                trimmed_hours.append((assignment, allocated_hours))
                remaining_hours -= allocated_hours
        normalized_hours = trimmed_hours
    return normalized_hours


def _apply_weekly_caps_by_class(
    class_subjects: list[ClassSubject],
    class_subject_hours: dict[int, int],
    class_weekly_limits: dict[int, int],
    fixed_class_totals: dict[int, int],
    warnings: list[str],
) -> None:
    subjects_by_class: dict[int, list[ClassSubject]] = defaultdict(list)
    for class_subject in class_subjects:
        subjects_by_class[class_subject.class_obj_id].append(class_subject)

    for class_id, items in subjects_by_class.items():
        weekly_limit = class_weekly_limits.get(class_id)
        if weekly_limit is None:
            continue

        fixed_hours = fixed_class_totals.get(class_id, 0)
        allowed_dynamic = max(0, weekly_limit - fixed_hours)
        requested_dynamic = sum(max(0, class_subject_hours.get(item.id, item.weekly_hours)) for item in items)
        if requested_dynamic <= allowed_dynamic:
            continue

        trimmed = _trim_hours_proportionally(
            values=[(item.id, max(0, class_subject_hours.get(item.id, item.weekly_hours))) for item in items],
            cap=allowed_dynamic,
        )
        for item in items:
            class_subject_hours[item.id] = trimmed.get(item.id, 0)

        warnings.append(
            f'Класс {items[0].class_obj.name}: недельная нагрузка снижена с {requested_dynamic} до '
            f'{allowed_dynamic} по лимиту СанПиН ({weekly_limit} часов в неделю).'
        )


def _apply_slot_capacity_caps_by_class(
    class_subjects: list[ClassSubject],
    class_subject_hours: dict[int, int],
    weekly_slot_capacity: int,
    fixed_class_totals: dict[int, int],
    warnings: list[str],
) -> None:
    if weekly_slot_capacity <= 0:
        return

    subjects_by_class: dict[int, list[ClassSubject]] = defaultdict(list)
    for class_subject in class_subjects:
        subjects_by_class[class_subject.class_obj_id].append(class_subject)

    for class_id, items in subjects_by_class.items():
        fixed_hours = fixed_class_totals.get(class_id, 0)
        allowed_dynamic = max(0, weekly_slot_capacity - fixed_hours)
        requested_dynamic = sum(max(0, class_subject_hours.get(item.id, item.weekly_hours)) for item in items)
        if requested_dynamic <= allowed_dynamic:
            continue

        trimmed = _trim_hours_proportionally(
            values=[(item.id, max(0, class_subject_hours.get(item.id, item.weekly_hours))) for item in items],
            cap=allowed_dynamic,
        )
        for item in items:
            class_subject_hours[item.id] = trimmed.get(item.id, 0)

        warnings.append(
            f'Класс {items[0].class_obj.name}: нагрузка снижена с {requested_dynamic} до {allowed_dynamic} '
            f'из-за емкости недельной сетки ({weekly_slot_capacity} доступных слотов).'
        )


def _trim_hours_proportionally(values: list[tuple[int, int]], cap: int) -> dict[int, int]:
    if cap <= 0:
        return {item_id: 0 for item_id, _hours in values}

    total = sum(hours for _item_id, hours in values)
    if total <= cap:
        return {item_id: hours for item_id, hours in values}
    if total == 0:
        return {item_id: 0 for item_id, _hours in values}

    trimmed: dict[int, int] = {}
    fractions: list[tuple[float, int, int]] = []
    for item_id, hours in values:
        scaled = (hours * cap) / total
        base = min(hours, int(scaled))
        trimmed[item_id] = base
        fractions.append((scaled - base, item_id, hours))

    remaining = cap - sum(trimmed.values())
    fractions.sort(reverse=True)
    while remaining > 0:
        progress = False
        for _fraction, item_id, original in fractions:
            if remaining <= 0:
                break
            if trimmed[item_id] >= original:
                continue
            trimmed[item_id] += 1
            remaining -= 1
            progress = True
        if not progress:
            break
    return trimmed


def _build_duration_warnings(time_slots: list[TimeSlotData]) -> list[str]:
    warnings: list[str] = []
    if not time_slots:
        return warnings

    monday_slots = sorted(
        [slot for slot in time_slots if slot.weekday == min(slot.weekday for slot in time_slots)],
        key=lambda slot: slot.lesson_number,
    )
    durations = [_minutes_between(slot.start_time, slot.end_time) for slot in monday_slots]
    if any(duration <= 0 for duration in durations):
        warnings.append('В сетке уроков найдены слоты с некорректной длительностью.')
    if any(duration not in {35, 40, 45} for duration in durations):
        warnings.append('Обнаружена нестандартная длительность урока; проверьте соответствие локальному режиму школы.')

    breaks: list[int] = []
    for previous, current in zip(monday_slots, monday_slots[1:]):
        break_minutes = _minutes_between(previous.end_time, current.start_time)
        breaks.append(break_minutes)
        if break_minutes < 10:
            warnings.append('Обнаружена перемена менее 10 минут.')
    if breaks and not any(20 <= break_minutes <= 30 for break_minutes in breaks):
        warnings.append('Рекомендуется добавить хотя бы одну большую перемену 20-30 минут.')
    return warnings


def _validate_school_input_with_pydantic(
    settings: SchedulerSettings,
    class_objects: list[Class],
    class_subjects: list[ClassSubject],
    assignments: list[TeachingAssignment],
    classrooms: dict[int, ClassroomData],
) -> None:
    hours_by_class: dict[int, dict[str, int]] = defaultdict(dict)
    for class_subject in class_subjects:
        hours_by_class[class_subject.class_obj_id][class_subject.subject.name] = class_subject.weekly_hours

    payload = {
        'school': {
            'name': settings.school.name,
            'shifts': settings.school.shifts,
            'start_time': settings.school.start_time.strftime('%H:%M'),
            'lesson_duration_minutes': settings.school.lesson_duration_minutes,
            'has_extracurricular': settings.school.has_extracurricular,
        },
        'teachers': [
            {
                'full_name': str(assignment.teacher),
                'subjects': sorted({item.subject.name for item in assignments if item.teacher_id == assignment.teacher_id}),
                'max_weekly_load': assignment.teacher.workload_hours,
                'max_lessons_per_day': assignment.teacher.max_lessons_per_day,
            }
            for assignment in assignments
        ],
        'classes': [
            {
                'name': class_obj.name,
                'grade': class_obj.grade,
                'students_count': class_obj.students_count,
                'parallel': class_obj.parallel,
                'weekly_subject_hours': hours_by_class.get(class_obj.id, {}),
            }
            for class_obj in class_objects
        ],
        'subjects': [
            {
                'name': class_subject.subject.name,
                'required_room_type': class_subject.subject.required_room_type,
                'max_lessons_per_day': class_subject.subject.max_lessons_per_day,
            }
            for class_subject in class_subjects
        ],
        'classrooms': [
            {
                'name': room.name,
                'capacity': room.capacity,
                'room_type': room.room_type,
            }
            for room in classrooms.values()
        ],
    }
    SchoolInputModel.model_validate(payload)


def _minutes_between(start: time, end: time) -> int:
    return (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
