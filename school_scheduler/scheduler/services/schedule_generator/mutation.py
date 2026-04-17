import random
from collections import defaultdict

from .chromosome import Chromosome, Placement
from .data_loader import GenerationContext
from .school_rules import alternation_group, is_pe_subject


def mutate(
    chromosome: Chromosome,
    context: GenerationContext,
    mutation_rate: float,
    randomizer: random.Random,
    room_choices: dict[str, list[int]],
) -> Chromosome:
    mutated = chromosome.copy()
    if not mutated.placements:
        return mutated

    ordered_slots = sorted(context.time_slots, key=lambda slot: (slot.lesson_number, slot.weekday))
    slot_lookup = {slot.id: slot for slot in ordered_slots}
    slot_ids = [slot.id for slot in ordered_slots]

    class_slot_usage = defaultdict(int)
    teacher_slot_usage = defaultdict(int)
    room_slot_usage = defaultdict(int)
    subject_daily = defaultdict(int)
    class_daily_counts = defaultdict(int)
    teacher_daily_counts = defaultdict(int)
    class_daily_numbers = defaultdict(list)
    teacher_daily_numbers = defaultdict(list)
    class_daily_lessons = defaultdict(list)

    for fixed in context.fixed_lessons:
        slot = slot_lookup.get(fixed.time_slot_id)
        if slot is None:
            continue
        _add_usage(
            class_slot_usage=class_slot_usage,
            teacher_slot_usage=teacher_slot_usage,
            room_slot_usage=room_slot_usage,
            subject_daily=subject_daily,
            class_daily_counts=class_daily_counts,
            teacher_daily_counts=teacher_daily_counts,
            class_daily_numbers=class_daily_numbers,
            teacher_daily_numbers=teacher_daily_numbers,
            class_daily_lessons=class_daily_lessons,
            class_id=fixed.class_id,
            subject_id=fixed.subject_id,
            subject_name=fixed.subject_name,
            teacher_id=fixed.teacher_id,
            room_id=fixed.classroom_id,
            weekday=slot.weekday,
            time_slot_id=fixed.time_slot_id,
            lesson_number=slot.lesson_number,
            required_room_type=fixed.required_room_type,
        )

    for requirement, placement in zip(context.lesson_requirements, mutated.placements):
        slot = slot_lookup[placement.time_slot_id]
        _add_usage(
            class_slot_usage=class_slot_usage,
            teacher_slot_usage=teacher_slot_usage,
            room_slot_usage=room_slot_usage,
            subject_daily=subject_daily,
            class_daily_counts=class_daily_counts,
            teacher_daily_counts=teacher_daily_counts,
            class_daily_numbers=class_daily_numbers,
            teacher_daily_numbers=teacher_daily_numbers,
            class_daily_lessons=class_daily_lessons,
            class_id=requirement.class_id,
            subject_id=requirement.subject_id,
            subject_name=requirement.subject_name,
            teacher_id=requirement.teacher_id,
            room_id=placement.classroom_id,
            weekday=slot.weekday,
            time_slot_id=placement.time_slot_id,
            lesson_number=slot.lesson_number,
            required_room_type=requirement.required_room_type,
        )

    for index, requirement in enumerate(context.lesson_requirements):
        current = mutated.placements[index]
        current_slot = slot_lookup[current.time_slot_id]
        current_weekday = current_slot.weekday
        gene_conflict_score = _gene_conflict_score(
            requirement=requirement,
            placement=current,
            weekday=current_weekday,
            class_slot_usage=class_slot_usage,
            teacher_slot_usage=teacher_slot_usage,
            room_slot_usage=room_slot_usage,
            subject_daily=subject_daily,
            class_daily_counts=class_daily_counts,
            teacher_daily_counts=teacher_daily_counts,
            class_daily_lessons=class_daily_lessons,
            context=context,
        )
        per_gene_rate = _per_gene_mutation_rate(mutation_rate=mutation_rate, gene_conflict_score=gene_conflict_score)
        if randomizer.random() > per_gene_rate:
            continue

        _remove_usage(
            class_slot_usage=class_slot_usage,
            teacher_slot_usage=teacher_slot_usage,
            room_slot_usage=room_slot_usage,
            subject_daily=subject_daily,
            class_daily_counts=class_daily_counts,
            teacher_daily_counts=teacher_daily_counts,
            class_daily_numbers=class_daily_numbers,
            teacher_daily_numbers=teacher_daily_numbers,
            class_daily_lessons=class_daily_lessons,
            class_id=requirement.class_id,
            subject_id=requirement.subject_id,
            subject_name=requirement.subject_name,
            teacher_id=requirement.teacher_id,
            room_id=current.classroom_id,
            weekday=current_weekday,
            time_slot_id=current.time_slot_id,
            lesson_number=current_slot.lesson_number,
            required_room_type=requirement.required_room_type,
        )

        room_candidates = _candidate_rooms(requirement, context, room_choices)
        slot_candidates = _slot_candidates(
            slot_ids=slot_ids,
            slot_lookup=slot_lookup,
            current_slot_id=current.time_slot_id,
            randomizer=randomizer,
            conflict_score=gene_conflict_score,
        )
        best = current
        best_penalty = _local_position_penalty(
            requirement=requirement,
            placement=current,
            context=context,
            slot_lookup=slot_lookup,
            class_slot_usage=class_slot_usage,
            teacher_slot_usage=teacher_slot_usage,
            room_slot_usage=room_slot_usage,
            subject_daily=subject_daily,
            class_daily_counts=class_daily_counts,
            teacher_daily_counts=teacher_daily_counts,
            class_daily_numbers=class_daily_numbers,
            teacher_daily_numbers=teacher_daily_numbers,
            class_daily_lessons=class_daily_lessons,
        )

        for slot_id in slot_candidates:
            sampled_rooms = _sample_rooms(
                room_ids=room_candidates,
                current_room_id=current.classroom_id,
                randomizer=randomizer,
                limit=6 if gene_conflict_score > 0 else 4,
            )
            for room_id in sampled_rooms:
                candidate = Placement(time_slot_id=slot_id, classroom_id=room_id)
                penalty = _local_position_penalty(
                    requirement=requirement,
                    placement=candidate,
                    context=context,
                    slot_lookup=slot_lookup,
                    class_slot_usage=class_slot_usage,
                    teacher_slot_usage=teacher_slot_usage,
                    room_slot_usage=room_slot_usage,
                    subject_daily=subject_daily,
                    class_daily_counts=class_daily_counts,
                    teacher_daily_counts=teacher_daily_counts,
                    class_daily_numbers=class_daily_numbers,
                    teacher_daily_numbers=teacher_daily_numbers,
                    class_daily_lessons=class_daily_lessons,
                )
                if penalty < best_penalty or (penalty == best_penalty and randomizer.random() < 0.2):
                    best = candidate
                    best_penalty = penalty

        mutated.placements[index] = best
        best_slot = slot_lookup[best.time_slot_id]
        _add_usage(
            class_slot_usage=class_slot_usage,
            teacher_slot_usage=teacher_slot_usage,
            room_slot_usage=room_slot_usage,
            subject_daily=subject_daily,
            class_daily_counts=class_daily_counts,
            teacher_daily_counts=teacher_daily_counts,
            class_daily_numbers=class_daily_numbers,
            teacher_daily_numbers=teacher_daily_numbers,
            class_daily_lessons=class_daily_lessons,
            class_id=requirement.class_id,
            subject_id=requirement.subject_id,
            subject_name=requirement.subject_name,
            teacher_id=requirement.teacher_id,
            room_id=best.classroom_id,
            weekday=best_slot.weekday,
            time_slot_id=best.time_slot_id,
            lesson_number=best_slot.lesson_number,
            required_room_type=requirement.required_room_type,
        )

    return mutated


