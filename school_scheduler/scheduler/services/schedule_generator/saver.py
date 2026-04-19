from datetime import date, timedelta
import time

from django.db import OperationalError, transaction

from scheduler.models import IntegrationLog, Schedule

from .chromosome import Chromosome
from .data_loader import GenerationContext
from .school_rules import allows_double_lesson, is_pe_subject


def persist_schedule(chromosome: Chromosome, context: GenerationContext) -> tuple[int, int]:
    week_end = context.week_start + timedelta(days=len(context.weekday_numbers))

    slot_lookup = {slot.id: slot for slot in context.time_slots}
    existing = list(
        Schedule.objects.select_related('subject', 'time_slot__lesson_time').filter(
            lesson_date__gte=context.week_start,
            lesson_date__lt=week_end,
        ).exclude(
            class_obj_id__in=context.class_ids,
            is_locked=False,
        )
    )
    used_class = {(item.class_obj_id, item.lesson_date, item.time_slot_id) for item in existing}
    used_teacher = {(item.teacher_id, item.lesson_date, item.time_slot_id) for item in existing}
    used_room = {(item.classroom_id, item.lesson_date, item.time_slot_id) for item in existing}
    daily_subject_counts: dict[tuple[int, int, date], int] = {}
    teacher_daily_counts: dict[tuple[int, date], int] = {}
    class_daily_lessons: dict[tuple[int, date], list[tuple[int, str, str]]] = {}
    for item in existing:
        key = (item.class_obj_id, item.subject_id, item.lesson_date)
        daily_subject_counts[key] = daily_subject_counts.get(key, 0) + 1
        teacher_day_key = (item.teacher_id, item.lesson_date)
        teacher_daily_counts[teacher_day_key] = teacher_daily_counts.get(teacher_day_key, 0) + 1
        class_day_key = (item.class_obj_id, item.lesson_date)
        class_daily_lessons.setdefault(class_day_key, []).append(
            (
                item.time_slot.lesson_time.lesson_number,
                item.subject.name,
                item.subject.required_room_type,
            )
        )

    scheduled_items = list(zip(context.lesson_requirements, chromosome.placements))
    scheduled_items.sort(
        key=lambda item: (
            len(_compatible_rooms(item[0], context)) or 999,
            len(_available_slots(item[0], context)) or 999,
        )
    )

    staged_schedules: list[Schedule] = []
    skipped = 0

    for requirement, placement in scheduled_items:
        staged = _place_requirement(
            requirement=requirement,
            preferred_time_slot_id=placement.time_slot_id,
            preferred_classroom_id=placement.classroom_id,
            context=context,
            slot_lookup=slot_lookup,
            used_class=used_class,
            used_teacher=used_teacher,
            used_room=used_room,
            daily_subject_counts=daily_subject_counts,
            teacher_daily_counts=teacher_daily_counts,
            class_daily_lessons=class_daily_lessons,
        )
        if staged:
            staged_schedules.append(staged)
        else:
            skipped += 1

    created = _replace_week_schedule_with_retry(
        context=context,
        week_end=week_end,
        staged_schedules=staged_schedules,
    )
    skipped += max(0, len(staged_schedules) - created)

    _log_generation(
        context=context,
        created=created,
        skipped=skipped,
        hard_penalty=chromosome.hard_penalty,
        soft_penalty=chromosome.soft_penalty,
    )
    return created, skipped


def _place_requirement(
    requirement,
    preferred_time_slot_id: int,
    preferred_classroom_id: int,
    context: GenerationContext,
    slot_lookup: dict[int, object],
    used_class: set[tuple[int, date, int]],
    used_teacher: set[tuple[int, date, int]],
    used_room: set[tuple[int, date, int]],
    daily_subject_counts: dict[tuple[int, int, date], int],
    teacher_daily_counts: dict[tuple[int, date], int],
    class_daily_lessons: dict[tuple[int, date], list[tuple[int, str, str]]],
) -> Schedule | None:
    room_ids = _compatible_rooms(requirement, context)
    if not room_ids:
        return None

    preferred = [(preferred_time_slot_id, preferred_classroom_id)]
    all_options = []
    for slot in sorted(context.time_slots, key=lambda item: (item.lesson_number, item.weekday)):
        for room_id in room_ids:
            all_options.append((slot.id, room_id))
    options = preferred + [item for item in all_options if item != preferred[0]]

    for time_slot_id, room_id in options:
        slot = slot_lookup[time_slot_id]
        if (requirement.teacher_id, time_slot_id) in context.teacher_unavailability:
            continue

        lesson_date = context.week_start + timedelta(days=slot.weekday - 1)
        class_key = (requirement.class_id, lesson_date, time_slot_id)
        teacher_key = (requirement.teacher_id, lesson_date, time_slot_id)
        room_key = (room_id, lesson_date, time_slot_id)
        subject_day_key = (requirement.class_id, requirement.subject_id, lesson_date)
        teacher_day_key = (requirement.teacher_id, lesson_date)
        class_day_key = (requirement.class_id, lesson_date)

        if class_key in used_class or teacher_key in used_teacher or room_key in used_room:
            continue
        if daily_subject_counts.get(subject_day_key, 0) >= requirement.daily_limit:
            continue
        if teacher_daily_counts.get(teacher_day_key, 0) >= requirement.teacher_daily_limit:
            continue

        day_lessons = class_daily_lessons.get(class_day_key, [])
        projected_day = day_lessons + [(slot.lesson_number, requirement.subject_name, requirement.required_room_type)]
        if len(projected_day) > _allowed_daily_limit(requirement, projected_day):
            continue
        candidate = Schedule(
            class_obj_id=requirement.class_id,
            subject_id=requirement.subject_id,
            teacher_id=requirement.teacher_id,
            classroom_id=room_id,
            time_slot_id=time_slot_id,
            lesson_date=lesson_date,
            is_locked=False,
        )
        used_class.add(class_key)
        used_teacher.add(teacher_key)
        used_room.add(room_key)
        daily_subject_counts[subject_day_key] = daily_subject_counts.get(subject_day_key, 0) + 1
        teacher_daily_counts[teacher_day_key] = teacher_daily_counts.get(teacher_day_key, 0) + 1
        class_daily_lessons.setdefault(class_day_key, []).append(
            (slot.lesson_number, requirement.subject_name, requirement.required_room_type)
        )
        return candidate

    return None


