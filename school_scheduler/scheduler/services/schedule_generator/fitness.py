from __future__ import annotations

from collections import defaultdict

import numpy as np

from .chromosome import Chromosome
from .data_loader import GenerationContext
from .sanpin_validator import LessonLoadEntry
from .school_rules import alternation_group, allows_double_lesson, is_hard_subject, is_pe_subject


HARD_WEIGHTS = {
    'class_overlap': 1400,
    'teacher_overlap': 1400,
    'room_overlap': 1400,
    'teacher_unavailable': 1000,
    'room_type': 1000,
    'room_capacity': 1000,
    'subject_daily_limit': 800,
    'class_daily_overload': 1000,
    'class_weekly_overload': 900,
    'teacher_daily_overload': 650,
}


def evaluate_chromosome(chromosome: Chromosome, context: GenerationContext) -> Chromosome:
    diagnostics: dict[str, int] = defaultdict(int)
    weights = context.settings.algorithm.weights
    slot_lookup = {slot.id: slot for slot in context.time_slots}
    day_count = max(1, len(context.weekday_numbers))
    max_lesson_number = max((slot.lesson_number for slot in context.time_slots), default=1)

    class_count = max(1, len(context.class_index_map))
    teacher_count = max(1, len(context.teacher_index_map))
    room_count = max(1, len(context.room_index_map))
    subject_count = max(1, len(context.subject_index_map))

    class_usage = np.zeros((class_count, day_count, max_lesson_number), dtype=np.int16)
    teacher_usage = np.zeros((teacher_count, day_count, max_lesson_number), dtype=np.int16)
    room_usage = np.zeros((room_count, day_count, max_lesson_number), dtype=np.int16)
    subject_daily = np.zeros((class_count, subject_count, day_count), dtype=np.int16)
    class_daily_counts = np.zeros((class_count, day_count), dtype=np.int16)
    teacher_daily_counts = np.zeros((teacher_count, day_count), dtype=np.int16)
    class_weekly_counts = np.zeros(class_count, dtype=np.int16)
    daily_scores = np.zeros((class_count, day_count), dtype=np.int16)
    weekly_scores = np.zeros(class_count, dtype=np.int16)

    class_daily_numbers: dict[tuple[int, int], list[int]] = defaultdict(list)
    teacher_daily_numbers: dict[tuple[int, int], list[int]] = defaultdict(list)
    class_daily_lessons: dict[tuple[int, int], list[tuple[int, str, str]]] = defaultdict(list)
    class_day_has_pe: dict[tuple[int, int], bool] = defaultdict(bool)
    lesson_load_entries: list[LessonLoadEntry] = []

    last_lesson_by_weekday = {
        weekday: max(
            (slot.lesson_number for slot in context.time_slots if slot.weekday == weekday),
            default=max_lesson_number,
        )
        for weekday in context.weekday_numbers
    }

    subject_limits: dict[tuple[int, int], int] = {}
    teacher_limits: dict[int, int] = {}
    for requirement in context.lesson_requirements:
        subject_limits[(requirement.class_id, requirement.subject_id)] = requirement.daily_limit
        teacher_limits[requirement.teacher_id] = requirement.teacher_daily_limit

    def register_lesson(
        *,
        class_id: int,
        class_grade: int,
        subject_id: int,
        subject_name: str,
        difficulty_score: int,
        teacher_id: int,
        room_id: int,
        slot_id: int,
        required_room_type: str,
        room_type: str,
        room_capacity: int,
        min_capacity: int,
        teacher_unavailable: bool,
        preferences,
    ) -> None:
        slot = slot_lookup[slot_id]
        day_index = slot.weekday_index
        slot_index = slot.lesson_number - 1
        class_index = context.class_index_map[class_id]
        teacher_index = context.teacher_index_map[teacher_id]
        room_index = context.room_index_map[room_id]
        subject_index = context.subject_index_map.get(subject_id, 0)

        class_usage[class_index, day_index, slot_index] += 1
        teacher_usage[teacher_index, day_index, slot_index] += 1
        room_usage[room_index, day_index, slot_index] += 1
        subject_daily[class_index, subject_index, day_index] += 1
        class_daily_counts[class_index, day_index] += 1
        teacher_daily_counts[teacher_index, day_index] += 1
        class_weekly_counts[class_index] += 1
        daily_scores[class_index, day_index] += difficulty_score
        weekly_scores[class_index] += difficulty_score
        class_daily_numbers[(class_id, slot.weekday)].append(slot.lesson_number)
        teacher_daily_numbers[(teacher_id, slot.weekday)].append(slot.lesson_number)
        class_daily_lessons[(class_id, slot.weekday)].append((slot.lesson_number, subject_name, required_room_type))
        class_day_has_pe[(class_id, slot.weekday)] = class_day_has_pe[(class_id, slot.weekday)] or is_pe_subject(subject_name)

        lesson_load_entries.append(
            LessonLoadEntry(
                class_id=class_id,
                class_grade=class_grade,
                subject_name=subject_name,
                weekday=slot.weekday,
                lesson_number=slot.lesson_number,
                difficulty_score=difficulty_score,
                is_pe=is_pe_subject(subject_name),
            )
        )

        if teacher_unavailable:
            diagnostics['teacher_unavailable'] += 1
        if room_type != required_room_type:
            diagnostics['room_type'] += 1
        if room_capacity < min_capacity:
            diagnostics['room_capacity'] += 1
        if preferences.avoid_first_lesson and slot.lesson_number == 1:
            diagnostics['teacher_preference_violations'] += 1
        if preferences.avoid_last_lesson and slot.lesson_number == last_lesson_by_weekday.get(slot.weekday, max_lesson_number):
            diagnostics['teacher_preference_violations'] += 1
        if preferences.preferred_weekdays and slot.weekday not in preferences.preferred_weekdays:
            diagnostics['teacher_preference_violations'] += 1
        if preferences.avoid_weekdays and slot.weekday in preferences.avoid_weekdays:
            diagnostics['teacher_preference_violations'] += 1
        if preferences.preferred_lesson_numbers and slot.lesson_number not in preferences.preferred_lesson_numbers:
            diagnostics['teacher_preference_violations'] += 1
        if preferences.avoid_lesson_numbers and slot.lesson_number in preferences.avoid_lesson_numbers:
            diagnostics['teacher_preference_violations'] += 1
        if difficulty_score >= 8 and slot.lesson_number in {1, last_lesson_by_weekday.get(slot.weekday, max_lesson_number)}:
            diagnostics['hard_subject_position_violations'] += 1

    for fixed in context.fixed_lessons:
        room = context.classrooms[fixed.classroom_id]
        register_lesson(
            class_id=fixed.class_id,
            class_grade=fixed.class_grade,
            subject_id=fixed.subject_id,
            subject_name=fixed.subject_name,
            difficulty_score=fixed.difficulty_score,
            teacher_id=fixed.teacher_id,
            room_id=fixed.classroom_id,
            slot_id=fixed.time_slot_id,
            required_room_type=fixed.required_room_type,
            room_type=room.room_type,
            room_capacity=room.capacity,
            min_capacity=0,
            teacher_unavailable=False,
            preferences=_empty_preferences(),
        )

    for requirement, placement in zip(context.lesson_requirements, chromosome.placements):
        room = context.classrooms[placement.classroom_id]
        register_lesson(
            class_id=requirement.class_id,
            class_grade=requirement.class_grade,
            subject_id=requirement.subject_id,
            subject_name=requirement.subject_name,
            difficulty_score=requirement.difficulty_score,
            teacher_id=requirement.teacher_id,
            room_id=placement.classroom_id,
            slot_id=placement.time_slot_id,
            required_room_type=requirement.required_room_type,
            room_type=room.room_type,
            room_capacity=room.capacity,
            min_capacity=requirement.min_capacity,
            teacher_unavailable=(requirement.teacher_id, placement.time_slot_id) in context.teacher_unavailability,
            preferences=requirement.teacher_preferences,
        )

    diagnostics['class_overlap'] = int(np.clip(class_usage - 1, 0, None).sum())
    diagnostics['teacher_overlap'] = int(np.clip(teacher_usage - 1, 0, None).sum())
    diagnostics['room_overlap'] = int(np.clip(room_usage - 1, 0, None).sum())

    for (class_id, subject_id), daily_limit in subject_limits.items():
        class_index = context.class_index_map[class_id]
        subject_index = context.subject_index_map.get(subject_id, 0)
        counts = subject_daily[class_index, subject_index]
        excess = np.clip(counts - daily_limit, 0, None)
        diagnostics['subject_daily_limit'] += int(excess.sum())

    for teacher_id, teacher_index in context.teacher_index_map.items():
        teacher_limit = teacher_limits.get(teacher_id, 6)
        excess = np.clip(teacher_daily_counts[teacher_index] - teacher_limit, 0, None)
        diagnostics['teacher_daily_overload'] += int(excess.sum())

    for class_id, class_index in context.class_index_map.items():
        grade = context.class_grades.get(class_id, 1)
        for day_index, weekday in enumerate(context.weekday_numbers):
            pe_bonus = class_day_has_pe.get((class_id, weekday), False)
            daily_limit = context.sanpin_validator.daily_lesson_limit(grade, pe_bonus=pe_bonus)
            if class_daily_counts[class_index, day_index] > daily_limit:
                diagnostics['class_daily_overload'] += int(class_daily_counts[class_index, day_index] - daily_limit)

        weekly_limit = context.class_weekly_limits.get(class_id, 0)
        if weekly_limit and class_weekly_counts[class_index] > weekly_limit:
            diagnostics['class_weekly_overload'] += int(class_weekly_counts[class_index] - weekly_limit)

    diagnostics['class_gap'] = _count_gaps(class_daily_numbers)
    diagnostics['teacher_gap'] = _count_gaps(teacher_daily_numbers)
    diagnostics['class_window_penalty'] = diagnostics['class_gap']
    diagnostics['teacher_window_penalty'] = diagnostics['teacher_gap']
    diagnostics['class_late_start'] = _count_late_starts(class_daily_numbers)
    diagnostics['teacher_late_start'] = _count_late_starts(teacher_daily_numbers)
    diagnostics['class_daily_imbalance'] = _count_daily_imbalance(daily_scores, class_weekly_counts)
    diagnostics['class_sparse_days'] = _count_sparse_days(class_daily_counts, class_weekly_counts)
    diagnostics['hard_subject_weekday_mismatch'] = _count_hard_subject_weekday_mismatch(
        class_daily_lessons=class_daily_lessons,
        class_grades=context.class_grades,
    )
    diagnostics['subject_alternation'] = _count_subject_alternation(
        class_daily_lessons=class_daily_lessons,
        class_grades=context.class_grades,
    )
    diagnostics['forbidden_double_lesson'] = _count_forbidden_double_lessons(
        class_daily_lessons=class_daily_lessons,
        class_grades=context.class_grades,
    )

    sanpin_result = context.sanpin_validator.validate_load_distribution(lesson_load_entries)
    for key, value in sanpin_result.diagnostics.items():
        diagnostics[key] = diagnostics.get(key, 0) + value

    hard_penalty = sum(diagnostics.get(name, 0) * weight for name, weight in HARD_WEIGHTS.items())
    soft_penalty = 0.0
    soft_penalty += diagnostics.get('sanpin_weekly_score_overload', 0) * weights.weekly_load_penalty
    soft_penalty += diagnostics.get('class_daily_imbalance', 0) * weights.daily_unevenness_penalty
    soft_penalty += diagnostics.get('teacher_gap', 0) * weights.teacher_gap_penalty
    soft_penalty += diagnostics.get('class_gap', 0) * weights.class_gap_penalty
    soft_penalty += diagnostics.get('teacher_preference_violations', 0) * weights.teacher_preference_penalty
    soft_penalty += diagnostics.get('hard_subject_position_violations', 0) * weights.hard_subject_position_penalty
    soft_penalty += (
        diagnostics.get('forbidden_double_lesson', 0) + diagnostics.get('subject_alternation', 0)
    ) * weights.doubled_subject_penalty
    soft_penalty += (
        diagnostics.get('sanpin_daily_score_overload', 0)
        + diagnostics.get('sanpin_weekly_score_overload', 0)
        + diagnostics.get('sanpin_peak_distribution_violation', 0)
        + diagnostics.get('sanpin_primary_light_day_violation', 0)
    ) * weights.sanpin_score_penalty
    soft_penalty += diagnostics.get('hard_subject_weekday_mismatch', 0) * weights.peak_day_penalty
    soft_penalty += diagnostics.get('class_window_penalty', 0) * weights.class_window_penalty
    soft_penalty += diagnostics.get('teacher_window_penalty', 0) * weights.teacher_window_penalty
    soft_penalty += diagnostics.get('class_sparse_days', 0) * (weights.daily_unevenness_penalty / 2.0)
    soft_penalty += diagnostics.get('class_late_start', 0) * (weights.class_gap_penalty / 2.0)
    soft_penalty += diagnostics.get('teacher_late_start', 0) * (weights.teacher_gap_penalty / 2.0)

    chromosome.hard_penalty = int(round(hard_penalty))
    chromosome.soft_penalty = int(round(soft_penalty))
    chromosome.score = 100000 - chromosome.hard_penalty - chromosome.soft_penalty
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


