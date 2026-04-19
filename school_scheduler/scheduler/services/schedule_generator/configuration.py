from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any

import yaml


WEEKDAY_NAME_TO_NUMBER = {
    'monday': 1,
    'tuesday': 2,
    'wednesday': 3,
    'thursday': 4,
    'friday': 5,
    'saturday': 6,
}


DEFAULT_CONFIG_RELATIVE_PATH = Path('config.yaml')


@dataclass(frozen=True)
class CSPSettings:
    timeout_seconds: int = 30
    solver_backtrack: bool = True
    seed_solution_limit: int = 100


@dataclass(frozen=True)
class GASettings:
    population_size: int = 200
    generations: int = 500
    crossover_rate: float = 0.85
    mutation_rate: float = 0.15
    elitism_count: int = 10
    local_search_iterations: int = 20
    csp_seed_fraction: float = 0.5
    local_search_fraction: float = 0.1


@dataclass(frozen=True)
class WeightSettings:
    weekly_load_penalty: float = 10.0
    daily_unevenness_penalty: float = 8.0
    teacher_gap_penalty: float = 5.0
    class_gap_penalty: float = 5.0
    teacher_preference_penalty: float = 6.0
    hard_subject_position_penalty: float = 7.0
    doubled_subject_penalty: float = 8.0
    sanpin_score_penalty: float = 15.0
    peak_day_penalty: float = 6.0
    class_window_penalty: float = 6.0
    teacher_window_penalty: float = 5.0


@dataclass(frozen=True)
class AlgorithmSettings:
    csp: CSPSettings
    ga: GASettings
    weights: WeightSettings


@dataclass(frozen=True)
class SchoolSettings:
    name: str
    start_time: time
    lesson_duration_minutes: int
    breaks: tuple[int, ...]
    shifts: int
    max_lessons_per_day: int
    multi_shift_break_minutes: int
    sanpin_compliance: bool
    has_extracurricular: bool
    weekdays: tuple[int, ...]


@dataclass(frozen=True)
class SanPinSettings:
    enable_score_caps: bool
    primary_light_weekday: int
    middle_peak_weekdays: tuple[int, ...]
    high_peak_weekdays: tuple[int, ...]


@dataclass(frozen=True)
class SchedulerSettings:
    algorithm: AlgorithmSettings
    school: SchoolSettings
    sanpin: SanPinSettings
    source_path: Path | None = None


