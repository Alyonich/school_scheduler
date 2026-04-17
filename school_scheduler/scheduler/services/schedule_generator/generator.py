import random
from collections import defaultdict
from dataclasses import dataclass

from .chromosome import Chromosome, Placement
from .crossover import crossover
from .data_loader import GenerationContext, LessonRequirement, load_generation_context
from .fitness import evaluate_chromosome
from .mutation import mutate
from .saver import persist_schedule
from .school_rules import alternation_group


@dataclass(frozen=True)
class GenerationResult:
    created_lessons: int
    hard_penalty: int
    soft_penalty: int
    diagnostics: dict[str, int]
    warnings: list[str]


class GeneticScheduleGenerator:
    def __init__(
        self,
        population_size: int = 80,
        generations: int = 160,
        mutation_rate: float = 0.18,
        seed: int | None = None,
    ) -> None:
        self.population_size = population_size
        self.generations = generations
        self.mutation_rate = mutation_rate
        self.randomizer = random.Random(seed)

    def generate(self, week_start, class_ids: list[int] | None = None) -> GenerationResult:
        return self._generate_resilient(week_start=week_start, class_ids=class_ids)

    def _generate_resilient(self, week_start, class_ids: list[int] | None = None) -> GenerationResult:
        context = load_generation_context(week_start=week_start, class_ids=class_ids)
        if not context.lesson_requirements:
            return GenerationResult(
                created_lessons=0,
                hard_penalty=0,
                soft_penalty=0,
                diagnostics={},
                warnings=context.warnings or ['Для выбранных условий нет занятий, которые нужно сгенерировать.'],
            )

        room_choices = self._build_room_choices(context)
        population = [self._create_initial_chromosome(context, room_choices) for _ in range(self.population_size)]
        population.sort(key=self._chromosome_sort_key)
        best = population[0].copy()
        stagnation = 0
        min_generations = max(20, self.generations // 5)
        reheat_interval = max(10, self.generations // 10)
        soft_target = max(45, len(context.lesson_requirements) // 3)

        for generation_index in range(self.generations):
            population.sort(key=self._chromosome_sort_key)
            elite_count = max(2, self.population_size // 8)
            next_population = [item.copy() for item in population[:elite_count]]
            adaptive_mutation_rate = self._adaptive_mutation_rate(
                generation_index=generation_index,
                best=best,
                stagnation=stagnation,
            )

            while len(next_population) < self.population_size:
                parent_a = self._select(population)
                parent_b = self._select(population)
                child = crossover(parent_a, parent_b, self.randomizer)
                child = mutate(
                    chromosome=child,
                    context=context,
                    mutation_rate=adaptive_mutation_rate,
                    randomizer=self.randomizer,
                    room_choices=room_choices,
                )
                evaluate_chromosome(child, context)
                next_population.append(child)

            population = next_population
            population.sort(key=self._chromosome_sort_key)
            current_best = population[0]
            if self._is_better(current_best, best):
                best = current_best.copy()
                stagnation = 0
            else:
                stagnation += 1

            should_reheat = (
                stagnation >= reheat_interval
                and generation_index < self.generations - 1
                and (best.hard_penalty > 0 or best.soft_penalty > soft_target)
            )
            if should_reheat:
                population = self._inject_diversity(
                    population=population,
                    context=context,
                    room_choices=room_choices,
                    mutation_rate=min(0.95, adaptive_mutation_rate + 0.12),
                )
                population.sort(key=self._chromosome_sort_key)
                reheated_best = population[0]
                if self._is_better(reheated_best, best):
                    best = reheated_best.copy()
                    stagnation = 0
                else:
                    stagnation = max(0, stagnation // 2)

            should_stop = (
                generation_index >= min_generations
                and best.hard_penalty == 0
                and best.soft_penalty <= soft_target
                and stagnation >= max(4, reheat_interval // 2)
            )
            if should_stop:
                break

        repaired = self._repair(best, context, room_choices)
        created_lessons, skipped_lessons = persist_schedule(repaired, context)
        warnings = list(context.warnings)
        if repaired.hard_penalty > 0:
            warnings.append('В расписании остались жёсткие конфликты, часть ограничений не удалось полностью удовлетворить.')
        if skipped_lessons:
            warnings.append(
                f'Не удалось разместить {skipped_lessons} занятий из-за конфликтующих ограничений. '
                'Проверьте доступность преподавателей, кабинетов и недельную нагрузку классов.'
            )

        return GenerationResult(
            created_lessons=created_lessons,
            hard_penalty=repaired.hard_penalty,
            soft_penalty=repaired.soft_penalty,
            diagnostics=repaired.diagnostics,
            warnings=warnings,
        )

    def _create_initial_chromosome(
        self,
        context: GenerationContext,
        room_choices: dict[str, list[int]],
    ) -> Chromosome:
        placements: list[Placement] = []
        used_class: set[tuple[int, int, int]] = set()
        used_teacher: set[tuple[int, int, int]] = set()
        used_room: set[tuple[int, int, int]] = set()
        subject_daily: dict[tuple[int, int, int], int] = {}
        class_daily_numbers: dict[tuple[int, int], list[int]] = defaultdict(list)
        teacher_daily_numbers: dict[tuple[int, int], list[int]] = defaultdict(list)
        class_daily_counts: dict[tuple[int, int], int] = defaultdict(int)
        teacher_daily_counts: dict[tuple[int, int], int] = defaultdict(int)
        class_daily_lessons: dict[tuple[int, int], list[tuple[int, str, str]]] = defaultdict(list)
        slot_lookup = {slot.id: slot for slot in context.time_slots}
        slot_ids = [slot.id for slot in sorted(context.time_slots, key=lambda item: (item.lesson_number, item.weekday))]

        for fixed in context.fixed_lessons:
            weekday = fixed.lesson_date.isoweekday()
            fixed_slot = slot_lookup.get(fixed.time_slot_id)
            if fixed_slot is None:
                continue

            used_class.add((fixed.class_id, weekday, fixed.time_slot_id))
            used_teacher.add((fixed.teacher_id, weekday, fixed.time_slot_id))
            used_room.add((fixed.classroom_id, weekday, fixed.time_slot_id))
            subject_daily[(fixed.class_id, fixed.subject_id, weekday)] = (
                subject_daily.get((fixed.class_id, fixed.subject_id, weekday), 0) + 1
            )
            class_daily_numbers[(fixed.class_id, weekday)].append(fixed_slot.lesson_number)
            teacher_daily_numbers[(fixed.teacher_id, weekday)].append(fixed_slot.lesson_number)
            class_daily_counts[(fixed.class_id, weekday)] += 1
            teacher_daily_counts[(fixed.teacher_id, weekday)] += 1
            class_daily_lessons[(fixed.class_id, weekday)].append(
                (fixed_slot.lesson_number, fixed.subject_name, fixed.required_room_type)
            )

        requirements = sorted(
            context.lesson_requirements,
            key=lambda item: (
                len(self._candidate_rooms_for_requirement(item, context, room_choices)) or 999,
                item.daily_limit,
            ),
        )

        original_positions = {requirement.lesson_id: index for index, requirement in enumerate(context.lesson_requirements)}
        placement_map = {}

        for requirement in requirements:
            slot_id, room_id = self._pick_best_position(
                requirement=requirement,
                context=context,
                room_choices=room_choices,
                slot_ids=slot_ids,
                slot_lookup=slot_lookup,
                used_class=used_class,
                used_teacher=used_teacher,
                used_room=used_room,
                subject_daily=subject_daily,
                class_daily_numbers=class_daily_numbers,
                teacher_daily_numbers=teacher_daily_numbers,
                class_daily_counts=class_daily_counts,
                teacher_daily_counts=teacher_daily_counts,
                class_daily_lessons=class_daily_lessons,
            )
            slot = slot_lookup[slot_id]
            used_class.add((requirement.class_id, slot.weekday, slot_id))
            used_teacher.add((requirement.teacher_id, slot.weekday, slot_id))
            used_room.add((room_id, slot.weekday, slot_id))
            subject_daily[(requirement.class_id, requirement.subject_id, slot.weekday)] = (
                subject_daily.get((requirement.class_id, requirement.subject_id, slot.weekday), 0) + 1
            )
            class_daily_numbers[(requirement.class_id, slot.weekday)].append(slot.lesson_number)
            teacher_daily_numbers[(requirement.teacher_id, slot.weekday)].append(slot.lesson_number)
            class_daily_counts[(requirement.class_id, slot.weekday)] += 1
            teacher_daily_counts[(requirement.teacher_id, slot.weekday)] += 1
            class_daily_lessons[(requirement.class_id, slot.weekday)].append(
                (slot.lesson_number, requirement.subject_name, requirement.required_room_type)
            )
            placement_map[original_positions[requirement.lesson_id]] = Placement(slot_id, room_id)

        for index in range(len(context.lesson_requirements)):
            placements.append(placement_map[index])

        chromosome = Chromosome(placements=placements)
        return evaluate_chromosome(chromosome, context)

    def _pick_best_position(
        self,
        requirement: LessonRequirement,
        context: GenerationContext,
        room_choices: dict[str, list[int]],
        slot_ids: list[int],
        slot_lookup: dict[int, object],
        used_class: set[tuple[int, int, int]],
        used_teacher: set[tuple[int, int, int]],
        used_room: set[tuple[int, int, int]],
        subject_daily: dict[tuple[int, int, int], int],
        class_daily_numbers: dict[tuple[int, int], list[int]],
        teacher_daily_numbers: dict[tuple[int, int], list[int]],
        class_daily_counts: dict[tuple[int, int], int],
        teacher_daily_counts: dict[tuple[int, int], int],
        class_daily_lessons: dict[tuple[int, int], list[tuple[int, str, str]]],
    ) -> tuple[int, int]:
        candidate_rooms = self._candidate_rooms_for_requirement(requirement, context, room_choices)
        candidate_rooms = candidate_rooms[:]
        self.randomizer.shuffle(candidate_rooms)

        best_option = None
        best_score = None

        for slot_id in slot_ids:
            slot = slot_lookup[slot_id]
            weekday = slot.weekday
            score = self.randomizer.random()
            if (requirement.teacher_id, slot_id) in context.teacher_unavailability:
                score += 70
            if (requirement.class_id, weekday, slot_id) in used_class:
                score += 130
            if (requirement.teacher_id, weekday, slot_id) in used_teacher:
                score += 130
            if subject_daily.get((requirement.class_id, requirement.subject_id, weekday), 0) >= requirement.daily_limit:
                score += 80

            projected_teacher_daily = teacher_daily_counts.get((requirement.teacher_id, weekday), 0) + 1
            if projected_teacher_daily > requirement.teacher_daily_limit:
                score += (projected_teacher_daily - requirement.teacher_daily_limit) * 55

            projected_class_daily = class_daily_counts.get((requirement.class_id, weekday), 0) + 1
            class_daily_limit = requirement.class_daily_limit
            if requirement.class_grade <= 4 and requirement.is_pe_lesson:
                class_daily_limit += 1
            if projected_class_daily > class_daily_limit:
                score += (projected_class_daily - class_daily_limit) * 150

            target_daily = context.class_daily_targets.get(requirement.class_id, 0)
            score += int(abs(projected_class_daily - target_daily) * 8)
            if projected_class_daily > int(target_daily) + 1:
                score += (projected_class_daily - (int(target_daily) + 1)) * 30

            score += self._start_and_gap_penalty(
                slot.lesson_number,
                class_daily_numbers.get((requirement.class_id, weekday), []),
                weight_late_start=36,
                weight_gap=58,
            )
            score += self._start_and_gap_penalty(
                slot.lesson_number,
                teacher_daily_numbers.get((requirement.teacher_id, weekday), []),
                weight_late_start=10,
                weight_gap=22,
            )
            score += self._alternation_penalty(
                lesson_number=slot.lesson_number,
                subject_group=requirement.alternation_group,
                class_grade=requirement.class_grade,
                existing_lessons=class_daily_lessons.get((requirement.class_id, weekday), []),
            )
            score += self._double_lesson_penalty(
                lesson_number=slot.lesson_number,
                subject_name=requirement.subject_name,
                allows_double_lesson=requirement.allows_double_lesson,
                existing_lessons=class_daily_lessons.get((requirement.class_id, weekday), []),
            )
            score += self._difficulty_weekday_penalty(
                weekday=weekday,
                is_hard_subject=requirement.is_hard_subject,
            )

            for room_id in candidate_rooms:
                room_score = score
                if (room_id, weekday, slot_id) in used_room:
                    room_score += 130
                room = context.classrooms[room_id]
                if room.room_type != requirement.required_room_type:
                    room_score += 35
                if best_score is None or room_score < best_score:
                    best_score = room_score
                    best_option = (slot_id, room_id)

        if best_option is None:
            return context.time_slots[0].id, next(iter(context.classrooms.keys()))
        return best_option

    def _select(self, population: list[Chromosome]) -> Chromosome:
        contenders = self.randomizer.sample(population, k=min(4, len(population)))
        contenders.sort(key=self._chromosome_sort_key)
        return contenders[0]

    def _chromosome_sort_key(self, chromosome: Chromosome) -> tuple[int, int, int]:
        return (
            chromosome.hard_penalty,
            chromosome.soft_penalty,
            -chromosome.score,
        )

    def _is_better(self, candidate: Chromosome, baseline: Chromosome) -> bool:
        return self._chromosome_sort_key(candidate) < self._chromosome_sort_key(baseline)

    def _adaptive_mutation_rate(
        self,
        generation_index: int,
        best: Chromosome,
        stagnation: int,
    ) -> float:
        # Conflict-heavy or stagnating populations need stronger mutation pressure.
        base_rate = self.mutation_rate
        if best.hard_penalty > 0:
            base_rate += 0.08
        if stagnation > 0:
            base_rate += min(0.18, stagnation * 0.01)
        if generation_index > (self.generations * 0.7):
            base_rate += 0.03
        return max(0.05, min(0.95, base_rate))

    def _inject_diversity(
        self,
        population: list[Chromosome],
        context: GenerationContext,
        room_choices: dict[str, list[int]],
        mutation_rate: float,
    ) -> list[Chromosome]:
        population.sort(key=self._chromosome_sort_key)
        keep_count = max(2, self.population_size // 10)
        immigrant_count = max(3, self.population_size // 6)
        survivors = [item.copy() for item in population[:keep_count]]

        immigrants = [
            self._create_initial_chromosome(context, room_choices)
            for _ in range(immigrant_count)
        ]

        next_population = survivors + immigrants
        source_pool = population[: max(keep_count * 2, self.population_size // 3)]
        while len(next_population) < self.population_size:
            parent = self.randomizer.choice(source_pool).copy()
            mutated = mutate(
                chromosome=parent,
                context=context,
                mutation_rate=mutation_rate,
                randomizer=self.randomizer,
                room_choices=room_choices,
            )
            evaluate_chromosome(mutated, context)
            next_population.append(mutated)
        return next_population

    def _repair(
        self,
        chromosome: Chromosome,
        context: GenerationContext,
        room_choices: dict[str, list[int]],
    ) -> Chromosome:
        repaired = chromosome.copy()
        evaluate_chromosome(repaired, context)
        slot_ids = [slot.id for slot in sorted(context.time_slots, key=lambda item: (item.lesson_number, item.weekday))]
        soft_target = max(40, len(context.lesson_requirements) // 4)

        max_rounds = 3 if repaired.hard_penalty > 0 else 2
        for round_index in range(max_rounds):
            improved_in_round = False
            indices = list(range(len(context.lesson_requirements)))
            self.randomizer.shuffle(indices)

            for index in indices:
                requirement = context.lesson_requirements[index]
                current = repaired.placements[index]
                best_placement = current
                best_snapshot = repaired.copy()
                best_key = self._chromosome_sort_key(repaired)

                candidate_slots = self._repair_slot_candidates(slot_ids, current.time_slot_id, repaired.hard_penalty > 0)
                candidate_rooms = self._repair_room_candidates(
                    self._candidate_rooms_for_requirement(requirement, context, room_choices),
                    current.classroom_id,
                )
                for slot_id in candidate_slots:
                    for room_id in candidate_rooms:
                        candidate = Placement(slot_id, room_id)
                        if candidate == current:
                            continue
                        repaired.placements[index] = candidate
                        evaluate_chromosome(repaired, context)
                        candidate_key = self._chromosome_sort_key(repaired)
                        if candidate_key < best_key:
                            best_key = candidate_key
                            best_placement = candidate
                            best_snapshot = repaired.copy()

                repaired.placements[index] = best_placement
                repaired.hard_penalty = best_snapshot.hard_penalty
                repaired.soft_penalty = best_snapshot.soft_penalty
                repaired.score = best_snapshot.score
                repaired.diagnostics = dict(best_snapshot.diagnostics)

                if best_placement != current:
                    improved_in_round = True

            evaluate_chromosome(repaired, context)
            if repaired.hard_penalty == 0 and repaired.soft_penalty <= soft_target:
                break
            if not improved_in_round and round_index > 0:
                break

        return evaluate_chromosome(repaired, context)

    def _build_room_choices(self, context: GenerationContext) -> dict[str, list[int]]:
        room_choices: dict[str, list[int]] = {}
        for room_id, room in context.classrooms.items():
            room_choices.setdefault(room.room_type, []).append(room_id)
        return room_choices

    def _candidate_rooms_for_requirement(
        self,
        requirement: LessonRequirement,
        context: GenerationContext,
        room_choices: dict[str, list[int]],
    ) -> list[int]:
        typed_rooms = room_choices.get(requirement.required_room_type, [])
        suitable = [
            room_id
            for room_id in typed_rooms
            if context.classrooms[room_id].capacity >= requirement.min_capacity
        ]
        if suitable:
            return suitable
        fallback = [
            room_id
            for room_id, room in context.classrooms.items()
            if room.capacity >= requirement.min_capacity
        ]
        return fallback or list(context.classrooms.keys())

    def _repair_slot_candidates(
        self,
        slot_ids: list[int],
        current_slot_id: int,
        hard_conflicts_present: bool,
    ) -> list[int]:
        if hard_conflicts_present:
            return slot_ids

        prioritized = [current_slot_id]
        for slot_id in slot_ids:
            if slot_id == current_slot_id:
                continue
            prioritized.append(slot_id)
            if len(prioritized) >= min(14, len(slot_ids)):
                break
        return prioritized

    def _repair_room_candidates(
        self,
        room_ids: list[int],
        current_room_id: int,
    ) -> list[int]:
        if not room_ids:
            return [current_room_id]

        unique = [current_room_id]
        for room_id in room_ids:
            if room_id in unique:
                continue
            unique.append(room_id)
            if len(unique) >= 6:
                break
        return unique

    def _start_and_gap_penalty(
        self,
        lesson_number: int,
        existing_numbers: list[int],
        weight_late_start: int,
        weight_gap: int,
    ) -> int:
        if not existing_numbers:
            return max(0, lesson_number - 1) * weight_late_start

        updated = sorted(set(existing_numbers + [lesson_number]))
        gaps = max(0, (updated[-1] - updated[0] + 1) - len(updated))
        late_start = max(0, updated[0] - 1)
        return gaps * weight_gap + late_start * weight_late_start

    def _alternation_penalty(
        self,
        lesson_number: int,
        subject_group: str,
        class_grade: int,
        existing_lessons: list[tuple[int, str, str]],
    ) -> int:
        if not existing_lessons:
            return 0

        penalty = 0
        for existing_number, existing_subject, _room_type in existing_lessons:
            if abs(existing_number - lesson_number) != 1:
                continue
            existing_group = alternation_group(existing_subject, class_grade)
            if class_grade <= 4:
                if subject_group in {'hard', 'light'} and subject_group == existing_group:
                    penalty += 12
            elif subject_group in {'stem', 'humanities'} and subject_group == existing_group:
                penalty += 10
        return penalty

    def _double_lesson_penalty(
        self,
        lesson_number: int,
        subject_name: str,
        allows_double_lesson: bool,
        existing_lessons: list[tuple[int, str, str]],
    ) -> int:
        if allows_double_lesson:
            return 0

        for existing_number, existing_subject, _room_type in existing_lessons:
            if existing_subject == subject_name and abs(existing_number - lesson_number) == 1:
                return 85
        return 0

    def _difficulty_weekday_penalty(self, weekday: int, is_hard_subject: bool) -> int:
        if not is_hard_subject:
            return 0
        if weekday in {1, 5}:
            return 16
        if weekday in {2, 3}:
            return -4
        return 0