def _count_daily_imbalance(daily_scores: np.ndarray, weekly_counts: np.ndarray) -> int:
    penalty = 0
    for class_index in range(daily_scores.shape[0]):
        if weekly_counts[class_index] <= 0:
            continue
        expected = float(daily_scores[class_index].sum()) / max(1, daily_scores.shape[1])
        penalty += int(np.abs(daily_scores[class_index] - expected).sum() / 6.0)
    return penalty


def _count_sparse_days(class_daily_counts: np.ndarray, class_weekly_counts: np.ndarray) -> int:
    penalty = 0
    for class_index in range(class_daily_counts.shape[0]):
        weekly_total = int(class_weekly_counts[class_index])
        if weekly_total < 8:
            continue
        average = weekly_total / max(1, class_daily_counts.shape[1])
        lower_bound = max(1, int(average) - 1)
        for count in class_daily_counts[class_index]:
            if count < lower_bound:
                penalty += int(lower_bound - count)
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
    for hard_by_day in by_class.values():
        total_hard = sum(hard_by_day.values())
        if total_hard == 0:
            continue
        tue_wed = hard_by_day.get(2, 0) + hard_by_day.get(3, 0)
        mon_fri = hard_by_day.get(1, 0) + hard_by_day.get(5, 0)
        preferred_min = max(1, (total_hard + 1) // 2)
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

            if grade <= 4:
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


def _empty_preferences():
    class _Preferences:
        avoid_first_lesson = False
        avoid_last_lesson = False
        preferred_weekdays = ()
        avoid_weekdays = ()
        preferred_lesson_numbers = ()
        avoid_lesson_numbers = ()

    return _Preferences()
