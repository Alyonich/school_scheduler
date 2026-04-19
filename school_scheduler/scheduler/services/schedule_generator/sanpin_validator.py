from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import time
from typing import Iterable

from .configuration import SanPinSettings, SchoolSettings


OFFICIAL_DAILY_LIMITS_5_DAY = {
    1: 4,
    2: 5,
    3: 5,
    4: 5,
    5: 6,
    6: 6,
    7: 7,
    8: 7,
    9: 7,
    10: 7,
    11: 7,
}

OFFICIAL_DAILY_LIMITS_6_DAY = {
    1: 4,
    2: 6,
    3: 6,
    4: 6,
    5: 6,
    6: 6,
    7: 7,
    8: 7,
    9: 7,
    10: 7,
    11: 7,
}

OFFICIAL_WEEKLY_LIMITS_5_DAY = {
    1: 21,
    2: 23,
    3: 23,
    4: 23,
    5: 29,
    6: 30,
    7: 32,
    8: 33,
    9: 33,
    10: 34,
    11: 34,
}

OFFICIAL_WEEKLY_LIMITS_6_DAY = {
    1: 21,
    2: 26,
    3: 26,
    4: 26,
    5: 32,
    6: 33,
    7: 35,
    8: 36,
    9: 36,
    10: 37,
    11: 37,
}

# These score caps are operational defaults for automated optimisation.
# The official SanPiN tables provide the difficulty scales themselves; exact
# per-day and per-week score ceilings are typically formalised by a school's
# local scheduling policy, so these defaults are intentionally configurable.
DEFAULT_DAILY_SCORE_LIMITS = {
    1: 22,
    2: 25,
    3: 25,
    4: 26,
    5: 36,
    6: 40,
    7: 46,
    8: 50,
    9: 54,
    10: 58,
    11: 58,
}

DEFAULT_WEEKLY_SCORE_LIMITS = {
    grade: DEFAULT_DAILY_SCORE_LIMITS[grade] * (5 if grade == 1 else 5)
    for grade in range(1, 12)
}

PRIMARY_DIFFICULTY_TABLE = (
    (('матем', 'math'), 8),
    (('русск', 'родн', 'language', 'англ', 'english', 'иностр'), 7),
    (('окружа', 'природовед', 'информ'), 6),
    (('литератур', 'чтени'), 5),
    (('истор',), 4),
    (('музык', 'изо', 'рисован', 'art'), 3),
    (('технолог', 'труд'), 2),
    (('физкульт', 'physical education', 'pe', 'спорт'), 1),
)

MIDDLE_DIFFICULTY_TABLE = (
    (('физик', 'physics'), {7: 8, 8: 9, 9: 13}),
    (('хим', 'chem'), {8: 10, 9: 12}),
    (('истор', 'history'), {5: 5, 6: 8, 7: 6, 8: 8, 9: 10}),
    (('англ', 'english', 'иностр', 'foreign language', 'немец', 'language'), {5: 9, 6: 11, 7: 10, 8: 8, 9: 9}),
    (('матем', 'math'), {5: 10, 6: 13}),
    (('геометр', 'geometry'), {7: 12, 8: 10, 9: 8}),
    (('алгебр', 'algebra'), {7: 10, 8: 9, 9: 7}),
    (('окружа', 'природовед'), {5: 7, 6: 8}),
    (('биолог', 'biology'), {5: 10, 6: 8, 7: 7, 8: 7, 9: 7}),
    (('литератур', 'literature'), {5: 4, 6: 6, 7: 4, 8: 4, 9: 7}),
    (('информ', 'ict', 'computer'), {5: 4, 6: 10, 7: 4, 8: 7, 9: 7}),
    (('русск', 'родн', 'russian'), {5: 8, 6: 12, 7: 11, 8: 7, 9: 6}),
    (('географ', 'geography'), {6: 7, 7: 6, 8: 6, 9: 5}),
    (('изо', 'искусств', 'art'), {5: 3, 6: 3, 7: 1}),
    (('мхк',), {7: 8, 8: 5, 9: 5}),
    (('музык', 'music'), {5: 2, 6: 1, 7: 1, 8: 1}),
    (('обществ', 'эконом', 'право', 'social'), {5: 6, 6: 9, 7: 9, 8: 5, 9: 5}),
    (('технолог', 'труд'), {5: 4, 6: 3, 7: 2, 8: 1, 9: 4}),
    (('черчен',), {8: 5, 9: 4}),
    (('обж', 'безопасн'), {5: 1, 6: 2, 7: 3, 8: 3, 9: 3}),
    (('физкульт', 'physical education', 'pe', 'спорт'), {5: 3, 6: 4, 7: 2, 8: 2, 9: 2}),
)

