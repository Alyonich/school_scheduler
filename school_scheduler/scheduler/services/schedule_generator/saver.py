from datetime import date, timedelta
import time

from django.db import OperationalError, transaction

from scheduler.models import IntegrationLog, Schedule

from .chromosome import Chromosome
from .data_loader import GenerationContext


def persist_schedule(chromosome: Chromosome, context: GenerationContext) -> tuple[int, int]:
    week_end = context.week_start + timedelta(days=5)

    slot_lookup = {slot.id: slot for slot in context.time_slots}
    existing = list(
        Schedule.objects.filter(
            class_obj_id__in=context.class_ids,
            lesson_date__gte=context.week_start,
            lesson_date__lt=week_end,
            is_locked=True,
        )
    )
    used_class = {(item.class_obj_id, item.lesson_date, item.time_slot_id) for item in existing}
    used_teacher = {(item.teacher_id, item.lesson_date, item.time_slot_id) for item in existing}
    used_room = {(item.classroom_id, item.lesson_date, item.time_slot_id) for item in existing}
    daily_subject_counts = {}
    for item in existing:
        key = (item.class_obj_id, item.subject_id, item.lesson_date)
        daily_subject_counts[key] = daily_subject_counts.get(key, 0) + 1

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

        if class_key in used_class or teacher_key in used_teacher or room_key in used_room:
            continue
        if daily_subject_counts.get(subject_day_key, 0) >= requirement.daily_limit:
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
        return candidate

    return None


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
