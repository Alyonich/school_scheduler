from dataclasses import dataclass
from datetime import date, timedelta

from django.db.models import Prefetch

from scheduler.models import (
    Classroom,
    ClassSubject,
    Schedule,
    TeacherAvailability,
    TeachingAssignment,
    TimeSlot,
    WeeklyClassSubjectLoad,
    Weekday,
)


@dataclass(frozen=True)
class TimeSlotData:
    id: int
    weekday: int
    lesson_number: int
    label: str


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
    subject_id: int
    subject_name: str
    teacher_id: int
    teacher_name: str
    required_room_type: str
    min_capacity: int
    daily_limit: int
    teacher_daily_limit: int


@dataclass(frozen=True)
class FixedLesson:
    class_id: int
    subject_id: int
    teacher_id: int
    classroom_id: int
    time_slot_id: int
    lesson_date: date


@dataclass(frozen=True)
class GenerationContext:
    week_start: date
    class_ids: list[int]
    time_slots: list[TimeSlotData]
    classrooms: dict[int, ClassroomData]
    lesson_requirements: list[LessonRequirement]
    fixed_lessons: list[FixedLesson]
    teacher_unavailability: set[tuple[int, int]]
    warnings: list[str]


def load_generation_context(week_start: date, class_ids: list[int] | None = None) -> GenerationContext:
    target_classes = list(class_ids or [])
    if not target_classes:
        target_classes = list(ClassSubject.objects.values_list('class_obj_id', flat=True).distinct())

    time_slots = [
        TimeSlotData(
            id=slot.id,
            weekday=slot.weekday,
            lesson_number=slot.lesson_time.lesson_number,
            label=f'{slot.get_weekday_display()} · lesson {slot.lesson_time.lesson_number}',
        )
        for slot in TimeSlot.objects.select_related('lesson_time').filter(
            weekday__in=[Weekday.MONDAY, Weekday.TUESDAY, Weekday.WEDNESDAY, Weekday.THURSDAY, Weekday.FRIDAY]
        ).order_by('weekday', 'lesson_time__lesson_number')
    ]

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
            lesson_date__lt=week_start + timedelta(days=5),
            is_locked=True,
        )
        .order_by('lesson_date', 'time_slot__lesson_time__lesson_number')
    )

    fixed_lessons = [
        FixedLesson(
            class_id=item.class_obj_id,
            subject_id=item.subject_id,
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

    assignments = (
        TeachingAssignment.objects.select_related(
            'teacher__user',
            'class_obj',
            'subject',
        )
        .filter(class_obj_id__in=target_classes)
        .order_by('class_obj__name', 'subject__name', 'teacher__user__full_name')
    )

    assignments_by_pair: dict[tuple[int, int], list[TeachingAssignment]] = {}
    for assignment in assignments:
        assignments_by_pair.setdefault((assignment.class_obj_id, assignment.subject_id), []).append(assignment)

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

    fixed_counts: dict[tuple[int, int, int], int] = {}
    for fixed in fixed_lessons:
        key = (fixed.class_id, fixed.subject_id, fixed.teacher_id)
        fixed_counts[key] = fixed_counts.get(key, 0) + 1

    lesson_requirements: list[LessonRequirement] = []
    warnings: list[str] = []

    for class_subject in class_subjects:
        target_weekly_hours = class_subject_hours.get(class_subject.id, class_subject.weekly_hours)
        if target_weekly_hours <= 0:
            continue
        key = (class_subject.class_obj_id, class_subject.subject_id)
        assignment_group = assignments_by_pair.get(key, [])
        if not assignment_group:
            warnings.append(
                f'No teacher assignment for {class_subject.class_obj.name} / {class_subject.subject.name}.'
            )
            continue

        total_assigned_hours = 0
        normalized_hours: list[tuple[TeachingAssignment, int]] = []
        for assignment in assignment_group:
            hours = assignment.hours_per_week or 0
            total_assigned_hours += hours
            normalized_hours.append((assignment, hours))

        if total_assigned_hours == 0:
            normalized_hours[0] = (normalized_hours[0][0], target_weekly_hours)
            total_assigned_hours = target_weekly_hours

        if total_assigned_hours < target_weekly_hours:
            lead_assignment, lead_hours = normalized_hours[0]
            normalized_hours[0] = (
                lead_assignment,
                lead_hours + (target_weekly_hours - total_assigned_hours),
            )
        elif total_assigned_hours > target_weekly_hours:
            warnings.append(
                f'Assignments for {class_subject.class_obj.name} / {class_subject.subject.name} exceed weekly hours.'
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
                        subject_id=assignment.subject_id,
                        subject_name=assignment.subject.name,
                        teacher_id=assignment.teacher_id,
                        teacher_name=str(assignment.teacher),
                        required_room_type=assignment.subject.required_room_type,
                        min_capacity=assignment.class_obj.students_count,
                        daily_limit=assignment.subject.max_lessons_per_day,
                        teacher_daily_limit=assignment.teacher.max_lessons_per_day,
                    )
                )

    return GenerationContext(
        week_start=week_start,
        class_ids=target_classes,
        time_slots=time_slots,
        classrooms=classrooms,
        lesson_requirements=lesson_requirements,
        fixed_lessons=fixed_lessons,
        teacher_unavailability=teacher_unavailability,
        warnings=warnings,
    )