def _per_gene_mutation_rate(mutation_rate: float, gene_conflict_score: int) -> float:
    if gene_conflict_score <= 0:
        return max(0.03, min(0.55, mutation_rate * 0.65))
    boosted = mutation_rate + min(0.5, gene_conflict_score * 0.08)
    return max(0.08, min(0.98, boosted))


def _slot_candidates(
    slot_ids: list[int],
    slot_lookup: dict[int, object],
    current_slot_id: int,
    randomizer: random.Random,
    conflict_score: int,
) -> list[int]:
    if not slot_ids:
        return [current_slot_id]

    remaining = [slot_id for slot_id in slot_ids if slot_id != current_slot_id]
    remaining.sort(key=lambda slot_id: (slot_lookup[slot_id].lesson_number, randomizer.random()))
    if conflict_score > 0:
        return [current_slot_id] + remaining

    focused_limit = min(18, len(slot_ids))
    focused = [current_slot_id]
    for slot_id in remaining:
        focused.append(slot_id)
        if len(focused) >= focused_limit:
            break
    return focused


def _sample_rooms(
    room_ids: list[int],
    current_room_id: int,
    randomizer: random.Random,
    limit: int,
) -> list[int]:
    if not room_ids:
        return [current_room_id]

    unique = [current_room_id]
    shuffled = [room_id for room_id in room_ids if room_id != current_room_id]
    randomizer.shuffle(shuffled)
    for room_id in shuffled:
        unique.append(room_id)
        if len(unique) >= limit:
            break
    return unique