def load_scheduler_settings(config_path: str | Path | None = None) -> SchedulerSettings:
    resolved_path = _resolve_config_path(config_path)
    payload: dict[str, Any] = {}
    if resolved_path and resolved_path.exists():
        with resolved_path.open('r', encoding='utf-8') as file_obj:
            payload = yaml.safe_load(file_obj) or {}

    algorithm_payload = payload.get('algorithm', {})
    csp_payload = algorithm_payload.get('csp', {})
    ga_payload = algorithm_payload.get('ga', {})
    weight_payload = algorithm_payload.get('weights', {})

    school_payload = payload.get('school', {})
    sanpin_payload = payload.get('sanpin', {})

    return SchedulerSettings(
        algorithm=AlgorithmSettings(
            csp=CSPSettings(
                timeout_seconds=int(csp_payload.get('timeout_seconds', 30)),
                solver_backtrack=bool(csp_payload.get('solver_backtrack', True)),
                seed_solution_limit=int(csp_payload.get('seed_solution_limit', 100)),
            ),
            ga=GASettings(
                population_size=int(ga_payload.get('population_size', 200)),
                generations=int(ga_payload.get('generations', 500)),
                crossover_rate=float(ga_payload.get('crossover_rate', 0.85)),
                mutation_rate=float(ga_payload.get('mutation_rate', 0.15)),
                elitism_count=int(ga_payload.get('elitism_count', 10)),
                local_search_iterations=int(ga_payload.get('local_search_iterations', 20)),
                csp_seed_fraction=float(ga_payload.get('csp_seed_fraction', 0.5)),
                local_search_fraction=float(ga_payload.get('local_search_fraction', 0.1)),
            ),
            weights=WeightSettings(
                weekly_load_penalty=float(weight_payload.get('weekly_load_penalty', 10.0)),
                daily_unevenness_penalty=float(weight_payload.get('daily_unevenness_penalty', 8.0)),
                teacher_gap_penalty=float(weight_payload.get('teacher_gap_penalty', 5.0)),
                class_gap_penalty=float(weight_payload.get('class_gap_penalty', 5.0)),
                teacher_preference_penalty=float(weight_payload.get('teacher_preference_penalty', 6.0)),
                hard_subject_position_penalty=float(weight_payload.get('hard_subject_position_penalty', 7.0)),
                doubled_subject_penalty=float(weight_payload.get('doubled_subject_penalty', 8.0)),
                sanpin_score_penalty=float(weight_payload.get('sanpin_score_penalty', 15.0)),
                peak_day_penalty=float(weight_payload.get('peak_day_penalty', 6.0)),
                class_window_penalty=float(weight_payload.get('class_window_penalty', 6.0)),
                teacher_window_penalty=float(weight_payload.get('teacher_window_penalty', 5.0)),
            ),
        ),
        school=SchoolSettings(
            name=str(school_payload.get('name', 'School')),
            start_time=_parse_time(school_payload.get('start_time', '08:30')),
            lesson_duration_minutes=int(school_payload.get('lesson_duration_minutes', 45)),
            breaks=tuple(int(item) for item in school_payload.get('breaks', [10, 20, 10, 20, 10])),
            shifts=int(school_payload.get('shifts', 1)),
            max_lessons_per_day=int(school_payload.get('max_lessons_per_day', 6)),
            multi_shift_break_minutes=int(school_payload.get('multi_shift_break_minutes', 30)),
            sanpin_compliance=bool(school_payload.get('sanpin_compliance', True)),
            has_extracurricular=bool(school_payload.get('has_extracurricular', False)),
            weekdays=tuple(_parse_weekday(item) for item in school_payload.get('weekdays', ['monday', 'tuesday', 'wednesday', 'thursday', 'friday'])),
        ),
        sanpin=SanPinSettings(
            enable_score_caps=bool(sanpin_payload.get('enable_score_caps', True)),
            primary_light_weekday=_parse_weekday(sanpin_payload.get('primary_light_weekday', 'wednesday')),
            middle_peak_weekdays=tuple(_parse_weekday(item) for item in sanpin_payload.get('middle_peak_weekdays', ['tuesday', 'wednesday'])),
            high_peak_weekdays=tuple(_parse_weekday(item) for item in sanpin_payload.get('high_peak_weekdays', ['tuesday', 'wednesday'])),
        ),
        source_path=resolved_path,
    )


def _resolve_config_path(config_path: str | Path | None) -> Path | None:
    if config_path is not None:
        return Path(config_path)

    env_path = os.environ.get('SCHEDULER_CONFIG_PATH')
    if env_path:
        return Path(env_path)

    return Path.cwd() / DEFAULT_CONFIG_RELATIVE_PATH


def _parse_time(value: Any) -> time:
    if isinstance(value, time):
        return value
    raw = str(value).strip()
    hours, minutes = raw.split(':', 1)
    return time(hour=int(hours), minute=int(minutes))


def _parse_weekday(value: Any) -> int:
    if isinstance(value, int):
        if value not in WEEKDAY_NAME_TO_NUMBER.values():
            raise ValueError(f'Unsupported weekday number: {value}')
        return value

    normalized = str(value).strip().casefold()
    if normalized not in WEEKDAY_NAME_TO_NUMBER:
        raise ValueError(f'Unsupported weekday name: {value}')
    return WEEKDAY_NAME_TO_NUMBER[normalized]
