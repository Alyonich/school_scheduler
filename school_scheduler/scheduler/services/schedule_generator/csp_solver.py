from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass

from constraint import BacktrackingSolver, Problem

from .data_loader import GenerationContext, LessonRequirement
from .sanpin_validator import is_pe_subject


@dataclass(frozen=True)
class CspSeedResult:
    candidate_domains: dict[str, list[tuple[int, int]]]
    seed_solutions: list[dict[str, tuple[int, int]]]
    warnings: list[str]


class CspSeedGenerator:
    def __init__(self, context: GenerationContext) -> None:
        self.context = context
        self.slot_lookup = {slot.id: slot for slot in context.time_slots}

    def build(self, limit: int | None = None) -> CspSeedResult:
        candidate_domains = self._build_candidate_domains()
        warnings: list[str] = []
        empty_domains = [
            requirement.lesson_id
            for requirement in self.context.lesson_requirements
            if not candidate_domains.get(requirement.lesson_id)
        ]
        if empty_domains:
            warnings.append(
                f'Для {len(empty_domains)} занятий CSP не смог построить ни одного допустимого домена.'
            )
            return CspSeedResult(candidate_domains=candidate_domains, seed_solutions=[], warnings=warnings)

        if len(self.context.lesson_requirements) > 72:
            warnings.append(
                'Полная CSP-инициализация пропущена из-за большого размера задачи; используются домены и эвристические seed-решения.'
            )
            return CspSeedResult(candidate_domains=candidate_domains, seed_solutions=[], warnings=warnings)

        skip_reason = self._skip_exact_seed_search_reason(candidate_domains)
        if skip_reason:
            warnings.append(skip_reason)
            return CspSeedResult(candidate_domains=candidate_domains, seed_solutions=[], warnings=warnings)

        solver = BacktrackingSolver(forwardcheck=True)
        problem = Problem(solver)
        search_domains = self._trim_domains_for_exact_search(candidate_domains)
        variable_order = [
            requirement.lesson_id
            for requirement in sorted(
                self.context.lesson_requirements,
                key=lambda item: len(search_domains[item.lesson_id]),
            )
        ]
        for variable in variable_order:
            problem.addVariable(variable, search_domains[variable])

        self._add_pairwise_constraints(problem, search_domains)
        self._add_group_constraints(problem)

        timeout_seconds = self.context.settings.algorithm.csp.timeout_seconds
        max_solutions = limit or self.context.settings.algorithm.csp.seed_solution_limit
        seeds: list[dict[str, tuple[int, int]]] = []
        start_time = time.perf_counter()
        iterator = problem.getSolutionIter()
        try:
            for solution in iterator:
                seeds.append(solution)
                if len(seeds) >= max_solutions:
                    break
                if time.perf_counter() - start_time >= timeout_seconds:
                    warnings.append('CSP seed generation stopped by timeout.')
                    break
        except RuntimeError:
            warnings.append('CSP solver stopped early due to backtracking exhaustion.')

        if not seeds:
            warnings.append('CSP не нашел полных стартовых решений в рамках заданного таймаута.')
        return CspSeedResult(candidate_domains=candidate_domains, seed_solutions=seeds, warnings=warnings)

    def _skip_exact_seed_search_reason(
        self,
        candidate_domains: dict[str, list[tuple[int, int]]],
    ) -> str | None:
        lesson_count = len(self.context.lesson_requirements)
        if lesson_count <= 12:
            return None

        domain_lengths = [
            len(candidate_domains.get(requirement.lesson_id, ()))
            for requirement in self.context.lesson_requirements
        ]
        if not domain_lengths:
            return None

        avg_domain = sum(domain_lengths) / max(1, len(domain_lengths))
        max_domain = max(domain_lengths)
        pairwise_edges = self._estimate_pairwise_edge_count(candidate_domains)
        max_edges = lesson_count * (lesson_count - 1) // 2
        density = (pairwise_edges / max_edges) if max_edges else 0.0
        search_pressure = lesson_count * avg_domain

        if lesson_count >= 18 and avg_domain >= 28 and density >= 0.85:
            return (
                'CSP seed generation skipped: exact search space is too dense for an interactive run; '
                'the generator will use constrained domains and heuristic seeds instead.'
            )
        if lesson_count >= 14 and search_pressure >= 800 and max_domain >= 60:
            return (
                'CSP seed generation skipped: too many candidate placements per lesson for the current '
                'problem size; the generator will continue with heuristic initialization.'
            )
        return None

    def _trim_domains_for_exact_search(
        self,
        candidate_domains: dict[str, list[tuple[int, int]]],
    ) -> dict[str, list[tuple[int, int]]]:
        lesson_count = len(self.context.lesson_requirements)
        if lesson_count <= 8:
            domain_limit = 36
        elif lesson_count <= 12:
            domain_limit = 24
        else:
            domain_limit = 16
        return {
            lesson_id: placements[:domain_limit]
            for lesson_id, placements in candidate_domains.items()
        }

    def _estimate_pairwise_edge_count(
        self,
        candidate_domains: dict[str, list[tuple[int, int]]],
    ) -> int:
        edge_count = 0
        requirements = self.context.lesson_requirements
        for left_index, left_requirement in enumerate(requirements):
            left_rooms = {room_id for _slot_id, room_id in candidate_domains[left_requirement.lesson_id]}
            for right_requirement in requirements[left_index + 1:]:
                if left_requirement.class_id == right_requirement.class_id:
                    edge_count += 1
                    continue
                if left_requirement.teacher_id == right_requirement.teacher_id:
                    edge_count += 1
                    continue
                right_rooms = {room_id for _slot_id, room_id in candidate_domains[right_requirement.lesson_id]}
                if left_rooms & right_rooms:
                    edge_count += 1
        return edge_count

    def _build_candidate_domains(self) -> dict[str, list[tuple[int, int]]]:
        domains: dict[str, list[tuple[int, int]]] = {}
        for requirement in self.context.lesson_requirements:
            placements: list[tuple[int, int]] = []
            for slot in self.context.time_slots:
                if slot.start_time < self.context.settings.school.start_time:
                    continue
                if (requirement.teacher_id, slot.id) in self.context.teacher_unavailability:
                    continue
                for room_id, room in self.context.classrooms.items():
                    if room.capacity < requirement.min_capacity:
                        continue
                    if room.room_type != requirement.required_room_type:
                        continue
                    placements.append((slot.id, room_id))

            if not placements:
                fallback = [
                    (slot.id, room_id)
                    for slot in self.context.time_slots
                    for room_id, room in self.context.classrooms.items()
                    if room.capacity >= requirement.min_capacity
                    and slot.start_time >= self.context.settings.school.start_time
                ]
                placements = fallback

            domains[requirement.lesson_id] = sorted(
                placements,
                key=lambda item: (
                    self.slot_lookup[item[0]].weekday,
                    self.slot_lookup[item[0]].lesson_number,
                    item[1],
                ),
            )
        return domains

    def _add_pairwise_constraints(
        self,
        problem: Problem,
        candidate_domains: dict[str, list[tuple[int, int]]],
    ) -> None:
        requirements = self.context.lesson_requirements
        for left_index, left_requirement in enumerate(requirements):
            left_rooms = {room_id for _slot_id, room_id in candidate_domains[left_requirement.lesson_id]}
            for right_requirement in requirements[left_index + 1:]:
                shares_class = left_requirement.class_id == right_requirement.class_id
                shares_teacher = left_requirement.teacher_id == right_requirement.teacher_id
                right_rooms = {room_id for _slot_id, room_id in candidate_domains[right_requirement.lesson_id]}
                shares_possible_room = bool(left_rooms & right_rooms)

                if not (shares_class or shares_teacher or shares_possible_room):
                    continue

                problem.addConstraint(
                    _make_pairwise_constraint(
                        shares_class=shares_class,
                        shares_teacher=shares_teacher,
                        shares_room=shares_possible_room,
                    ),
                    (left_requirement.lesson_id, right_requirement.lesson_id),
                )

    def _add_group_constraints(self, problem: Problem) -> None:
        fixed_subject_daily: dict[tuple[int, int, int], int] = defaultdict(int)
        fixed_class_daily: dict[tuple[int, int], int] = defaultdict(int)
        fixed_teacher_daily: dict[tuple[int, int], int] = defaultdict(int)
        fixed_class_daily_score: dict[tuple[int, int], int] = defaultdict(int)
        fixed_class_weekly_score: dict[int, int] = defaultdict(int)
        fixed_has_pe: dict[tuple[int, int], bool] = defaultdict(bool)

        for fixed in self.context.fixed_lessons:
            weekday = fixed.lesson_date.isoweekday()
            fixed_subject_daily[(fixed.class_id, fixed.subject_id, weekday)] += 1
            fixed_class_daily[(fixed.class_id, weekday)] += 1
            fixed_teacher_daily[(fixed.teacher_id, weekday)] += 1
            fixed_class_daily_score[(fixed.class_id, weekday)] += fixed.difficulty_score
            fixed_class_weekly_score[fixed.class_id] += fixed.difficulty_score
            if fixed.subject_name:
                fixed_has_pe[(fixed.class_id, weekday)] = fixed_has_pe[(fixed.class_id, weekday)] or is_pe_subject(fixed.subject_name)

        subject_groups: dict[tuple[int, int], list[LessonRequirement]] = defaultdict(list)
        class_groups: dict[int, list[LessonRequirement]] = defaultdict(list)
        teacher_groups: dict[int, list[LessonRequirement]] = defaultdict(list)
        for requirement in self.context.lesson_requirements:
            subject_groups[(requirement.class_id, requirement.subject_id)].append(requirement)
            class_groups[requirement.class_id].append(requirement)
            teacher_groups[requirement.teacher_id].append(requirement)

        for (_class_id, _subject_id), group in subject_groups.items():
            if len(group) <= 1:
                continue
            problem.addConstraint(
                _make_subject_daily_constraint(group, self.slot_lookup, fixed_subject_daily),
                [item.lesson_id for item in group],
            )

        for class_id, group in class_groups.items():
            if len(group) <= 1:
                continue
            grade = self.context.class_grades.get(class_id, 1)
            problem.addConstraint(
                _make_class_daily_constraint(
                    class_id=class_id,
                    grade=grade,
                    group=group,
                    slot_lookup=self.slot_lookup,
                    fixed_class_daily=fixed_class_daily,
                    fixed_has_pe=fixed_has_pe,
                    validator=self.context.sanpin_validator,
                ),
                [item.lesson_id for item in group],
            )
            if self.context.settings.sanpin.enable_score_caps:
                problem.addConstraint(
                    _make_class_score_constraint(
                        class_id=class_id,
                        grade=grade,
                        group=group,
                        slot_lookup=self.slot_lookup,
                        fixed_daily_score=fixed_class_daily_score,
                        fixed_weekly_score=fixed_class_weekly_score,
                        validator=self.context.sanpin_validator,
                    ),
                    [item.lesson_id for item in group],
                )

        for teacher_id, group in teacher_groups.items():
            if len(group) <= 1:
                continue
            problem.addConstraint(
                _make_teacher_daily_constraint(
                    teacher_id=teacher_id,
                    group=group,
                    slot_lookup=self.slot_lookup,
                    fixed_teacher_daily=fixed_teacher_daily,
                ),
                [item.lesson_id for item in group],
            )