def _candidate_rooms(requirement, context: GenerationContext, room_choices: dict[str, list[int]]) -> list[int]:
    typed = [
        room_id
        for room_id in room_choices.get(requirement.required_room_type, [])
        if context.classrooms[room_id].capacity >= requirement.min_capacity
    ]
    if typed:
        return typed
    fallback = [
        room_id
        for room_id, room in context.classrooms.items()
        if room.capacity >= requirement.min_capacity
    ]
    return fallback or list(context.classrooms.keys())


def _gene_conflict_score(
    requirement,
    placement: Placement,
    weekday: int,
    class_slot_usage,
    teacher_slot_usage,
    room_slot_usage,
    subject_daily,
    class_daily_counts,
    teacher_daily_counts,
    class_daily_lessons,
    context: GenerationContext,
) -> int:
    score = 0
    class_slot_key = (requirement.class_id, weekday, placement.time_slot_id)
    teacher_slot_key = (requirement.teacher_id, weekday, placement.time_slot_id)
    room_slot_key = (placement.classroom_id, weekday, placement.time_slot_id)
    subject_key = (requirement.class_id, requirement.subject_id, weekday)
    class_day_key = (requirement.class_id, weekday)
    teacher_day_key = (requirement.teacher_id, weekday)

    if class_slot_usage[class_slot_key] > 1:
        score += 3
    if teacher_slot_usage[teacher_slot_key] > 1:
        score += 3
    if room_slot_usage[room_slot_key] > 1:
        score += 3
    if (requirement.teacher_id, placement.time_slot_id) in context.teacher_unavailability:
        score += 3
    if subject_daily[subject_key] > requirement.daily_limit:
        score += 2
    if teacher_daily_counts[teacher_day_key] > requirement.teacher_daily_limit:
        score += 2
    if class_daily_counts[class_day_key] > _allowed_daily_limit(
        requirement=requirement,
        day_lessons=class_daily_lessons.get(class_day_key, []),
    ):
        score += 2
    return score


