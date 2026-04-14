import random
from dataclasses import dataclass

from .chromosome import Chromosome, Placement
from .crossover import crossover
from .data_loader import GenerationContext, LessonRequirement, load_generation_context
from .fitness import evaluate_chromosome
from .mutation import mutate
from .saver import persist_schedule


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
        context = load_generation_context(week_start=week_start, class_ids=class_ids)
        if not context.lesson_requirements:
            return GenerationResult(
                created_lessons=0,
                hard_penalty=0,
                soft_penalty=0,
                diagnostics={},
                warnings=context.warnings or ['There were no lessons to generate.'],
            )

        room_choices = self._build_room_choices(context)
        population = [self._create_initial_chromosome(context, room_choices) for _ in range(self.population_size)]
        best = max(population, key=lambda item: item.score)

        for _generation in range(self.generations):
            population.sort(key=lambda item: item.score, reverse=True)
            elite_count = max(2, self.population_size // 8)
            next_population = [item.copy() for item in population[:elite_count]]

            while len(next_population) < self.population_size:
                parent_a = self._select(population)
                parent_b = self._select(population)
                child = crossover(parent_a, parent_b, self.randomizer)
                child = mutate(child, context, self.mutation_rate, self.randomizer, room_choices)
                evaluate_chromosome(child, context)
                next_population.append(child)

            population = next_population
            population.sort(key=lambda item: item.score, reverse=True)
            if population[0].score > best.score:
                best = population[0].copy()
            if best.hard_penalty == 0 and best.soft_penalty < 50:
                break

        repaired = self._repair(best, context, room_choices)
        created_lessons, skipped_lessons = persist_schedule(repaired, context)
        warnings = list(context.warnings)
        if repaired.hard_penalty > 0:
            warnings.append('The generated schedule still contains unresolved hard penalties.')
        if skipped_lessons:
            warnings.append(
                f'Не удалось разместить {skipped_lessons} занятий из-за конфликтующих ограничений. '
                'Проверьте доступность преподавателей и ограничения по кабинетам.'
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
        used_class = set()
        used_teacher = set()
        used_room = set()
        subject_daily = {}
        class_daily_numbers: dict[tuple[int, int], list[int]] = {}
        teacher_daily_numbers: dict[tuple[int, int], list[int]] = {}

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
                used_class=used_class,
                used_teacher=used_teacher,
                used_room=used_room,
                subject_daily=subject_daily,
                class_daily_numbers=class_daily_numbers,
                teacher_daily_numbers=teacher_daily_numbers,
            )
            slot = next(slot for slot in context.time_slots if slot.id == slot_id)
            used_class.add((requirement.class_id, slot.weekday, slot_id))
            used_teacher.add((requirement.teacher_id, slot.weekday, slot_id))
            used_room.add((room_id, slot.weekday, slot_id))
            subject_daily[(requirement.class_id, requirement.subject_id, slot.weekday)] = (
                subject_daily.get((requirement.class_id, requirement.subject_id, slot.weekday), 0) + 1
            )
            class_daily_numbers.setdefault((requirement.class_id, slot.weekday), []).append(slot.lesson_number)
            teacher_daily_numbers.setdefault((requirement.teacher_id, slot.weekday), []).append(slot.lesson_number)
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
        used_class: set[tuple[int, int, int]],
        used_teacher: set[tuple[int, int, int]],
        used_room: set[tuple[int, int, int]],
        subject_daily: dict[tuple[int, int, int], int],
        class_daily_numbers: dict[tuple[int, int], list[int]],
        teacher_daily_numbers: dict[tuple[int, int], list[int]],
    ) -> tuple[int, int]:
        slot_ids = sorted(
            [slot.id for slot in context.time_slots],
            key=lambda item: (
                next(slot.lesson_number for slot in context.time_slots if slot.id == item),
                self.randomizer.random(),
            ),
        )
        candidate_rooms = self._candidate_rooms_for_requirement(requirement, context, room_choices)
        candidate_rooms = candidate_rooms[:]
        self.randomizer.shuffle(candidate_rooms)

        best_option = None
        best_score = None
        slot_lookup = {slot.id: slot for slot in context.time_slots}

        for slot_id in slot_ids:
            slot = slot_lookup[slot_id]
            weekday = slot.weekday
            score = 0
            if (requirement.teacher_id, slot_id) in context.teacher_unavailability:
                score += 50
            if (requirement.class_id, weekday, slot_id) in used_class:
                score += 80
            if (requirement.teacher_id, weekday, slot_id) in used_teacher:
                score += 80
            if subject_daily.get((requirement.class_id, requirement.subject_id, weekday), 0) >= requirement.daily_limit:
                score += 45

            score += self._start_and_gap_penalty(
                slot.lesson_number,
                class_daily_numbers.get((requirement.class_id, weekday), []),
                weight_late_start=18,
                weight_gap=36,
            )
            score += self._start_and_gap_penalty(
                slot.lesson_number,
                teacher_daily_numbers.get((requirement.teacher_id, weekday), []),
                weight_late_start=8,
                weight_gap=18,
            )

            for room_id in candidate_rooms:
                room_score = score
                if (room_id, weekday, slot_id) in used_room:
                    room_score += 80
                if best_score is None or room_score < best_score:
                    best_score = room_score
                    best_option = (slot_id, room_id)

        if best_option is None:
            return context.time_slots[0].id, next(iter(context.classrooms.keys()))
        return best_option

    def _select(self, population: list[Chromosome]) -> Chromosome:
        contenders = self.randomizer.sample(population, k=min(4, len(population)))
        contenders.sort(key=lambda item: item.score, reverse=True)
        return contenders[0]

    def _repair(
        self,
        chromosome: Chromosome,
        context: GenerationContext,
        room_choices: dict[str, list[int]],
    ) -> Chromosome:
        repaired = chromosome.copy()
        evaluate_chromosome(repaired, context)
        if repaired.hard_penalty == 0:
            return repaired

        slot_lookup = {slot.id: slot for slot in context.time_slots}
        slot_ids = [slot.id for slot in context.time_slots]

        for index, requirement in enumerate(context.lesson_requirements):
            current = repaired.placements[index]
            best = current
            best_score = repaired.score

            for slot_id in slot_ids:
                for room_id in self._candidate_rooms_for_requirement(requirement, context, room_choices):
                    repaired.placements[index] = Placement(slot_id, room_id)
                    evaluate_chromosome(repaired, context)
                    if repaired.score > best_score:
                        best_score = repaired.score
                        best = Placement(slot_id, room_id)
                        if repaired.hard_penalty == 0:
                            break
                if repaired.hard_penalty == 0:
                    break

            repaired.placements[index] = best
            evaluate_chromosome(repaired, context)
            if repaired.hard_penalty == 0:
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