def _make_pairwise_constraint(
    shares_class: bool,
    shares_teacher: bool,
    shares_room: bool,
):
    def _constraint(left: tuple[int, int], right: tuple[int, int]) -> bool:
        left_slot, left_room = left
        right_slot, right_room = right
        if shares_class and left_slot == right_slot:
            return False
        if shares_teacher and left_slot == right_slot:
            return False
        if shares_room and left_slot == right_slot and left_room == right_room:
            return False
        return True

    return _constraint


def _make_subject_daily_constraint(
    group: list[LessonRequirement],
    slot_lookup: dict[int, object],
    fixed_subject_daily: dict[tuple[int, int, int], int],
):
    class_id = group[0].class_id
    subject_id = group[0].subject_id
    daily_limit = group[0].daily_limit

    def _constraint(*placements: tuple[int, int]) -> bool:
        counts = defaultdict(int)
        for placement in placements:
            slot = slot_lookup[placement[0]]
            counts[slot.weekday] += 1
        for weekday, dynamic_count in counts.items():
            if dynamic_count + fixed_subject_daily.get((class_id, subject_id, weekday), 0) > daily_limit:
                return False
        return True

    return _constraint


def _make_class_daily_constraint(
    class_id: int,
    grade: int,
    group: list[LessonRequirement],
    slot_lookup: dict[int, object],
    fixed_class_daily: dict[tuple[int, int], int],
    fixed_has_pe: dict[tuple[int, int], bool],
    validator,
):
    def _constraint(*placements: tuple[int, int]) -> bool:
        counts = defaultdict(int)
        dynamic_has_pe = defaultdict(bool)
        for requirement, placement in zip(group, placements):
            slot = slot_lookup[placement[0]]
            counts[slot.weekday] += 1
            dynamic_has_pe[slot.weekday] = dynamic_has_pe[slot.weekday] or requirement.is_pe_lesson

        for weekday, dynamic_count in counts.items():
            pe_bonus = fixed_has_pe.get((class_id, weekday), False) or dynamic_has_pe.get(weekday, False)
            limit = validator.daily_lesson_limit(grade, pe_bonus=pe_bonus)
            if dynamic_count + fixed_class_daily.get((class_id, weekday), 0) > limit:
                return False
        return True

    return _constraint