def _local_position_penalty(
    requirement,
    placement: Placement,
    context: GenerationContext,
    slot_lookup: dict[int, object],
    class_slot_usage,
    teacher_slot_usage,
    room_slot_usage,
    subject_daily,
    class_daily_counts,
    teacher_daily_counts,
    class_daily_numbers,
    teacher_daily_numbers,
    class_daily_lessons,
) -> int:
    slot = slot_lookup[placement.time_slot_id]
    weekday = slot.weekday
    lesson_number = slot.lesson_number
    penalty = 0

    class_slot_key = (requirement.class_id, weekday, placement.time_slot_id)
    teacher_slot_key = (requirement.teacher_id, weekday, placement.time_slot_id)
    room_slot_key = (placement.classroom_id, weekday, placement.time_slot_id)
    class_day_key = (requirement.class_id, weekday)
    teacher_day_key = (requirement.teacher_id, weekday)
    subject_key = (requirement.class_id, requirement.subject_id, weekday)

    if (requirement.teacher_id, placement.time_slot_id) in context.teacher_unavailability:
        penalty += 240

    penalty += class_slot_usage[class_slot_key] * 320
    penalty += teacher_slot_usage[teacher_slot_key] * 320
    penalty += room_slot_usage[room_slot_key] * 250

    room = context.classrooms[placement.classroom_id]
    if room.room_type != requirement.required_room_type:
        penalty += 80
    if room.capacity < requirement.min_capacity:
        penalty += 220

    projected_subject = subject_daily[subject_key] + 1
    if projected_subject > requirement.daily_limit:
        penalty += (projected_subject - requirement.daily_limit) * 170

    projected_teacher_daily = teacher_daily_counts[teacher_day_key] + 1
    if projected_teacher_daily > requirement.teacher_daily_limit:
        penalty += (projected_teacher_daily - requirement.teacher_daily_limit) * 140

    day_lessons = class_daily_lessons.get(class_day_key, [])
    projected_day_lessons = day_lessons + [(lesson_number, requirement.subject_name, requirement.required_room_type)]
    projected_class_daily = class_daily_counts[class_day_key] + 1
    allowed_daily = _allowed_daily_limit(requirement=requirement, day_lessons=projected_day_lessons)
    if projected_class_daily > allowed_daily:
        penalty += (projected_class_daily - allowed_daily) * 180

    class_numbers = class_daily_numbers[class_day_key] + [lesson_number]
    class_gap, class_late = _gaps_and_late_start(class_numbers)
    penalty += class_gap * 70
    penalty += class_late * 45

    teacher_numbers = teacher_daily_numbers[teacher_day_key] + [lesson_number]
    teacher_gap, teacher_late = _gaps_and_late_start(teacher_numbers)
    penalty += teacher_gap * 28
    penalty += teacher_late * 12

    target_daily = context.class_daily_targets.get(requirement.class_id, 0)
    penalty += int(abs(projected_class_daily - target_daily) * 10)
    if projected_class_daily > int(target_daily) + 2:
        penalty += (projected_class_daily - (int(target_daily) + 2)) * 30

    if requirement.is_hard_subject:
        if weekday in {1, 5}:
            penalty += 18
        elif weekday in {2, 3}:
            penalty -= 4

    penalty += _alternation_penalty(
        class_grade=requirement.class_grade,
        subject_group=requirement.alternation_group,
        lesson_number=lesson_number,
        existing_lessons=day_lessons,
    )
    penalty += _double_lesson_penalty(
        allows_double_lesson=requirement.allows_double_lesson,
        subject_name=requirement.subject_name,
        lesson_number=lesson_number,
        existing_lessons=day_lessons,
    )
    return penalty


def _gaps_and_late_start(numbers: list[int]) -> tuple[int, int]:
    if not numbers:
        return (0, 0)
    ordered = sorted(set(numbers))
    gaps = max(0, (ordered[-1] - ordered[0] + 1) - len(ordered))
    late_start = max(0, ordered[0] - 1)
    return gaps, late_start


