from __future__ import annotations

import random
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Callable, Iterable

from .chromosome import Chromosome, Placement
from .configuration import SchedulerSettings, load_scheduler_settings
from .csp_solver import CspSeedGenerator
from .data_loader import GenerationContext, LessonRequirement, load_generation_context
from .fitness import evaluate_chromosome
from .genetictabler_bridge import GeneticTablerBridge
from .sanpin_validator import is_pe_subject

# persist_schedule импортируется lazy внутри generate(), чтобы schedule_generator
# можно было импортировать без django.setup() — полезно для unit-тестов и для
# изоляции от Django ORM при автономной диагностике алгоритма.


@dataclass(frozen=True)
class GenerationResult:
    created_lessons: int
    hard_penalty: int
    soft_penalty: int
    diagnostics: dict[str, int]
    warnings: list[str]


ProgressCallback = Callable[[str, str, str, int], None]


@dataclass(frozen=True)
class SearchProfile:
    population_size: int
    generations: int
    local_search_iterations: int
    local_search_interval: int
    local_search_top_count: int
    hill_domain_scan_limit: int
    hill_candidate_limit: int
    max_runtime_seconds: float
    stagnation_limit: int
    constructive_attempts: int


class GeneticScheduleGenerator:
    def __init__(
        self,
        population_size: int | None = None,
        generations: int | None = None,
        mutation_rate: float | None = None,
        crossover_rate: float | None = None,
        elitism_count: int | None = None,
        local_search_iterations: int | None = None,
        config_path: str | None = None,
        seed: int | None = None,
        ga_time_limit_seconds: float | None = None,
    ) -> None:
        base_settings = load_scheduler_settings(config_path)
        ga_settings = base_settings.algorithm.ga
        overridden_ga = replace(
            ga_settings,
            population_size=population_size if population_size is not None else ga_settings.population_size,
            generations=generations if generations is not None else ga_settings.generations,
            mutation_rate=mutation_rate if mutation_rate is not None else ga_settings.mutation_rate,
            crossover_rate=crossover_rate if crossover_rate is not None else ga_settings.crossover_rate,
            elitism_count=elitism_count if elitism_count is not None else ga_settings.elitism_count,
            local_search_iterations=(
                local_search_iterations
                if local_search_iterations is not None
                else ga_settings.local_search_iterations
            ),
        )
        self.settings = replace(
            base_settings,
            algorithm=replace(base_settings.algorithm, ga=overridden_ga),
        )
        self.randomizer = random.Random(seed)
        # None → используем профиль (autosize). Положительное число → жёсткий
        # пользовательский лимит на цикл GA (CSP-сид и финальный repair не считаются).
        self.ga_time_limit_seconds: float | None = (
            float(ga_time_limit_seconds) if ga_time_limit_seconds is not None else None
        )

    def generate(
        self,
        week_start,
        class_ids: list[int] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> GenerationResult:
        self._emit_progress(
            progress_callback,
            stage='preparing',
            stage_label='Подготовка данных',
            message='Собираем учебную нагрузку, кабинеты и доступность преподавателей.',
            progress_percent=3,
        )
        context = load_generation_context(
            week_start=week_start,
            class_ids=class_ids,
            settings=self.settings,
        )
        self._emit_progress(
            progress_callback,
            stage='preparing',
            stage_label='Подготовка данных',
            message=(
                f'Подготовлено {len(context.lesson_requirements)} уроков '
                f'для {len(context.class_ids)} классов. Переходим к поиску допустимых вариантов.'
            ),
            progress_percent=10,
        )
        if not context.lesson_requirements:
            self._emit_progress(
                progress_callback,
                stage='completed',
                stage_label='Готово',
                message='Для выбранной недели нет уроков, которые нужно пересчитывать.',
                progress_percent=100,
            )
            return GenerationResult(
                created_lessons=0,
                hard_penalty=0,
                soft_penalty=0,
                diagnostics={},
                warnings=context.warnings or ['Для выбранных условий нет занятий, которые нужно сгенерировать.'],
            )

        best, warnings = self._optimize(context, progress_callback=progress_callback)
        self._emit_progress(
            progress_callback,
            stage='postprocessing',
            stage_label='Финальная проверка',
            message='Проверяем результат по СанПиН и при необходимости доулучшаем расписание.',
            progress_percent=90,
        )
        best, post_warnings = self._postprocess(context, best, progress_callback=progress_callback)
        warnings.extend(post_warnings)

        self._emit_progress(
            progress_callback,
            stage='saving',
            stage_label='Сохранение результата',
            message='Сохраняем итоговое расписание в базу данных.',
            progress_percent=97,
        )
        from .saver import persist_schedule

        created_lessons, skipped_lessons = persist_schedule(best, context)
        if best.hard_penalty > 0:
            warnings.append(
                'В расписании остались жесткие конфликты; проверьте доступность преподавателей, кабинетов и недельную нагрузку.'
            )
        if skipped_lessons:
            warnings.append(
                f'Не удалось разместить {skipped_lessons} занятий из-за конфликтующих ограничений.'
            )
        result_message = (
            f'Готово: создано {created_lessons} занятий. '
            f'Жесткий штраф {best.hard_penalty}, мягкий штраф {best.soft_penalty}.'
        )
        if skipped_lessons:
            result_message += f' Не удалось разместить {skipped_lessons} занятий.'
        self._emit_progress(
            progress_callback,
            stage='completed',
            stage_label='Готово',
            message=result_message,
            progress_percent=100,
        )

        return GenerationResult(
            created_lessons=created_lessons,
            hard_penalty=best.hard_penalty,
            soft_penalty=best.soft_penalty,
            diagnostics=best.diagnostics,
            warnings=warnings,
        )

    def _optimize(
        self,
        context: GenerationContext,
        seed_population: list[Chromosome] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> tuple[Chromosome, list[str]]:
        profile = self._build_search_profile(context)
        self._emit_progress(
            progress_callback,
            stage='csp',
            stage_label='Проверка ограничений',
            message='Строим допустимые домены и стартовые решения без конфликтов.',
            progress_percent=14,
        )
        csp_result = CspSeedGenerator(context).build(
            limit=max(2, int(profile.population_size * context.settings.algorithm.ga.csp_seed_fraction))
        )
        warnings = list(context.warnings) + list(csp_result.warnings)
        candidate_domains = csp_result.candidate_domains
        bridge = GeneticTablerBridge(context, self.randomizer)

        self._emit_progress(
            progress_callback,
            stage='population',
            stage_label='Стартовая популяция',
            message='Собираем начальные варианты расписания для генетического алгоритма.',
            progress_percent=24,
        )
        population = self._build_initial_population(
            context=context,
            candidate_domains=candidate_domains,
            bridge=bridge,
            csp_seed_solutions=csp_result.seed_solutions,
            seed_population=seed_population or [],
            population_size=profile.population_size,
            constructive_attempts=profile.constructive_attempts,
        )
        population.sort(key=self._chromosome_sort_key)
        best = population[0].copy()

        ga_settings = context.settings.algorithm.ga
        # Если пользователь задал жёсткий лимит времени — он полностью заменяет
        # автоматический бюджет из профиля. Иначе берём интерактивный профиль.
        time_budget_seconds = (
            float(self.ga_time_limit_seconds)
            if self.ga_time_limit_seconds is not None and self.ga_time_limit_seconds > 0
            else profile.max_runtime_seconds
        )
        budget_is_user_override = self.ga_time_limit_seconds is not None and self.ga_time_limit_seconds > 0

        # Ранний выход на «идеальной» хромосоме: hard_penalty=0 (нет пересечений,
        # перегрузок, недоступности учителя) И class_gap=0 (нет окон между уроками).
        # Этого пользователь явно требует — если такая хромосома уже в стартовой
        # популяции, GA не запускается.
        if self._is_ideal(best):
            warnings.append('GA остановлен сразу: стартовое решение уже без жёстких конфликтов и окон.')
            ga_loop_was_run = False
        else:
            ga_loop_was_run = True

        started_at = time.perf_counter()
        stagnation = 0
        for generation_index in range(profile.generations):
            if not ga_loop_was_run:
                break
            elapsed = time.perf_counter() - started_at
            if elapsed >= time_budget_seconds:
                if budget_is_user_override:
                    warnings.append(
                        f'GA остановлен по лимиту времени ({time_budget_seconds:.1f} сек). '
                        f'Берём лучший найденный вариант (hard={best.hard_penalty}, soft={best.soft_penalty}).'
                    )
                else:
                    warnings.append('GA search stopped after reaching the interactive runtime budget.')
                break
            generation_number = generation_index + 1
            self._emit_progress(
                progress_callback,
                stage='evolution',
                stage_label='Генетический поиск',
                message=(
                    f'Поколение {generation_number} из {profile.generations}: '
                    'скрещиваем варианты и вносим мутации.'
                ),
                progress_percent=self._generation_progress_percent(generation_number, profile.generations, 32, 76),
            )
            population.sort(key=self._chromosome_sort_key)
            elite_count = min(max(2, ga_settings.elitism_count), len(population))
            next_population = [item.copy() for item in population[:elite_count]]

            while len(next_population) < profile.population_size:
                parent_a = self._select(population)
                parent_b = self._select(population)
                child = self._crossover(
                    parent_a=parent_a,
                    parent_b=parent_b,
                    context=context,
                    candidate_domains=candidate_domains,
                    bridge=bridge,
                )
                child = self._mutate(
                    chromosome=child,
                    context=context,
                    candidate_domains=candidate_domains,
                    bridge=bridge,
                    mutation_rate=ga_settings.mutation_rate,
                    smart=generation_index >= profile.generations // 2,
                )
                evaluate_chromosome(child, context)
                next_population.append(child)

            population = next_population
            population.sort(key=self._chromosome_sort_key)
            improved = self._is_better(population[0], best)
            if improved:
                best = population[0].copy()
                stagnation = 0
            else:
                stagnation += 1

            self._emit_progress(
                progress_callback,
                stage='local_search',
                stage_label='Локальное улучшение',
                message=(
                    f'Поколение {generation_number} из {ga_settings.generations}: '
                    'дошлифовываем лучшие варианты, убираем окна и перегрузки.'
                ),
                progress_percent=self._generation_progress_percent(generation_number, ga_settings.generations, 38, 84),
            )
            population = self._apply_local_search(
                population=population,
                context=context,
                candidate_domains=candidate_domains,
                bridge=bridge,
            )
            population.sort(key=self._chromosome_sort_key)
            if self._is_better(population[0], best):
                best = population[0].copy()
                stagnation = 0

            # Идеальное расписание найдено — досрочный выход.
            if self._is_ideal(best):
                warnings.append(
                    f'GA остановлен досрочно: найдено решение без конфликтов и окон '
                    f'(поколение {generation_number}).'
                )
                break

            if best.hard_penalty == 0 and stagnation >= profile.stagnation_limit:
                if budget_is_user_override:
                    # При пользовательском лимите не вылетаем по стагнации —
                    # используем оставшееся время для эволюции. Multi-restart
                    # пробовали — он на больших задачах вытеснял качественные
                    # хромосомы свежими и популяция деградировала. Лучше дать
                    # GA крутить как есть.
                    pass
                else:
                    minimum_generations = max(6, profile.generations // 3)
                    if generation_number >= minimum_generations:
                        break

        self._emit_progress(
            progress_callback,
            stage='repair',
            stage_label='Финальное улучшение',
            message='Точечно улучшаем лучший найденный вариант перед сохранением.',
            progress_percent=87,
        )
        repaired = self._hill_climb(
            chromosome=best,
            context=context,
            candidate_domains=candidate_domains,
            iterations=profile.local_search_iterations,
            bridge=bridge,
            domain_scan_limit=profile.hill_domain_scan_limit,
            candidate_slot_limit=profile.hill_candidate_limit,
        )
        repaired = self._compact_daily_starts(
            chromosome=repaired,
            context=context,
            candidate_domains=candidate_domains,
        )
        repaired = self._repair_class_windows(
            chromosome=repaired,
            context=context,
            candidate_domains=candidate_domains,
        )
        evaluate_chromosome(repaired, context)
        return repaired, warnings

    def _build_search_profile(self, context: GenerationContext) -> SearchProfile:
        ga_settings = context.settings.algorithm.ga
        lesson_count = len(context.lesson_requirements)
        class_count = max(1, len(context.class_ids))

        if lesson_count <= 24:
            population_cap = 18 + min(8, class_count * 2)
            generations_cap = 12 + min(8, class_count * 2)
            local_search_iterations = 1
            local_search_top_count = 1
            hill_domain_scan_limit = 4
            hill_candidate_limit = 3
            max_runtime_seconds = 8.0 + class_count * 1.5
            stagnation_limit = 3
            constructive_attempts = 12
        elif lesson_count <= 48:
            population_cap = 24 + min(8, class_count * 2)
            generations_cap = 16 + min(8, class_count * 2)
            local_search_iterations = 1
            local_search_top_count = 1
            hill_domain_scan_limit = 4
            hill_candidate_limit = 3
            max_runtime_seconds = 12.0 + class_count * 1.5
            stagnation_limit = 4
            constructive_attempts = 10
        elif lesson_count <= 84:
            population_cap = 28 + min(8, class_count * 2)
            generations_cap = 20 + min(8, class_count * 2)
            local_search_iterations = 1
            local_search_top_count = 2
            hill_domain_scan_limit = 4
            hill_candidate_limit = 3
            max_runtime_seconds = 16.0 + class_count * 1.5
            stagnation_limit = 4
            constructive_attempts = 8
        else:
            population_cap = 28 + min(6, class_count)
            generations_cap = 18 + min(6, class_count)
            local_search_iterations = 1
            local_search_top_count = 2
            hill_domain_scan_limit = 3
            hill_candidate_limit = 2
            max_runtime_seconds = 14.0 + class_count
            stagnation_limit = 5
            constructive_attempts = 6

        # Если пользователь задал жёсткий лимит времени, реальным
        # ограничителем будет он. Поэтому НЕ режем generations по
        # generations_cap (иначе цикл закончится раньше времени) и
        # переписываем max_runtime_seconds на пользовательский лимит.
        # Соседство hill-climb оставляем УЗКИМ — раздутые сканы делали одну
        # итерацию настолько дорогой, что за то же время проходило в 3-5 раз
        # меньше поколений. Большее число дешёвых итераций даёт лучший
        # результат, чем малое число дорогих.
        if self.ga_time_limit_seconds is not None and self.ga_time_limit_seconds > 0:
            effective_generations = max(8, ga_settings.generations)
            effective_runtime = float(self.ga_time_limit_seconds)
        else:
            effective_generations = max(8, min(ga_settings.generations, generations_cap))
            effective_runtime = max(6.0, max_runtime_seconds)

        return SearchProfile(
            population_size=max(12, min(ga_settings.population_size, population_cap)),
            generations=effective_generations,
            local_search_iterations=max(1, min(ga_settings.local_search_iterations, local_search_iterations)),
            local_search_interval=1,
            local_search_top_count=max(1, local_search_top_count),
            hill_domain_scan_limit=max(2, hill_domain_scan_limit),
            hill_candidate_limit=max(2, hill_candidate_limit),
            max_runtime_seconds=effective_runtime,
            stagnation_limit=max(2, stagnation_limit),
            constructive_attempts=max(4, constructive_attempts),
        )

    def _build_initial_population(
        self,
        context: GenerationContext,
        candidate_domains: dict[str, list[tuple[int, int]]],
        bridge: GeneticTablerBridge,
        csp_seed_solutions: list[dict[str, tuple[int, int]]],
        seed_population: list[Chromosome],
        population_size: int | None = None,
        constructive_attempts: int = 8,
    ) -> list[Chromosome]:
        population: list[Chromosome] = []
        for chromosome in seed_population:
            population.append(evaluate_chromosome(chromosome.copy(), context))

        target_population = population_size or context.settings.algorithm.ga.population_size
        target_csp = max(
            1,
            int(target_population * context.settings.algorithm.ga.csp_seed_fraction),
        )

        for solution in csp_seed_solutions[:target_csp]:
            chromosome = self._chromosome_from_solution(context, solution)
            population.append(evaluate_chromosome(chromosome, context))

        for _ in range(max(2, constructive_attempts)):
            if len(population) >= min(target_population, target_csp):
                break
            chromosome = self._construct_feasible_chromosome(
                context=context,
                candidate_domains=candidate_domains,
                bridge=bridge,
                random_only=False,
            )
            if chromosome is not None:
                population.append(chromosome)

        while len(population) < min(target_population, target_csp):
            chromosome = self._create_initial_chromosome(
                context=context,
                candidate_domains=candidate_domains,
                bridge=bridge,
            )
            population.append(chromosome)

        while len(population) < target_population:
            chromosome = self._construct_feasible_chromosome(
                context=context,
                candidate_domains=candidate_domains,
                bridge=bridge,
                random_only=True,
            )
            if chromosome is None:
                chromosome = self._create_initial_chromosome(
                    context=context,
                    candidate_domains=candidate_domains,
                    bridge=bridge,
                    random_only=True,
                )
            population.append(chromosome)
        return population

    def _chromosome_from_solution(
        self,
        context: GenerationContext,
        solution: dict[str, tuple[int, int]],
    ) -> Chromosome:
        placements = [
            Placement(*solution[requirement.lesson_id])
            for requirement in context.lesson_requirements
        ]
        return Chromosome(placements=placements)

    def _create_initial_chromosome(
        self,
        context: GenerationContext,
        candidate_domains: dict[str, list[tuple[int, int]]],
        bridge: GeneticTablerBridge,
        random_only: bool = False,
    ) -> Chromosome:
        state = self._initialize_usage_state(context)
        placement_map: dict[int, Placement] = {}
        ordered_requirements = sorted(
            enumerate(context.lesson_requirements),
            key=lambda item: (
                len(candidate_domains.get(item[1].lesson_id, [])),
                -item[1].difficulty_score,
                item[1].class_id,
            ),
        )

        for original_index, requirement in ordered_requirements:
            preferred_slots = []
            if random_only:
                preferred_slots.extend(bridge.random_slot_population(requirement, size=5))
            else:
                preferred_slot = bridge.random_slot_id()
                preferred_slots.append(preferred_slot)
                preferred_slots.append(bridge.mutate_slot(requirement, preferred_slot, smart=False))
            placement = self._pick_best_position(
                requirement=requirement,
                context=context,
                candidate_domains=candidate_domains,
                preferred_slots=preferred_slots,
                state=state,
            )
            placement_map[original_index] = placement
            self._apply_usage_state(state, requirement, placement)

        placements = [placement_map[index] for index in range(len(context.lesson_requirements))]
        chromosome = Chromosome(placements=placements)
        return evaluate_chromosome(chromosome, context)

    def _construct_feasible_chromosome(
        self,
        context: GenerationContext,
        candidate_domains: dict[str, list[tuple[int, int]]],
        bridge: GeneticTablerBridge,
        random_only: bool,
        attempts: int = 6,
    ) -> Chromosome | None:
        base_order = sorted(
            enumerate(context.lesson_requirements),
            key=lambda item: (
                len(candidate_domains.get(item[1].lesson_id, [])),
                -item[1].difficulty_score,
                item[1].class_id,
                item[1].teacher_id,
            ),
        )
        for attempt_index in range(max(1, attempts)):
            state = self._initialize_usage_state(context)
            placement_map: dict[int, Placement] = {}
            ordered_requirements = list(base_order)
            if attempt_index:
                ordered_requirements.sort(
                    key=lambda item: (
                        len(candidate_domains.get(item[1].lesson_id, [])),
                        -item[1].difficulty_score,
                        self.randomizer.random(),
                    ),
                )

            success = True
            for original_index, requirement in ordered_requirements:
                preferred_slots: list[int] = []
                if random_only:
                    preferred_slots.extend(bridge.random_slot_population(requirement, size=3))
                else:
                    preferred_slot = bridge.random_slot_id()
                    preferred_slots.append(preferred_slot)
                    preferred_slots.append(bridge.mutate_slot(requirement, preferred_slot, smart=False))

                placement = self._pick_feasible_position(
                    requirement=requirement,
                    context=context,
                    candidate_domains=candidate_domains,
                    preferred_slots=preferred_slots,
                    state=state,
                )
                if placement is None:
                    success = False
                    break
                placement_map[original_index] = placement
                self._apply_usage_state(state, requirement, placement)

            if not success:
                continue

            placements = [placement_map[index] for index in range(len(context.lesson_requirements))]
            return evaluate_chromosome(Chromosome(placements=placements), context)
        return None

    def _pick_feasible_position(
        self,
        requirement: LessonRequirement,
        context: GenerationContext,
        candidate_domains: dict[str, list[tuple[int, int]]],
        preferred_slots: Iterable[int],
        state: dict[str, object],
    ) -> Placement | None:
        candidates = list(candidate_domains.get(requirement.lesson_id, []))
        if not candidates:
            candidates = [
                (slot.id, room_id)
                for slot in context.time_slots
                for room_id, room in context.classrooms.items()
                if room.capacity >= requirement.min_capacity
            ]

        preferred_order = {slot_id: index for index, slot_id in enumerate(dict.fromkeys(preferred_slots))}
        candidates.sort(
            key=lambda item: (
                self._placement_cost(requirement, Placement(*item), context, state),
                preferred_order.get(item[0], 999),
                self.randomizer.random(),
            )
        )
        for slot_id, room_id in candidates:
            placement = Placement(slot_id, room_id)
            if self._is_hard_feasible(requirement, placement, context, state):
                return placement
        return None

    def _pick_best_position(
        self,
        requirement: LessonRequirement,
        context: GenerationContext,
        candidate_domains: dict[str, list[tuple[int, int]]],
        preferred_slots: Iterable[int],
        state: dict[str, object],
    ) -> Placement:
        # Кандидаты строятся ТОЛЬКО из слотов, где учитель доступен.
        candidates = [
            (slot_id, room_id)
            for slot_id, room_id in candidate_domains.get(requirement.lesson_id, [])
            if (requirement.teacher_id, slot_id) not in context.teacher_unavailability
        ]
        if not candidates:
            candidates = [
                (slot.id, room_id)
                for slot in context.time_slots
                for room_id, room in context.classrooms.items()
                if room.capacity >= requirement.min_capacity
                and (requirement.teacher_id, slot.id) not in context.teacher_unavailability
            ]
        if not candidates:
            # Полная инфизибельность по доступности — возвращаемся к старому поведению
            # (с пометкой в фитнесе как hard penalty), но это исключительный случай.
            candidates = [
                (slot.id, room_id)
                for slot in context.time_slots
                for room_id, room in context.classrooms.items()
                if room.capacity >= requirement.min_capacity
            ]

        preferred_order = {slot_id: index for index, slot_id in enumerate(dict.fromkeys(preferred_slots))}
        candidates.sort(
            key=lambda item: (
                self._placement_cost(requirement, Placement(*item), context, state),
                preferred_order.get(item[0], 999),
                self.randomizer.random(),
            )
        )
        best_slot_id, best_room_id = candidates[0]
        return Placement(best_slot_id, best_room_id)

    def _is_hard_feasible(
        self,
        requirement: LessonRequirement,
        placement: Placement,
        context: GenerationContext,
        state: dict[str, object],
    ) -> bool:
        slot_lookup = state['slot_lookup']
        slot = slot_lookup[placement.time_slot_id]
        room = context.classrooms[placement.classroom_id]
        weekday = slot.weekday

        if (requirement.class_id, weekday, placement.time_slot_id) in state['used_class']:
            return False
        if (requirement.teacher_id, weekday, placement.time_slot_id) in state['used_teacher']:
            return False
        if (placement.classroom_id, weekday, placement.time_slot_id) in state['used_room']:
            return False
        if (requirement.teacher_id, placement.time_slot_id) in context.teacher_unavailability:
            return False
        if room.room_type != requirement.required_room_type:
            return False
        if room.capacity < requirement.min_capacity:
            return False

        projected_subject_count = state['subject_daily'].get((requirement.class_id, requirement.subject_id, weekday), 0) + 1
        if projected_subject_count > requirement.daily_limit:
            return False

        projected_teacher_daily = state['teacher_daily_counts'].get((requirement.teacher_id, weekday), 0) + 1
        if projected_teacher_daily > requirement.teacher_daily_limit:
            return False

        day_has_pe = state['class_day_has_pe'].get((requirement.class_id, weekday), False) or requirement.is_pe_lesson
        projected_class_daily = state['class_daily_counts'].get((requirement.class_id, weekday), 0) + 1
        class_daily_limit = context.sanpin_validator.daily_lesson_limit(requirement.class_grade, pe_bonus=day_has_pe)
        if projected_class_daily > class_daily_limit:
            return False

        if context.settings.sanpin.enable_score_caps:
            projected_daily_score = state['class_daily_scores'].get((requirement.class_id, weekday), 0) + requirement.difficulty_score
            if projected_daily_score > context.sanpin_validator.daily_score_limit(requirement.class_grade):
                return False

            projected_weekly_score = state['class_weekly_scores'].get(requirement.class_id, 0) + requirement.difficulty_score
            if projected_weekly_score > context.sanpin_validator.weekly_score_limit(requirement.class_grade):
                return False

        return True

    def _placement_cost(
        self,
        requirement: LessonRequirement,
        placement: Placement,
        context: GenerationContext,
        state: dict[str, object],
    ) -> int:
        slot_lookup = state['slot_lookup']
        last_lesson_by_weekday = state['last_lesson_by_weekday']
        slot = slot_lookup[placement.time_slot_id]
        room = context.classrooms[placement.classroom_id]
        weekday = slot.weekday

        used_class = state['used_class']
        used_teacher = state['used_teacher']
        used_room = state['used_room']
        subject_daily = state['subject_daily']
        class_daily_counts = state['class_daily_counts']
        teacher_daily_counts = state['teacher_daily_counts']
        class_daily_numbers = state['class_daily_numbers']
        teacher_daily_numbers = state['teacher_daily_numbers']
        class_daily_lessons = state['class_daily_lessons']
        class_daily_scores = state['class_daily_scores']
        class_weekly_scores = state['class_weekly_scores']
        class_day_has_pe = state['class_day_has_pe']

        cost = 0
        if (requirement.class_id, weekday, placement.time_slot_id) in used_class:
            cost += 2400
        if (requirement.teacher_id, weekday, placement.time_slot_id) in used_teacher:
            cost += 2400
        if (placement.classroom_id, weekday, placement.time_slot_id) in used_room:
            cost += 2400
        if (requirement.teacher_id, placement.time_slot_id) in context.teacher_unavailability:
            cost += 2200
        if room.room_type != requirement.required_room_type:
            cost += 2000
        if room.capacity < requirement.min_capacity:
            cost += 2000

        projected_subject_count = subject_daily.get((requirement.class_id, requirement.subject_id, weekday), 0) + 1
        if projected_subject_count > requirement.daily_limit:
            cost += (projected_subject_count - requirement.daily_limit) * 1500

        projected_teacher_daily = teacher_daily_counts.get((requirement.teacher_id, weekday), 0) + 1
        if projected_teacher_daily > requirement.teacher_daily_limit:
            cost += (projected_teacher_daily - requirement.teacher_daily_limit) * 1200

        day_has_pe = class_day_has_pe.get((requirement.class_id, weekday), False) or requirement.is_pe_lesson
        projected_class_daily = class_daily_counts.get((requirement.class_id, weekday), 0) + 1
        class_daily_limit = context.sanpin_validator.daily_lesson_limit(requirement.class_grade, pe_bonus=day_has_pe)
        if projected_class_daily > class_daily_limit:
            cost += (projected_class_daily - class_daily_limit) * 1600

        projected_daily_score = class_daily_scores.get((requirement.class_id, weekday), 0) + requirement.difficulty_score
        if context.settings.sanpin.enable_score_caps:
            daily_score_limit = context.sanpin_validator.daily_score_limit(requirement.class_grade)
            if projected_daily_score > daily_score_limit:
                cost += (projected_daily_score - daily_score_limit) * 200

            projected_weekly_score = class_weekly_scores.get(requirement.class_id, 0) + requirement.difficulty_score
            weekly_score_limit = context.sanpin_validator.weekly_score_limit(requirement.class_grade)
            if projected_weekly_score > weekly_score_limit:
                cost += (projected_weekly_score - weekly_score_limit) * 120

        cost += self._start_and_gap_penalty(
            slot.lesson_number,
            class_daily_numbers.get((requirement.class_id, weekday), []),
            weight_late_start=80,
            weight_gap=45,
        )
        cost += self._start_and_gap_penalty(
            slot.lesson_number,
            teacher_daily_numbers.get((requirement.teacher_id, weekday), []),
            weight_late_start=20,
            weight_gap=18,
        )
        cost += self._alternation_penalty(
            lesson_number=slot.lesson_number,
            subject_group=requirement.alternation_group,
            class_grade=requirement.class_grade,
            existing_lessons=class_daily_lessons.get((requirement.class_id, weekday), []),
        )
        cost += self._double_lesson_penalty(
            lesson_number=slot.lesson_number,
            subject_name=requirement.subject_name,
            allows_double_lesson=requirement.allows_double_lesson,
            existing_lessons=class_daily_lessons.get((requirement.class_id, weekday), []),
        )
        if requirement.teacher_preferences.avoid_first_lesson and slot.lesson_number == 1:
            cost += 90
        if requirement.teacher_preferences.avoid_last_lesson and slot.lesson_number == max(
            1,
            last_lesson_by_weekday.get(weekday, context.settings.school.max_lessons_per_day),
        ):
            cost += 90
        if requirement.is_hard_subject and slot.lesson_number in {
            1,
            max(1, last_lesson_by_weekday.get(weekday, context.settings.school.max_lessons_per_day)),
        }:
            cost += 80
        if requirement.is_hard_subject and weekday in {2, 3}:
            cost -= 12

        # Дополнительная коррекция позиции для старших классов: тяжёлые предметы
        # должны идти в первые 3 урока. Для 9 и 11 классов коэффициент усилен.
        if requirement.is_hard_subject and slot.lesson_number > 3:
            late_excess = slot.lesson_number - 3
            if requirement.class_grade in {9, 11}:
                cost += late_excess * 140
            elif requirement.class_grade >= 9:
                cost += late_excess * 80
            elif requirement.class_grade >= 5:
                cost += late_excess * 35
        if requirement.is_hard_subject and slot.lesson_number <= 3 and requirement.class_grade in {9, 11}:
            cost -= 60
        return cost

    def _crossover(
        self,
        parent_a: Chromosome,
        parent_b: Chromosome,
        context: GenerationContext,
        candidate_domains: dict[str, list[tuple[int, int]]],
        bridge: GeneticTablerBridge,
    ) -> Chromosome:
        placements: list[Placement] = []
        for requirement, placement_a, placement_b in zip(
            context.lesson_requirements,
            parent_a.placements,
            parent_b.placements,
        ):
            if self.randomizer.random() > context.settings.algorithm.ga.crossover_rate:
                inherited = placement_a if self.randomizer.random() < 0.5 else placement_b
                safe_slot_id = self._safe_slot(
                    requirement=requirement,
                    proposed_slot_id=inherited.time_slot_id,
                    candidate_domains=candidate_domains,
                    context=context,
                )
                safe_room_id = self._pick_room_for_slot(
                    requirement=requirement,
                    slot_id=safe_slot_id,
                    current_room_id=inherited.classroom_id,
                    candidate_domains=candidate_domains,
                    context=context,
                )
                placements.append(Placement(safe_slot_id, safe_room_id))
                continue

            slot_id = bridge.crossover_slot(
                requirement,
                placement_a.time_slot_id,
                placement_b.time_slot_id,
                use_uniform=self.randomizer.random() < 0.2,
            )
            slot_id = self._safe_slot(
                requirement=requirement,
                proposed_slot_id=slot_id,
                candidate_domains=candidate_domains,
                context=context,
            )
            room_id = self._pick_room_for_slot(
                requirement=requirement,
                slot_id=slot_id,
                current_room_id=placement_a.classroom_id if self.randomizer.random() < 0.5 else placement_b.classroom_id,
                candidate_domains=candidate_domains,
                context=context,
            )
            placements.append(Placement(slot_id, room_id))

        return Chromosome(placements=placements)

    def _mutate(
        self,
        chromosome: Chromosome,
        context: GenerationContext,
        candidate_domains: dict[str, list[tuple[int, int]]],
        bridge: GeneticTablerBridge,
        mutation_rate: float,
        smart: bool,
    ) -> Chromosome:
        mutated = chromosome.copy()
        for index, requirement in enumerate(context.lesson_requirements):
            if self.randomizer.random() > mutation_rate:
                continue
            current = mutated.placements[index]
            slot_id = bridge.mutate_slot(
                requirement=requirement,
                current_slot_id=current.time_slot_id,
                smart=smart,
            )
            slot_id = self._safe_slot(
                requirement=requirement,
                proposed_slot_id=slot_id,
                candidate_domains=candidate_domains,
                context=context,
            )
            room_id = self._pick_room_for_slot(
                requirement=requirement,
                slot_id=slot_id,
                current_room_id=current.classroom_id,
                candidate_domains=candidate_domains,
                context=context,
            )
            mutated.placements[index] = Placement(slot_id, room_id)
        return mutated

    def _apply_local_search(
        self,
        population: list[Chromosome],
        context: GenerationContext,
        candidate_domains: dict[str, list[tuple[int, int]]],
        bridge: GeneticTablerBridge,
        profile: SearchProfile | None = None,
    ) -> list[Chromosome]:
        profile = profile or self._build_search_profile(context)
        fraction = context.settings.algorithm.ga.local_search_fraction
        dynamic_top_count = int(round(len(population) * fraction)) if fraction > 0 else 1
        top_count = max(1, min(len(population), profile.local_search_top_count, dynamic_top_count or 1))
        improved: list[Chromosome] = []
        for chromosome in population[:top_count]:
            improved.append(
                self._hill_climb(
                    chromosome=chromosome,
                    context=context,
                    candidate_domains=candidate_domains,
                    iterations=profile.local_search_iterations,
                    bridge=bridge,
                    domain_scan_limit=profile.hill_domain_scan_limit,
                    candidate_slot_limit=profile.hill_candidate_limit,
                )
            )
        improved.extend(item.copy() for item in population[top_count:])
        return improved

    def _hill_climb(
        self,
        chromosome: Chromosome,
        context: GenerationContext,
        candidate_domains: dict[str, list[tuple[int, int]]],
        iterations: int,
        bridge: GeneticTablerBridge,
        domain_scan_limit: int = 12,
        candidate_slot_limit: int = 8,
    ) -> Chromosome:
        best = evaluate_chromosome(chromosome.copy(), context)
        indices = list(range(len(context.lesson_requirements)))
        for _ in range(max(1, iterations)):
            self.randomizer.shuffle(indices)
            improved = False
            for index in indices:
                requirement = context.lesson_requirements[index]
                current = best.placements[index]
                proposed_slots = [
                    current.time_slot_id,
                    bridge.mutate_slot(requirement, current.time_slot_id, smart=True),
                    bridge.mutate_slot(requirement, current.time_slot_id, smart=False),
                ]
                allowed_slot_ids = {
                    slot_id for slot_id, _room_id in candidate_domains.get(requirement.lesson_id, [])
                }
                candidate_slots: list[int] = []
                seen_slots: set[int] = set()
                for slot_id in proposed_slots:
                    safe_id = self._safe_slot(
                        requirement=requirement,
                        proposed_slot_id=slot_id,
                        candidate_domains=candidate_domains,
                        context=context,
                    )
                    if safe_id not in seen_slots:
                        candidate_slots.append(safe_id)
                        seen_slots.add(safe_id)
                for slot_id, _room_id in candidate_domains.get(requirement.lesson_id, [])[: max(1, domain_scan_limit)]:
                    if slot_id in seen_slots:
                        continue
                    if allowed_slot_ids and slot_id not in allowed_slot_ids:
                        continue
                    candidate_slots.append(slot_id)
                    seen_slots.add(slot_id)

                trial_best = best
                for slot_id in candidate_slots[: max(1, candidate_slot_limit)]:
                    candidate_room_id = self._pick_room_for_slot(
                        requirement=requirement,
                        slot_id=slot_id,
                        current_room_id=current.classroom_id,
                        candidate_domains=candidate_domains,
                        context=context,
                    )
                    candidate = best.copy()
                    candidate.placements[index] = Placement(slot_id, candidate_room_id)
                    evaluate_chromosome(candidate, context)
                    if self._is_better(candidate, trial_best):
                        trial_best = candidate
                if self._is_better(trial_best, best):
                    best = trial_best
                    improved = True
            if not improved:
                break
        return best

    def _safe_slot(
        self,
        requirement: LessonRequirement,
        proposed_slot_id: int,
        candidate_domains: dict[str, list[tuple[int, int]]],
        context: GenerationContext,
    ) -> int:
        """Гарантирует, что предложенный слот допустим для требования.

        Главный инвариант: учитель, помеченный как болеет/выходной для
        конкретного слота, не должен попасть в выбор НИКАКИМ путём.
        """
        allowed_slot_ids = {
            slot_id for slot_id, _room_id in candidate_domains.get(requirement.lesson_id, [])
        }
        teacher_unavailable = context.teacher_unavailability
        proposed_teacher_ok = (requirement.teacher_id, proposed_slot_id) not in teacher_unavailable
        if proposed_slot_id in allowed_slot_ids and proposed_teacher_ok:
            return proposed_slot_id
        if allowed_slot_ids:
            ordered = sorted(allowed_slot_ids)
            return self.randomizer.choice(ordered)
        fallback = [
            slot.id
            for slot in context.time_slots
            if (requirement.teacher_id, slot.id) not in teacher_unavailable
        ]
        if fallback:
            return self.randomizer.choice(fallback)
        return proposed_slot_id

    def _pick_room_for_slot(
        self,
        requirement: LessonRequirement,
        slot_id: int,
        current_room_id: int,
        candidate_domains: dict[str, list[tuple[int, int]]],
        context: GenerationContext,
    ) -> int:
        # Если на этот слот педагог недоступен — что бы ни произошло выше по стеку,
        # на этой стадии стараемся не вернуть кабинет для невозможного урока.
        # (Слот всё равно отфильтруется на уровне _safe_slot, но защищаемся явно.)
        if (requirement.teacher_id, slot_id) in context.teacher_unavailability:
            # сохраняем текущий кабинет, чтобы не плодить ещё одну ошибку
            return current_room_id

        candidates = [
            room_id
            for candidate_slot_id, room_id in candidate_domains.get(requirement.lesson_id, [])
            if candidate_slot_id == slot_id
        ]
        if current_room_id in candidates:
            return current_room_id
        if candidates:
            return self.randomizer.choice(candidates)

        fallback = [
            room_id
            for room_id, room in context.classrooms.items()
            if room.capacity >= requirement.min_capacity
            and room.room_type == requirement.required_room_type
        ]
        if fallback:
            return self.randomizer.choice(fallback)
        return current_room_id

    @staticmethod
    def _emit_progress(
        callback: ProgressCallback | None,
        *,
        stage: str,
        stage_label: str,
        message: str,
        progress_percent: int,
    ) -> None:
        if callback is None:
            return
        callback(stage, stage_label, message, progress_percent)

    @staticmethod
    def _generation_progress_percent(
        generation_number: int,
        total_generations: int,
        start_percent: int,
        end_percent: int,
    ) -> int:
        if total_generations <= 1:
            return end_percent
        span = max(1, end_percent - start_percent)
        completed = generation_number - 1
        return min(end_percent, start_percent + int(span * completed / max(1, total_generations - 1)))

    def _initialize_usage_state(self, context: GenerationContext) -> dict[str, object]:
        state = {
            'used_class': set(),
            'used_teacher': set(),
            'used_room': set(),
            'subject_daily': defaultdict(int),
            'class_daily_counts': defaultdict(int),
            'teacher_daily_counts': defaultdict(int),
            'class_daily_numbers': defaultdict(list),
            'teacher_daily_numbers': defaultdict(list),
            'class_daily_lessons': defaultdict(list),
            'class_daily_scores': defaultdict(int),
            'class_weekly_scores': defaultdict(int),
            'class_day_has_pe': defaultdict(bool),
            'slot_lookup': {slot.id: slot for slot in context.time_slots},
            'last_lesson_by_weekday': {
                weekday: max(
                    (slot.lesson_number for slot in context.time_slots if slot.weekday == weekday),
                    default=context.settings.school.max_lessons_per_day,
                )
                for weekday in context.weekday_numbers
            },
        }
        slot_lookup = {slot.id: slot for slot in context.time_slots}
        for fixed in context.fixed_lessons:
            slot = slot_lookup.get(fixed.time_slot_id)
            if slot is None:
                continue
            placement = Placement(fixed.time_slot_id, fixed.classroom_id)
            requirement = LessonRequirement(
                lesson_id=f'fixed:{fixed.class_id}:{fixed.subject_id}:{fixed.time_slot_id}',
                class_id=fixed.class_id,
                class_name=context.class_names.get(fixed.class_id, str(fixed.class_id)),
                class_grade=fixed.class_grade,
                class_daily_limit=context.class_daily_limits.get(fixed.class_id, context.settings.school.max_lessons_per_day),
                class_weekly_limit=context.class_weekly_limits.get(fixed.class_id, 0),
                subject_id=fixed.subject_id,
                subject_name=fixed.subject_name,
                difficulty_score=fixed.difficulty_score,
                is_pe_lesson=is_pe_subject(fixed.subject_name),
                is_hard_subject=fixed.difficulty_score >= 8,
                alternation_group=requirement_group(fixed.subject_name, fixed.class_grade),
                allows_double_lesson=True,
                teacher_id=fixed.teacher_id,
                teacher_name='',
                teacher_preferences=self._empty_preferences(),
                required_room_type=fixed.required_room_type,
                min_capacity=0,
                daily_limit=99,
                teacher_daily_limit=99,
            )
            self._apply_usage_state(state, requirement, placement)
        return state

    def _apply_usage_state(
        self,
        state: dict[str, object],
        requirement: LessonRequirement,
        placement: Placement,
    ) -> None:
        slot_lookup = state.get('slot_lookup')
        if slot_lookup is None:
            raise RuntimeError('Usage state is missing slot_lookup')
        slot_data = slot_lookup[placement.time_slot_id]
        weekday = slot_data.weekday
        state['used_class'].add((requirement.class_id, weekday, placement.time_slot_id))
        state['used_teacher'].add((requirement.teacher_id, weekday, placement.time_slot_id))
        state['used_room'].add((placement.classroom_id, weekday, placement.time_slot_id))
        state['subject_daily'][(requirement.class_id, requirement.subject_id, weekday)] += 1
        state['class_daily_counts'][(requirement.class_id, weekday)] += 1
        state['teacher_daily_counts'][(requirement.teacher_id, weekday)] += 1
        state['class_daily_numbers'][(requirement.class_id, weekday)].append(slot_data.lesson_number)
        state['teacher_daily_numbers'][(requirement.teacher_id, weekday)].append(slot_data.lesson_number)
        state['class_daily_lessons'][(requirement.class_id, weekday)].append(
            (slot_data.lesson_number, requirement.subject_name, requirement.required_room_type)
        )
        state['class_daily_scores'][(requirement.class_id, weekday)] += requirement.difficulty_score
        state['class_weekly_scores'][requirement.class_id] += requirement.difficulty_score
        state['class_day_has_pe'][(requirement.class_id, weekday)] = (
            state['class_day_has_pe'][(requirement.class_id, weekday)] or requirement.is_pe_lesson
        )

    def _postprocess(
        self,
        context: GenerationContext,
        best: Chromosome,
        progress_callback: ProgressCallback | None = None,
    ) -> tuple[Chromosome, list[str]]:
        warnings: list[str] = []
        problematic_keys = {
            'sanpin_daily_score_overload',
            'sanpin_peak_distribution_violation',
            'sanpin_primary_light_day_violation',
            'hard_subject_position_violations',
        }
        if not any(best.diagnostics.get(key, 0) for key in problematic_keys):
            return best, warnings
        if len(context.lesson_requirements) > 60 and best.hard_penalty == 0:
            warnings.append('Skipped the extra GA post-processing pass for a large problem; hard constraints are already satisfied.')
            return best, warnings

        boosted_weights = replace(
            context.settings.algorithm.weights,
            sanpin_score_penalty=context.settings.algorithm.weights.sanpin_score_penalty * 1.4,
            peak_day_penalty=context.settings.algorithm.weights.peak_day_penalty * 1.4,
            hard_subject_position_penalty=context.settings.algorithm.weights.hard_subject_position_penalty * 1.25,
        )
        boosted_settings = replace(
            context.settings,
            algorithm=replace(context.settings.algorithm, weights=boosted_weights),
        )
        boosted_context = load_generation_context(
            week_start=context.week_start,
            class_ids=context.class_ids,
            settings=boosted_settings,
        )
        self._emit_progress(
            progress_callback,
            stage='postprocessing',
            stage_label='Финальная проверка',
            message='Нашли спорные места по СанПиН. Запускаем усиленную донастройку штрафов.',
            progress_percent=93,
        )
        boosted_best, boosted_warnings = self._optimize(
            boosted_context,
            seed_population=[best],
            progress_callback=progress_callback,
        )
        warnings.extend(boosted_warnings)
        if self._is_better(boosted_best, best):
            warnings.append('Выполнен повторный прогон GA с усиленными весами SanPiN-нарушений.')
            return boosted_best, warnings
        return best, warnings

    def _repair_class_windows(
        self,
        chromosome: Chromosome,
        context: GenerationContext,
        candidate_domains: dict[str, list[tuple[int, int]]],
        max_passes: int = 8,
    ) -> Chromosome:
        """Удаляет внутренние окна между уроками класса.

        Для каждого (класс, день) ищет пропуски в нумерации уроков и пытается
        переставить более поздний урок в свободный слот, если это не нарушает
        жёстких ограничений (учитель доступен, нет пересечений, тип/вместимость
        кабинета). Алгоритм детерминирован: выбирает первый улучшающий ход и
        повторяет, пока есть прогресс.
        """
        repaired = evaluate_chromosome(chromosome.copy(), context)
        slot_lookup = {slot.id: slot for slot in context.time_slots}

        fixed_numbers_by_class_day: dict[tuple[int, int], set[int]] = {}
        for fixed in context.fixed_lessons:
            slot = slot_lookup.get(fixed.time_slot_id)
            if slot is None:
                continue
            fixed_numbers_by_class_day.setdefault((fixed.class_id, slot.weekday), set()).add(slot.lesson_number)

        for _pass_index in range(max_passes):
            progress = False
            for class_id in context.class_ids:
                for weekday in context.weekday_numbers:
                    day_indices = [
                        index
                        for index, (req, place) in enumerate(zip(context.lesson_requirements, repaired.placements))
                        if req.class_id == class_id and slot_lookup[place.time_slot_id].weekday == weekday
                    ]
                    fixed_numbers = fixed_numbers_by_class_day.get((class_id, weekday), set())
                    dynamic_numbers = {slot_lookup[repaired.placements[i].time_slot_id].lesson_number for i in day_indices}
                    all_numbers = dynamic_numbers | fixed_numbers
                    if len(all_numbers) < 2:
                        continue
                    numbers_present = sorted(all_numbers)
                    full_range = set(range(min(numbers_present), max(numbers_present) + 1))
                    gaps = sorted(full_range - all_numbers)
                    if not gaps:
                        continue

                    # Заполняем каждое окно по очереди (от меньшего номера к большему),
                    # перенося в него самый поздний урок дня.
                    moved_this_day = False
                    for gap_number in gaps:
                        target_slot_id = context.slot_id_by_weekday_and_number.get((weekday, gap_number))
                        if target_slot_id is None:
                            continue
                        later_first = sorted(
                            day_indices,
                            key=lambda item: -slot_lookup[repaired.placements[item].time_slot_id].lesson_number,
                        )
                        for index in later_first:
                            current_placement = repaired.placements[index]
                            current_number = slot_lookup[current_placement.time_slot_id].lesson_number
                            if current_number <= gap_number:
                                continue
                            requirement = context.lesson_requirements[index]
                            if (requirement.teacher_id, target_slot_id) in context.teacher_unavailability:
                                continue
                            target_room_id = self._pick_room_for_slot(
                                requirement=requirement,
                                slot_id=target_slot_id,
                                current_room_id=current_placement.classroom_id,
                                candidate_domains=candidate_domains,
                                context=context,
                            )
                            candidate = repaired.copy()
                            candidate.placements[index] = Placement(target_slot_id, target_room_id)
                            evaluate_chromosome(candidate, context)
                            if candidate.hard_penalty <= repaired.hard_penalty and self._is_better(candidate, repaired):
                                repaired = candidate
                                progress = True
                                moved_this_day = True
                                break
                            # Если простой move не помог — пробуем Kempe-swap:
                            # урок «противника» уезжает на наш текущий слот.
                            kempe_result = self._try_kempe_swap(
                                chromosome=repaired,
                                context=context,
                                candidate_domains=candidate_domains,
                                our_index=index,
                                target_slot_id=target_slot_id,
                            )
                            if kempe_result is not None:
                                repaired = kempe_result
                                progress = True
                                moved_this_day = True
                                break
                        if moved_this_day:
                            break
            if not progress:
                break
        return repaired

    def _compact_daily_starts(
        self,
        chromosome: Chromosome,
        context: GenerationContext,
        candidate_domains: dict[str, list[tuple[int, int]]],
    ) -> Chromosome:
        compacted = evaluate_chromosome(chromosome.copy(), context)
        slot_lookup = {slot.id: slot for slot in context.time_slots}
        for class_id in context.class_ids:
            for weekday in context.weekday_numbers:
                lesson_indices = [
                    index
                    for index, (requirement, placement) in enumerate(zip(context.lesson_requirements, compacted.placements))
                    if requirement.class_id == class_id and slot_lookup[placement.time_slot_id].weekday == weekday
                ]
                if not lesson_indices:
                    continue
                earliest_index = min(
                    lesson_indices,
                    key=lambda item: slot_lookup[compacted.placements[item].time_slot_id].lesson_number,
                )
                earliest_placement = compacted.placements[earliest_index]
                earliest_number = slot_lookup[earliest_placement.time_slot_id].lesson_number
                for target_number in range(1, earliest_number):
                    target_slot_id = context.slot_id_by_weekday_and_number.get((weekday, target_number))
                    if target_slot_id is None:
                        continue
                    requirement = context.lesson_requirements[earliest_index]
                    candidate = compacted.copy()
                    candidate.placements[earliest_index] = Placement(
                        target_slot_id,
                        self._pick_room_for_slot(
                            requirement=requirement,
                            slot_id=target_slot_id,
                            current_room_id=earliest_placement.classroom_id,
                            candidate_domains=candidate_domains,
                            context=context,
                        ),
                    )
                    evaluate_chromosome(candidate, context)
                    if self._is_better(candidate, compacted):
                        compacted = candidate
                        break
        return compacted

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

    def _try_kempe_swap(
        self,
        chromosome: Chromosome,
        context: GenerationContext,
        candidate_domains: dict[str, list[tuple[int, int]]],
        our_index: int,
        target_slot_id: int,
    ) -> Chromosome | None:
        """Kempe-chain длины 2: пытаемся переместить our_index в target_slot_id,
        одновременно отправив блокирующий чужой урок в наш текущий слот.

        Это «универсальный обмен» для расписания: когда обычный move невозможен
        потому что в целевом слоте уже стоит чужой урок (другой класс, тот же
        учитель или та же комната), мы вместо move делаем swap. Если оба новых
        размещения феасибельны и общая хромосома хотя бы не хуже — применяем.
        """
        our_req = context.lesson_requirements[our_index]
        our_current = chromosome.placements[our_index]
        slot_lookup = {slot.id: slot for slot in context.time_slots}

        # Шорт-сёрвинг: учителю нашего урока вообще можно работать в целевом слоте?
        if (our_req.teacher_id, target_slot_id) in context.teacher_unavailability:
            return None

        target_weekday = slot_lookup[target_slot_id].weekday

        # Ищем «блокировщиков» в целевом слоте: уроки, которые конфликтуют
        # с нашим по классу, учителю или (с большой вероятностью) по комнате.
        blocking_indices: list[int] = []
        for index, (req, place) in enumerate(zip(context.lesson_requirements, chromosome.placements)):
            if index == our_index:
                continue
            if place.time_slot_id != target_slot_id:
                continue
            if req.class_id == our_req.class_id:
                blocking_indices.append(index)
                continue
            if req.teacher_id == our_req.teacher_id:
                blocking_indices.append(index)
                continue
            # Тот же класс или комната, что мы хотим использовать — потенциальный конфликт.
            if place.classroom_id == our_current.classroom_id:
                blocking_indices.append(index)

        if not blocking_indices:
            return None

        for other_index in blocking_indices:
            other_req = context.lesson_requirements[other_index]
            other_current = chromosome.placements[other_index]
            # Учитель «другого» урока должен быть доступен в исходном слоте «нашего».
            if (other_req.teacher_id, our_current.time_slot_id) in context.teacher_unavailability:
                continue

            our_new_slot = target_slot_id
            other_new_slot = our_current.time_slot_id

            our_new_room = self._pick_room_for_slot(
                requirement=our_req,
                slot_id=our_new_slot,
                current_room_id=other_current.classroom_id,
                candidate_domains=candidate_domains,
                context=context,
            )
            other_new_room = self._pick_room_for_slot(
                requirement=other_req,
                slot_id=other_new_slot,
                current_room_id=our_current.classroom_id,
                candidate_domains=candidate_domains,
                context=context,
            )

            candidate = chromosome.copy()
            candidate.placements[our_index] = Placement(our_new_slot, our_new_room)
            candidate.placements[other_index] = Placement(other_new_slot, other_new_room)
            evaluate_chromosome(candidate, context)

            # Жёсткий инвариант: после Kempe-swap не должно стать ХУЖЕ по hard.
            if candidate.hard_penalty <= chromosome.hard_penalty and self._is_better(candidate, chromosome):
                return candidate
        return None

    def _is_ideal(self, chromosome: Chromosome) -> bool:
        """«Идеальная» хромосома: ни одного жёсткого нарушения и ни одного
        внутреннего окна между уроками класса.

        Используется как условие досрочного выхода из GA по требованию
        пользователя — как только такая хромосома найдена, дальше популяции
        не создаются.
        """
        if chromosome.hard_penalty != 0:
            return False
        if chromosome.diagnostics.get('class_gap', 0) != 0:
            return False
        return True

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
            existing_group = requirement_group(existing_subject, class_grade)
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
                return 90
        return 0

    def _empty_preferences(self):
        class _Preferences:
            avoid_first_lesson = False
            avoid_last_lesson = False
            preferred_weekdays = ()
            avoid_weekdays = ()
            preferred_lesson_numbers = ()
            avoid_lesson_numbers = ()

        return _Preferences()


def requirement_group(subject_name: str, class_grade: int) -> str:
    from .school_rules import alternation_group

    return alternation_group(subject_name, class_grade)