def _make_teacher_daily_constraint(
    teacher_id: int,
    group: list[LessonRequirement],
    slot_lookup: dict[int, object],
    fixed_teacher_daily: dict[tuple[int, int], int],
):
    teacher_daily_limit = group[0].teacher_daily_limit

    def _constraint(*placements: tuple[int, int]) -> bool:
        counts = defaultdict(int)
        for placement in placements:
            slot = slot_lookup[placement[0]]
            counts[slot.weekday] += 1
        for weekday, dynamic_count in counts.items():
            if dynamic_count + fixed_teacher_daily.get((teacher_id, weekday), 0) > teacher_daily_limit:
                return False
        return True

    return _constraint


def _make_class_score_constraint(
    class_id: int,
    grade: int,
    group: list[LessonRequirement],
    slot_lookup: dict[int, object],
    fixed_daily_score: dict[tuple[int, int], int],
    fixed_weekly_score: dict[int, int],
    validator,
):
    daily_limit = validator.daily_score_limit(grade)
    weekly_limit = validator.weekly_score_limit(grade)

    def _constraint(*placements: tuple[int, int]) -> bool:
        daily_scores = defaultdict(int)
        weekly_score = fixed_weekly_score.get(class_id, 0)
        for requirement, placement in zip(group, placements):
            slot = slot_lookup[placement[0]]
            daily_scores[slot.weekday] += requirement.difficulty_score
            weekly_score += requirement.difficulty_score

        if weekly_score > weekly_limit:
            return False
        for weekday, dynamic_score in daily_scores.items():
            if dynamic_score + fixed_daily_score.get((class_id, weekday), 0) > daily_limit:
                return False
        return True

    return _constraint