def _alternation_penalty(
    class_grade: int,
    subject_group: str,
    lesson_number: int,
    existing_lessons: list[tuple[int, str, str]],
) -> int:
    penalty = 0
    for existing_number, existing_subject, _room_type in existing_lessons:
        if abs(existing_number - lesson_number) != 1:
            continue
        existing_group = alternation_group(existing_subject, class_grade)
        if class_grade <= 4:
            if subject_group in {'hard', 'light'} and subject_group == existing_group:
                penalty += 16
        elif subject_group in {'stem', 'humanities'} and subject_group == existing_group:
            penalty += 12
    return penalty


def _double_lesson_penalty(
    allows_double_lesson: bool,
    subject_name: str,
    lesson_number: int,
    existing_lessons: list[tuple[int, str, str]],
) -> int:
    if allows_double_lesson:
        return 0
    for existing_number, existing_subject, _room_type in existing_lessons:
        if existing_subject == subject_name and abs(existing_number - lesson_number) == 1:
            return 200
    return 0


def _allowed_daily_limit(requirement, day_lessons: list[tuple[int, str, str]]) -> int:
    limit = requirement.class_daily_limit
    if requirement.class_grade <= 4 and any(is_pe_subject(subject_name) for _n, subject_name, _room in day_lessons):
        limit += 1
    return limit


def _add_usage(
    class_slot_usage,
    teacher_slot_usage,
    room_slot_usage,
    subject_daily,
    class_daily_counts,
    teacher_daily_counts,
    class_daily_numbers,
    teacher_daily_numbers,
    class_daily_lessons,
    class_id: int,
    subject_id: int,
    subject_name: str,
    teacher_id: int,
    room_id: int,
    weekday: int,
    time_slot_id: int,
    lesson_number: int,
    required_room_type: str,
) -> None:
    class_slot_usage[(class_id, weekday, time_slot_id)] += 1
    teacher_slot_usage[(teacher_id, weekday, time_slot_id)] += 1
    room_slot_usage[(room_id, weekday, time_slot_id)] += 1
    subject_daily[(class_id, subject_id, weekday)] += 1
    class_daily_counts[(class_id, weekday)] += 1
    teacher_daily_counts[(teacher_id, weekday)] += 1
    class_daily_numbers[(class_id, weekday)].append(lesson_number)
    teacher_daily_numbers[(teacher_id, weekday)].append(lesson_number)
    class_daily_lessons[(class_id, weekday)].append((lesson_number, subject_name, required_room_type))


def _remove_usage(
    class_slot_usage,
    teacher_slot_usage,
    room_slot_usage,
    subject_daily,
    class_daily_counts,
    teacher_daily_counts,
    class_daily_numbers,
    teacher_daily_numbers,
    class_daily_lessons,
    class_id: int,
    subject_id: int,
    subject_name: str,
    teacher_id: int,
    room_id: int,
    weekday: int,
    time_slot_id: int,
    lesson_number: int,
    required_room_type: str,
) -> None:
    _decrement_counter(class_slot_usage, (class_id, weekday, time_slot_id))
    _decrement_counter(teacher_slot_usage, (teacher_id, weekday, time_slot_id))
    _decrement_counter(room_slot_usage, (room_id, weekday, time_slot_id))
    _decrement_counter(subject_daily, (class_id, subject_id, weekday))
    _decrement_counter(class_daily_counts, (class_id, weekday))
    _decrement_counter(teacher_daily_counts, (teacher_id, weekday))
    _remove_list_item(class_daily_numbers[(class_id, weekday)], lesson_number)
    _remove_list_item(teacher_daily_numbers[(teacher_id, weekday)], lesson_number)
    _remove_list_item(class_daily_lessons[(class_id, weekday)], (lesson_number, subject_name, required_room_type))


def _decrement_counter(counter, key) -> None:
    counter[key] -= 1
    if counter[key] <= 0:
        counter.pop(key, None)


def _remove_list_item(items: list, value) -> None:
    try:
        items.remove(value)
    except ValueError:
        return
