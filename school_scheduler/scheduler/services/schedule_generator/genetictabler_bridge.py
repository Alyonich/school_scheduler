from __future__ import annotations

import random
from collections import defaultdict

from genetictabler import GenerateTimeTable, TimetableConfig

from .data_loader import GenerationContext, LessonRequirement


class GeneticTablerBridge:
    def __init__(self, context: GenerationContext, randomizer: random.Random) -> None:
        self.context = context
        self.randomizer = randomizer
        self.sorted_slots = sorted(context.time_slots, key=lambda item: (item.weekday_index, item.lesson_number))
        self.slots_per_day = max((slot.lesson_number for slot in self.sorted_slots), default=1)
        self.slot_position_by_id = {
            slot.id: (slot.weekday_index * self.slots_per_day) + slot.lesson_number
            for slot in self.sorted_slots
        }
        self.slot_id_by_weekday_and_number = dict(context.slot_id_by_weekday_and_number)
        self.subject_ids = sorted(context.subject_index_map, key=context.subject_index_map.get)
        self.class_ids = sorted(context.class_index_map, key=context.class_index_map.get)
        self.subject_number_by_id = {
            subject_id: index + 1 for index, subject_id in enumerate(self.subject_ids)
        }
        self.class_number_by_id = {
            class_id: index + 1 for index, class_id in enumerate(self.class_ids)
        }

        repeat_limits, teacher_capacities = self._build_subject_caps()
        config = TimetableConfig(
            classes=max(1, len(self.class_ids)),
            courses=max(1, len(self.subject_ids)),
            slots=max(1, self.slots_per_day),
            days=max(1, len(self.context.weekday_numbers)),
            repeat=repeat_limits,
            teachers=teacher_capacities,
            population_size=self.context.settings.algorithm.ga.population_size,
            max_generations=self.context.settings.algorithm.ga.generations,
            mutation_rate=self.context.settings.algorithm.ga.mutation_rate,
            elite_ratio=max(
                0.01,
                min(
                    0.4,
                    self.context.settings.algorithm.ga.elitism_count
                    / max(1, self.context.settings.algorithm.ga.population_size),
                ),
            ),
            seed=randomizer.randint(1, 10_000_000),
        )
        self.toolkit = GenerateTimeTable.from_config(
            config,
            course_names=[str(subject_id) for subject_id in self.subject_ids],
            class_names=[str(class_id) for class_id in self.class_ids],
            day_names=[str(day) for day in self.context.weekday_numbers],
        )
        self.toolkit.initialize_genotype(
            no_courses=config.courses,
            classes=config.classes,
            slots=config.slots,
            days=config.days,
            daily_rep=config.repeat,
            teachers=config.teachers,
        )
        self.toolkit.tables = self.toolkit.generate_table_skeleton()

    def random_slot_id(self) -> int:
        return self.randomizer.choice(self.sorted_slots).id

    def random_slot_population(self, requirement: LessonRequirement, size: int) -> list[int]:
        slot_ids: list[int] = []
        for _ in range(size):
            slot_ids.append(self.decode_slot_id(self.toolkit.generate_gene(), requirement))
        return slot_ids

    def crossover_slot(
        self,
        requirement: LessonRequirement,
        slot_id_a: int,
        slot_id_b: int,
        use_uniform: bool = False,
    ) -> int:
        gene_a = self.encode_gene(requirement, slot_id_a)
        gene_b = self.encode_gene(requirement, slot_id_b)
        children = (
            self.toolkit.uniform_crossover(gene_a, gene_b)
            if use_uniform
            else self.toolkit.single_point_crossover(gene_a, gene_b)
        )
        return self.decode_slot_id(self.randomizer.choice(children), requirement)

    def mutate_slot(
        self,
        requirement: LessonRequirement,
        current_slot_id: int,
        smart: bool = True,
    ) -> int:
        gene = self.encode_gene(requirement, current_slot_id)
        if smart:
            mutated = self.toolkit.smart_mutation(
                gene,
                self.toolkit.course_bits,
                self.toolkit.slot_bits,
            )
        else:
            mutated = self.toolkit.mutation(
                gene,
                self.toolkit.course_bits,
                self.toolkit.slot_bits,
            )

        # Keep subject and class immutable for the real scheduling problem.
        course_bits = gene[: self.toolkit.course_bits]
        slot_bits = mutated[self.toolkit.course_bits : self.toolkit.course_bits + self.toolkit.slot_bits]
        class_bits = gene[self.toolkit.course_bits + self.toolkit.slot_bits :]
        rebuilt = f'{course_bits}{slot_bits}{class_bits}'
        return self.decode_slot_id(rebuilt, requirement)

    def encode_gene(self, requirement: LessonRequirement, slot_id: int) -> str:
        course_number = self.subject_number_by_id[requirement.subject_id]
        class_number = self.class_number_by_id[requirement.class_id]
        position = self.slot_position_by_id.get(slot_id, 1)
        return (
            self._to_binary(course_number, self.toolkit.course_bits)
            + self._to_binary(position, self.toolkit.slot_bits)
            + self._to_binary(class_number, self.toolkit.class_bits)
        )

    def decode_slot_id(self, gene: str, requirement: LessonRequirement | None = None) -> int:
        _course_no, lesson_number, day_no, _class_no = self.toolkit.decode_gene(gene)
        day_index = max(0, min(len(self.context.weekday_numbers) - 1, day_no - 1))
        weekday = self.context.weekday_numbers[day_index]
        slot_id = self.slot_id_by_weekday_and_number.get((weekday, lesson_number))
        if slot_id is not None:
            return slot_id

        fallback_slots = [
            slot.id
            for slot in self.sorted_slots
            if slot.weekday == weekday
        ]
        if fallback_slots:
            return self.randomizer.choice(fallback_slots)

        if requirement is not None:
            class_slots = [slot.id for slot in self.sorted_slots if slot.start_time >= self.context.settings.school.start_time]
            if class_slots:
                return self.randomizer.choice(class_slots)
        return self.sorted_slots[0].id

    def _build_subject_caps(self) -> tuple[list[int], list[int]]:
        repeat_limits: list[int] = []
        teacher_capacities: list[int] = []
        requirements_by_subject: dict[int, list[LessonRequirement]] = defaultdict(list)
        for requirement in self.context.lesson_requirements:
            requirements_by_subject[requirement.subject_id].append(requirement)

        for subject_id in self.subject_ids:
            subject_requirements = requirements_by_subject.get(subject_id, [])
            repeat_limits.append(
                max((requirement.daily_limit for requirement in subject_requirements), default=2)
            )
            teacher_capacities.append(
                max(1, len({requirement.teacher_id for requirement in subject_requirements}))
            )
        return repeat_limits, teacher_capacities

    @staticmethod
    def _to_binary(value: int, width: int) -> str:
        return format(max(0, value), f'0{width}b')