HIGH_DIFFICULTY_TABLE = (
    (('физик', 'physics'), 12),
    (('геометр', 'geometry', 'хим', 'chem'), 11),
    (('алгебр', 'матем', 'math'), 10),
    (('русск', 'родн', 'russian'), 9),
    (('литератур', 'иностр', 'english', 'language'), 8),
    (('биолог', 'biology'), 7),
    (('информ', 'ict', 'econom'), 6),
    (('истор', 'обществ', 'мхк', 'social'), 5),
    (('астроном',), 4),
    (('географ', 'эколог'), 3),
    (('обж', 'краевед'), 2),
    (('физкульт', 'physical education', 'pe', 'спорт'), 1),
)


@dataclass(frozen=True)
class LessonLoadEntry:
    class_id: int
    class_grade: int
    subject_name: str
    weekday: int
    lesson_number: int
    difficulty_score: int
    is_pe: bool


@dataclass(frozen=True)
class TimeGridEntry:
    weekday: int
    lesson_number: int
    start_time: time
    end_time: time


@dataclass
class SanPinValidationResult:
    diagnostics: dict[str, int]
    warnings: list[str]
    daily_scores: dict[tuple[int, int], int]
    weekly_scores: dict[int, int]


def normalize_text(value: str) -> str:
    return value.casefold().replace('\u0451', '\u0435').strip()


class SanPinValidator:
    def __init__(self, school_settings: SchoolSettings, sanpin_settings: SanPinSettings) -> None:
        self.school_settings = school_settings
        self.sanpin_settings = sanpin_settings

    def daily_lesson_limit(self, grade: int, study_days: int | None = None, pe_bonus: bool = False) -> int:
        days = study_days or len(self.school_settings.weekdays)
        limit_table = OFFICIAL_DAILY_LIMITS_6_DAY if days >= 6 else OFFICIAL_DAILY_LIMITS_5_DAY
        limit = limit_table.get(_normalize_grade(grade), limit_table[11])
        if grade <= 4 and pe_bonus:
            return limit + 1
        return limit

    def weekly_lesson_limit(self, grade: int, study_days: int | None = None) -> int:
        days = study_days or len(self.school_settings.weekdays)
        limit_table = OFFICIAL_WEEKLY_LIMITS_6_DAY if days >= 6 else OFFICIAL_WEEKLY_LIMITS_5_DAY
        return limit_table.get(_normalize_grade(grade), limit_table[11])

    def daily_score_limit(self, grade: int) -> int:
        return DEFAULT_DAILY_SCORE_LIMITS.get(_normalize_grade(grade), DEFAULT_DAILY_SCORE_LIMITS[11])

    def weekly_score_limit(self, grade: int) -> int:
        return DEFAULT_WEEKLY_SCORE_LIMITS.get(_normalize_grade(grade), DEFAULT_WEEKLY_SCORE_LIMITS[11])

    def difficulty_score(self, subject_name: str, grade: int, explicit_score: int | None = None) -> int:
        if explicit_score is not None:
            return explicit_score

        normalized_grade = _normalize_grade(grade)
        normalized_name = normalize_text(subject_name)
        if normalized_grade <= 4:
            return _lookup_primary_score(normalized_name)
        if normalized_grade <= 9:
            return _lookup_middle_score(normalized_name, normalized_grade)
        return _lookup_high_score(normalized_name)

    def validate_load_distribution(self, lessons: Iterable[LessonLoadEntry]) -> SanPinValidationResult:
        diagnostics: dict[str, int] = defaultdict(int)
        warnings: list[str] = []
        daily_scores: dict[tuple[int, int], int] = defaultdict(int)
        weekly_scores: dict[int, int] = defaultdict(int)
        daily_counts: dict[tuple[int, int], int] = defaultdict(int)
        grade_by_class: dict[int, int] = {}

        for lesson in lessons:
            day_key = (lesson.class_id, lesson.weekday)
            daily_scores[day_key] += lesson.difficulty_score
            weekly_scores[lesson.class_id] += lesson.difficulty_score
            daily_counts[day_key] += 1
            grade_by_class[lesson.class_id] = lesson.class_grade

            if lesson.difficulty_score >= 8 and lesson.lesson_number in {1, self.school_settings.max_lessons_per_day}:
                diagnostics['hard_subject_position_violations'] += 1

        for class_id, grade in grade_by_class.items():
            study_days = len(self.school_settings.weekdays)
            weekly_limit = self.weekly_score_limit(grade)
            if self.sanpin_settings.enable_score_caps and weekly_scores[class_id] > weekly_limit:
                diagnostics['sanpin_weekly_score_overload'] += weekly_scores[class_id] - weekly_limit

            daily_items = {
                weekday: daily_scores.get((class_id, weekday), 0)
                for weekday in self.school_settings.weekdays
            }
            for weekday, score in daily_items.items():
                if self.sanpin_settings.enable_score_caps and score > self.daily_score_limit(grade):
                    diagnostics['sanpin_daily_score_overload'] += score - self.daily_score_limit(grade)

                pe_bonus = False
                lesson_count = daily_counts.get((class_id, weekday), 0)
                if lesson_count > self.daily_lesson_limit(grade, study_days=study_days, pe_bonus=pe_bonus):
                    diagnostics['sanpin_daily_lesson_overload'] += (
                        lesson_count - self.daily_lesson_limit(grade, study_days=study_days, pe_bonus=pe_bonus)
                    )

            if grade <= 4:
                light_weekday = self.sanpin_settings.primary_light_weekday
                light_day_score = daily_items.get(light_weekday, 0)
                heavy_day_score = max(daily_items.values(), default=0)
                if light_day_score >= heavy_day_score and heavy_day_score > 0:
                    diagnostics['sanpin_primary_light_day_violation'] += 1
            else:
                peak_days = (
                    self.sanpin_settings.middle_peak_weekdays
                    if grade <= 9
                    else self.sanpin_settings.high_peak_weekdays
                )
                peak_score = max(daily_items.get(day, 0) for day in peak_days)
                monday_score = daily_items.get(1, 0)
                friday_score = daily_items.get(5, 0)
                if peak_score < max(monday_score, friday_score):
                    diagnostics['sanpin_peak_distribution_violation'] += 1

        if diagnostics.get('sanpin_daily_score_overload'):
            warnings.append('Есть дни с превышением допустимой суммарной трудности по СанПиН.')
        if diagnostics.get('sanpin_weekly_score_overload'):
            warnings.append('Есть классы с превышением недельной суммарной трудности по СанПиН.')
        if diagnostics.get('sanpin_peak_distribution_violation'):
            warnings.append('Пиковая учебная нагрузка смещена с вторника/среды на менее предпочтительные дни.')
        if diagnostics.get('sanpin_primary_light_day_violation'):
            warnings.append('Для начальной школы не выдержан облегченный учебный день в середине недели.')

        return SanPinValidationResult(
            diagnostics=dict(diagnostics),
            warnings=warnings,
            daily_scores=dict(daily_scores),
            weekly_scores=dict(weekly_scores),
        )

    def validate_time_grid(self, slots: Iterable[TimeGridEntry]) -> list[str]:
        warnings: list[str] = []
        ordered_slots = sorted(slots, key=lambda item: (item.weekday, item.lesson_number))
        if not ordered_slots:
            return ['Не найдены временные слоты для учебной недели.']

        earliest_start = min(slot.start_time for slot in ordered_slots)
        if earliest_start < time(hour=8, minute=0):
            warnings.append('Учебные занятия начинаются раньше 08:00, что противоречит СанПиН.')

        grouped: dict[int, list[TimeGridEntry]] = defaultdict(list)
        for slot in ordered_slots:
            grouped[slot.weekday].append(slot)

        if self.school_settings.shifts > 1:
            required_break = self.school_settings.multi_shift_break_minutes
            longest_gap = 0
            for day_slots in grouped.values():
                day_slots.sort(key=lambda item: item.lesson_number)
                for previous, current in zip(day_slots, day_slots[1:]):
                    gap = _minutes_between(previous.end_time, current.start_time)
                    longest_gap = max(longest_gap, gap)
            if longest_gap < required_break:
                warnings.append(
                    'В сетке звонков не найден перерыв между сменами не менее '
                    f'{required_break} минут.'
                )
        return warnings