def _allowed_daily_limit(requirement, day_lessons: list[tuple[int, str, str]]) -> int:
    limit = requirement.class_daily_limit
    if requirement.class_grade <= 4 and any(is_pe_subject(subject_name) for _n, subject_name, _room in day_lessons):
        limit += 1
    return limit


def _has_forbidden_double_lessons(class_grade: int, day_lessons: list[tuple[int, str, str]]) -> bool:
    if len(day_lessons) < 2:
        return False

    ordered = sorted(day_lessons, key=lambda item: item[0])
    run_subject = ordered[0][1]
    run_room_type = ordered[0][2]
    run_length = 1
    previous_number = ordered[0][0]

    for lesson_number, subject_name, room_type in ordered[1:]:
        is_consecutive = lesson_number == previous_number + 1
        is_same_subject = subject_name == run_subject
        if is_consecutive and is_same_subject:
            run_length += 1
        else:
            if _run_is_forbidden(class_grade, run_subject, run_room_type, run_length):
                return True
            run_subject = subject_name
            run_room_type = room_type
            run_length = 1
        previous_number = lesson_number

    return _run_is_forbidden(class_grade, run_subject, run_room_type, run_length)


def _run_is_forbidden(class_grade: int, subject_name: str, room_type: str, run_length: int) -> bool:
    if run_length <= 1:
        return False
    if run_length > 2:
        return True
    return not allows_double_lesson(class_grade, subject_name, room_type)


def _compatible_rooms(requirement, context: GenerationContext) -> list[int]:
    strict = [
        room_id
        for room_id, room in context.classrooms.items()
        if room.room_type == requirement.required_room_type and room.capacity >= requirement.min_capacity
    ]
    if strict:
        return strict
    return [
        room_id
        for room_id, room in context.classrooms.items()
        if room.capacity >= requirement.min_capacity
    ]


def _available_slots(requirement, context: GenerationContext) -> list[int]:
    return [
        slot.id
        for slot in context.time_slots
        if (requirement.teacher_id, slot.id) not in context.teacher_unavailability
    ]


def _replace_week_schedule_with_retry(
    context: GenerationContext,
    week_end: date,
    staged_schedules: list[Schedule],
    retries: int = 4,
) -> int:
    for attempt in range(retries):
        try:
            return _replace_week_schedule(context=context, week_end=week_end, staged_schedules=staged_schedules)
        except OperationalError as exc:
            message = str(exc).lower()
            if 'locked' not in message or attempt == retries - 1:
                raise
            time.sleep(0.2 * (attempt + 1))
    return 0


def _replace_week_schedule(
    context: GenerationContext,
    week_end: date,
    staged_schedules: list[Schedule],
) -> int:
    with transaction.atomic():
        week_qs = Schedule.objects.filter(
            class_obj_id__in=context.class_ids,
            lesson_date__gte=context.week_start,
            lesson_date__lt=week_end,
        )
        week_qs.filter(is_locked=False).delete()
        baseline_count = week_qs.count()
        if staged_schedules:
            Schedule.objects.bulk_create(staged_schedules, batch_size=200, ignore_conflicts=True)
        final_count = week_qs.count()
        return max(0, final_count - baseline_count)


def _log_generation(
    context: GenerationContext,
    created: int,
    skipped: int,
    hard_penalty: int,
    soft_penalty: int,
) -> None:
    try:
        IntegrationLog.objects.create(
            system_name='genetic_scheduler',
            operation=(
                f'Generated {created} lessons (skipped {skipped}) for week {context.week_start.isoformat()} '
                f'with hard_penalty={hard_penalty} and soft_penalty={soft_penalty}.'
            ),
        )
    except OperationalError:
        # Timetable was saved successfully; log write can be skipped if DB is temporarily busy.
        return
