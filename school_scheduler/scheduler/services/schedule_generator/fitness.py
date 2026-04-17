from collections import defaultdict

from .chromosome import Chromosome
from .data_loader import GenerationContext
from .school_rules import alternation_group, allows_double_lesson, is_hard_subject, is_pe_subject, is_primary_grade


HARD_WEIGHTS = {
    'class_overlap': 1300,
    'teacher_overlap': 1300,
    'room_overlap': 1300,
    'teacher_unavailable': 900,
    'room_type': 850,
    'room_capacity': 850,
    'subject_daily_limit': 550,
    'class_daily_overload': 900,
    'class_weekly_overload': 850,
    'forbidden_double_lesson': 650,
}

SOFT_WEIGHTS = {
    'teacher_daily_overload': 90,
    'class_gap': 140,
    'teacher_gap': 60,
    'class_late_start': 110,
    'teacher_late_start': 25,
    'class_daily_imbalance': 95,
    'class_sparse_days': 70,
    'hard_subject_weekday_mismatch': 35,
    'subject_alternation': 30,
}


def evaluate_chromosome(chromosome: Chromosome, context: GenerationContext) -> Chromosome:
    diagnostics = defaultdict(int)
    slot_map = {slot.id: slot for slot in context.time_slots}

    class_slot_usage = defaultdict(int)
    teacher_slot_usage = defaultdict(int)
    room_slot_usage = defaultdict(int)
    class_subject_daily = defaultdict(int)
    class_daily_counts = defaultdict(int)
    class_weekly_counts = defaultdict(int)
    teacher_daily = defaultdict(int)
    class_daily_numbers = defaultdict(list)
    teacher_daily_numbers = defaultdict(list)
    class_daily_lessons: dict[tuple[int, int], list[tuple[int, str, str]]] = defaultdict(list)

    for fixed in context.fixed_lessons:
        weekday = fixed.lesson_date.isoweekday()
        class_slot_usage[(fixed.class_id, weekday, fixed.time_slot_id)] += 1
        teacher_slot_usage[(fixed.teacher_id, weekday, fixed.time_slot_id)] += 1
        room_slot_usage[(fixed.classroom_id, weekday, fixed.time_slot_id)] += 1
        class_subject_daily[(fixed.class_id, fixed.subject_id, weekday)] += 1
        class_daily_counts[(fixed.class_id, weekday)] += 1
        class_weekly_counts[fixed.class_id] += 1

        fixed_slot = slot_map[fixed.time_slot_id]
        teacher_daily[(fixed.teacher_id, weekday)] += 1
        class_daily_numbers[(fixed.class_id, weekday)].append(fixed_slot.lesson_number)
        teacher_daily_numbers[(fixed.teacher_id, weekday)].append(fixed_slot.lesson_number)
        class_daily_lessons[(fixed.class_id, weekday)].append(
            (fixed_slot.lesson_number, fixed.subject_name, fixed.required_room_type)
        )

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
        class_daily_counts[(requirement.class_id, weekday)] += 1
        class_weekly_counts[requirement.class_id] += 1
        teacher_daily[(requirement.teacher_id, weekday)] += 1
        class_daily_numbers[(requirement.class_id, weekday)].append(slot.lesson_number)
        teacher_daily_numbers[(requirement.teacher_id, weekday)].append(slot.lesson_number)
        class_daily_lessons[(requirement.class_id, weekday)].append(
            (slot.lesson_number, requirement.subject_name, requirement.required_room_type)
        )

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

    subject_limits: dict[tuple[int, int], int] = {}
    for requirement in context.lesson_requirements:
        subject_limits[(requirement.class_id, requirement.subject_id)] = requirement.daily_limit

    for (class_id, subject_id, weekday), count in class_subject_daily.items():
        daily_limit = subject_limits.get((class_id, subject_id), 2)
        if count > daily_limit:
            diagnostics['subject_daily_limit'] += count - daily_limit

    teacher_limits: dict[int, int] = {}
    for requirement in context.lesson_requirements:
        teacher_limits[requirement.teacher_id] = requirement.teacher_daily_limit
    for (teacher_id, _weekday), count in teacher_daily.items():
        if count > teacher_limits.get(teacher_id, 5):
            diagnostics['teacher_daily_overload'] += count - teacher_limits.get(teacher_id, 5)

    for (class_id, weekday), count in class_daily_counts.items():
        class_grade = context.class_grades.get(class_id, 1)
        daily_limit = context.class_daily_limits.get(class_id, 7)
        allowed = daily_limit + _daily_pe_bonus(class_grade, class_daily_lessons.get((class_id, weekday), []))
        if count > allowed:
            diagnostics['class_daily_overload'] += count - allowed

    for class_id, count in class_weekly_counts.items():
        weekly_limit = context.class_weekly_limits.get(class_id, 0)
        if weekly_limit > 0 and count > weekly_limit:
            diagnostics['class_weekly_overload'] += count - weekly_limit

    diagnostics['forbidden_double_lesson'] = _count_forbidden_double_lessons(
        class_daily_lessons=class_daily_lessons,
        class_grades=context.class_grades,
    )
    diagnostics['class_gap'] = _count_gaps(class_daily_numbers)
    diagnostics['teacher_gap'] = _count_gaps(teacher_daily_numbers)
    diagnostics['class_late_start'] = _count_late_starts(class_daily_numbers)
    diagnostics['teacher_late_start'] = _count_late_starts(teacher_daily_numbers)
    diagnostics['class_daily_imbalance'] = _count_daily_imbalance(class_daily_counts, class_weekly_counts)
    diagnostics['class_sparse_days'] = _count_sparse_days(class_daily_counts, class_weekly_counts)
    diagnostics['hard_subject_weekday_mismatch'] = _count_hard_subject_weekday_mismatch(
        class_daily_lessons=class_daily_lessons,
        class_grades=context.class_grades,
    )
    diagnostics['subject_alternation'] = _count_subject_alternation(
        class_daily_lessons=class_daily_lessons,
        class_grades=context.class_grades,
    )

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
            day_weight = 2 if len(numbers) >= 3 else 1
            penalty += (first_lesson - 1) * day_weight
    return penalty


