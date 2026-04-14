from collections import defaultdict

from .chromosome import Chromosome
from .data_loader import GenerationContext


HARD_WEIGHTS = {
    'class_overlap': 1200,
    'teacher_overlap': 1200,
    'room_overlap': 1200,
    'teacher_unavailable': 800,
    'room_type': 800,
    'room_capacity': 800,
    'subject_daily_limit': 500,
}

SOFT_WEIGHTS = {
    'teacher_daily_overload': 80,
    'class_gap': 70,
    'teacher_gap': 50,
    'class_late_start': 45,
    'teacher_late_start': 20,
    'class_day_spread': 10,
}


def evaluate_chromosome(chromosome: Chromosome, context: GenerationContext) -> Chromosome:
    diagnostics = defaultdict(int)
    slot_map = {slot.id: slot for slot in context.time_slots}

    class_slot_usage = defaultdict(int)
    teacher_slot_usage = defaultdict(int)
    room_slot_usage = defaultdict(int)
    class_subject_daily = defaultdict(int)
    teacher_daily = defaultdict(int)
    class_daily_numbers = defaultdict(list)
    teacher_daily_numbers = defaultdict(list)

    for fixed in context.fixed_lessons:
        weekday = fixed.lesson_date.isoweekday()
        class_slot_usage[(fixed.class_id, weekday, fixed.time_slot_id)] += 1
        teacher_slot_usage[(fixed.teacher_id, weekday, fixed.time_slot_id)] += 1
        room_slot_usage[(fixed.classroom_id, weekday, fixed.time_slot_id)] += 1
        class_subject_daily[(fixed.class_id, fixed.subject_id, weekday)] += 1

        fixed_slot = slot_map[fixed.time_slot_id]
        teacher_daily[(fixed.teacher_id, weekday)] += 1
        class_daily_numbers[(fixed.class_id, weekday)].append(fixed_slot.lesson_number)
        teacher_daily_numbers[(fixed.teacher_id, weekday)].append(fixed_slot.lesson_number)

    for requirement, placement in zip(context.lesson_requirements, chromosome.placements):
        slot = slot_map[placement.time_slot_id]
        room = context.classrooms[placement.classroom_id]
        weekday = slot.weekday

        class_key = (requirement.class_id, weekday, placement.time_slot_id)
        teacher_key = (requirement.teacher_id, weekday, placement.time_slot_id)
        room_key = (placement.classroom_id, weekday, placement.time_slot_id)

        class_slot_usage[class_key] += 1
        teacher_slot_usage[teacher_key] += 1
        room_slot_usage[room_key] += 1
        class_subject_daily[(requirement.class_id, requirement.subject_id, weekday)] += 1
        teacher_daily[(requirement.teacher_id, weekday)] += 1
        class_daily_numbers[(requirement.class_id, weekday)].append(slot.lesson_number)
        teacher_daily_numbers[(requirement.teacher_id, weekday)].append(slot.lesson_number)

        if (requirement.teacher_id, placement.time_slot_id) in context.teacher_unavailability:
            diagnostics['teacher_unavailable'] += 1
        if room.room_type != requirement.required_room_type:
            diagnostics['room_type'] += 1
        if room.capacity < requirement.min_capacity:
            diagnostics['room_capacity'] += 1

    for count in class_slot_usage.values():
        if count > 1:
            diagnostics['class_overlap'] += count - 1
    for count in teacher_slot_usage.values():
        if count > 1:
            diagnostics['teacher_overlap'] += count - 1
    for count in room_slot_usage.values():
        if count > 1:
            diagnostics['room_overlap'] += count - 1

    for requirement in context.lesson_requirements:
        key = (requirement.class_id, requirement.subject_id, None)
        _ = key

    subject_limits = {}
    for requirement in context.lesson_requirements:
        subject_limits[(requirement.class_id, requirement.subject_id)] = requirement.daily_limit

    for (class_id, subject_id, weekday), count in class_subject_daily.items():
        daily_limit = subject_limits.get((class_id, subject_id), 2)
        if count > daily_limit:
            diagnostics['subject_daily_limit'] += count - daily_limit

    for requirement in context.lesson_requirements:
        limit_key = (requirement.teacher_id, requirement.teacher_daily_limit)
        _ = limit_key

    teacher_limits = {}
    for requirement in context.lesson_requirements:
        teacher_limits[requirement.teacher_id] = requirement.teacher_daily_limit

    for (teacher_id, _weekday), count in teacher_daily.items():
        if count > teacher_limits.get(teacher_id, 5):
            diagnostics['teacher_daily_overload'] += count - teacher_limits.get(teacher_id, 5)

    diagnostics['class_gap'] = _count_gaps(class_daily_numbers)
    diagnostics['teacher_gap'] = _count_gaps(teacher_daily_numbers)
    diagnostics['class_late_start'] = _count_late_starts(class_daily_numbers)
    diagnostics['teacher_late_start'] = _count_late_starts(teacher_daily_numbers)
    diagnostics['class_day_spread'] = _count_day_spread(class_daily_numbers)

    hard_penalty = sum(diagnostics[name] * weight for name, weight in HARD_WEIGHTS.items())
    soft_penalty = sum(diagnostics[name] * weight for name, weight in SOFT_WEIGHTS.items())

    chromosome.hard_penalty = hard_penalty
    chromosome.soft_penalty = soft_penalty
    chromosome.score = 100000 - hard_penalty - soft_penalty
    chromosome.diagnostics = dict(diagnostics)
    return chromosome


def _count_gaps(lessons_by_day: dict[tuple[int, int], list[int]]) -> int:
    gaps = 0
    for numbers in lessons_by_day.values():
        if len(numbers) < 2:
            continue
        ordered = sorted(set(numbers))
        gaps += max(0, (ordered[-1] - ordered[0] + 1) - len(ordered))
    return gaps


def _count_late_starts(lessons_by_day: dict[tuple[int, int], list[int]]) -> int:
    penalty = 0
    for numbers in lessons_by_day.values():
        if not numbers:
            continue
        first_lesson = min(numbers)
        if first_lesson > 1:
            penalty += first_lesson - 1
    return penalty


def _count_day_spread(lessons_by_day: dict[tuple[int, int], list[int]]) -> int:
    grouped = defaultdict(int)
    for (entity_id, weekday), numbers in lessons_by_day.items():
        grouped[entity_id] += len(numbers) * len(numbers)
        _ = weekday
    return max(0, sum(grouped.values()) // 10)