def is_pe_subject(subject_name: str) -> bool:
    normalized = normalize_text(subject_name)
    return any(marker in normalized for marker in ('физкульт', 'physical education', 'pe', 'спорт'))


def is_hard_subject(subject_name: str, grade: int) -> bool:
    normalized_name = normalize_text(subject_name)
    if grade <= 4:
        return _lookup_primary_score(normalized_name) >= 7
    if grade <= 9:
        return _lookup_middle_score(normalized_name, grade) >= 8
    return _lookup_high_score(normalized_name) >= 8


def _normalize_grade(grade: int) -> int:
    if grade < 1:
        return 1
    if grade > 11:
        return 11
    return grade


def _lookup_primary_score(normalized_name: str) -> int:
    for aliases, score in PRIMARY_DIFFICULTY_TABLE:
        if any(alias in normalized_name for alias in aliases):
            return score
    return 4


def _lookup_middle_score(normalized_name: str, grade: int) -> int:
    for aliases, score_map in MIDDLE_DIFFICULTY_TABLE:
        if any(alias in normalized_name for alias in aliases):
            return score_map.get(grade) or max(score_map.values())
    return 5


def _lookup_high_score(normalized_name: str) -> int:
    for aliases, score in HIGH_DIFFICULTY_TABLE:
        if any(alias in normalized_name for alias in aliases):
            return score
    return 5


def _minutes_between(start: time, end: time) -> int:
    return (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