def _count_daily_imbalance(
    class_daily_counts: dict[tuple[int, int], int],
    class_weekly_counts: dict[int, int],
) -> int:
    grouped: dict[int, dict[int, int]] = defaultdict(dict)
    for (class_id, weekday), count in class_daily_counts.items():
        grouped[class_id][weekday] = count

    penalty = 0
    for class_id, weekly_total in class_weekly_counts.items():
        if weekly_total <= 0:
            continue
        expected = weekly_total / 5.0
        for weekday in range(1, 6):
            count = grouped[class_id].get(weekday, 0)
            penalty += int(abs(count - expected) * 2)
    return penalty


def _count_sparse_days(
    class_daily_counts: dict[tuple[int, int], int],
    class_weekly_counts: dict[int, int],
) -> int:
    grouped: dict[int, dict[int, int]] = defaultdict(dict)
    for (class_id, weekday), count in class_daily_counts.items():
        grouped[class_id][weekday] = count

    penalty = 0
    for class_id, weekly_total in class_weekly_counts.items():
        if weekly_total < 8:
            continue

        average = weekly_total / 5.0
        lower_bound = max(1, int(average) - 1)
        for weekday in range(1, 6):
            count = grouped[class_id].get(weekday, 0)
            if count < lower_bound:
                penalty += lower_bound - count
            if weekly_total >= 15 and count <= 1:
                penalty += 2
    return penalty


def _count_hard_subject_weekday_mismatch(
    class_daily_lessons: dict[tuple[int, int], list[tuple[int, str, str]]],
    class_grades: dict[int, int],
) -> int:
    by_class: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for (class_id, weekday), lessons in class_daily_lessons.items():
        grade = class_grades.get(class_id, 1)
        for _lesson_number, subject_name, _room_type in lessons:
            if is_hard_subject(subject_name, grade):
                by_class[class_id][weekday] += 1

    penalty = 0
    for class_id, hard_by_day in by_class.items():
        total_hard = sum(hard_by_day.values())
        if total_hard == 0:
            continue

        tue_wed = hard_by_day.get(2, 0) + hard_by_day.get(3, 0)
        mon_fri = hard_by_day.get(1, 0) + hard_by_day.get(5, 0)
        preferred_min = (total_hard + 1) // 2
        if tue_wed < preferred_min:
            penalty += preferred_min - tue_wed

        max_mon_fri = max(1, total_hard // 3)
        if mon_fri > max_mon_fri:
            penalty += mon_fri - max_mon_fri
    return penalty


def _count_subject_alternation(
    class_daily_lessons: dict[tuple[int, int], list[tuple[int, str, str]]],
    class_grades: dict[int, int],
) -> int:
    penalty = 0
    for (class_id, _weekday), lessons in class_daily_lessons.items():
        if len(lessons) < 2:
            continue
        grade = class_grades.get(class_id, 1)
        ordered = sorted(lessons, key=lambda item: item[0])

        for previous, current in zip(ordered, ordered[1:]):
            prev_number, prev_subject, _prev_room = previous
            curr_number, curr_subject, _curr_room = current
            if curr_number != prev_number + 1:
                continue

            prev_group = alternation_group(prev_subject, grade)
            curr_group = alternation_group(curr_subject, grade)

            if is_primary_grade(grade):
                if prev_group in {'hard', 'light'} and prev_group == curr_group:
                    penalty += 1
            elif prev_group in {'stem', 'humanities'} and prev_group == curr_group:
                penalty += 1
    return penalty


def _count_forbidden_double_lessons(
    class_daily_lessons: dict[tuple[int, int], list[tuple[int, str, str]]],
    class_grades: dict[int, int],
) -> int:
    violations = 0
    for (class_id, _weekday), lessons in class_daily_lessons.items():
        if len(lessons) < 2:
            continue

        grade = class_grades.get(class_id, 1)
        ordered = sorted(lessons, key=lambda item: item[0])
        run_subject = ordered[0][1]
        run_room_type = ordered[0][2]
        run_length = 1
        prev_number = ordered[0][0]

        for lesson_number, subject_name, room_type in ordered[1:]:
            is_consecutive = lesson_number == prev_number + 1
            is_same_subject = subject_name == run_subject
            if is_consecutive and is_same_subject:
                run_length += 1
            else:
                violations += _run_violation(grade, run_subject, run_room_type, run_length)
                run_subject = subject_name
                run_room_type = room_type
                run_length = 1
            prev_number = lesson_number

        violations += _run_violation(grade, run_subject, run_room_type, run_length)

    return violations


def _run_violation(grade: int, subject_name: str, room_type: str, run_length: int) -> int:
    if run_length <= 1:
        return 0
    if run_length > 2:
        return run_length - 2
    if allows_double_lesson(grade, subject_name, room_type):
        return 0
    return run_length - 1


def _daily_pe_bonus(grade: int, lessons: list[tuple[int, str, str]]) -> int:
    if not is_primary_grade(grade):
        return 0
    return 1 if any(is_pe_subject(subject_name) for _n, subject_name, _room in lessons) else 0
