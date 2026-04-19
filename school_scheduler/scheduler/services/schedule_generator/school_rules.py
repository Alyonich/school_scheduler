from __future__ import annotations

from dataclasses import dataclass

from .sanpin_validator import is_hard_subject, is_pe_subject, normalize_text


PRIMARY_GRADES = {1, 2, 3, 4}
MIDDLE_GRADES = {5, 6, 7, 8, 9}


@dataclass(frozen=True)
class SanPinLimits:
    daily_max: int
    weekly_max: int
    pe_bonus_daily: int = 0


SANPIN_LIMITS_5_DAY: dict[int, SanPinLimits] = {
    1: SanPinLimits(daily_max=4, weekly_max=21, pe_bonus_daily=1),
    2: SanPinLimits(daily_max=5, weekly_max=23, pe_bonus_daily=1),
    3: SanPinLimits(daily_max=5, weekly_max=23, pe_bonus_daily=1),
    4: SanPinLimits(daily_max=5, weekly_max=23, pe_bonus_daily=1),
    5: SanPinLimits(daily_max=6, weekly_max=29),
    6: SanPinLimits(daily_max=6, weekly_max=30),
    7: SanPinLimits(daily_max=7, weekly_max=32),
    8: SanPinLimits(daily_max=7, weekly_max=33),
    9: SanPinLimits(daily_max=7, weekly_max=33),
    10: SanPinLimits(daily_max=7, weekly_max=34),
    11: SanPinLimits(daily_max=7, weekly_max=34),
}


LIGHT_KEYWORDS = (
    'музык',
    'music',
    'изо',
    'рисован',
    'art',
    'технолог',
    'technology',
    'физкульт',
    'physical education',
    'pe',
    'труд',
)

HUMANITIES_KEYWORDS = (
    'литератур',
    'literature',
    'истор',
    'history',
    'обществ',
    'social',
    'географ',
    'geography',
    'русск',
    'language',
    'англ',
    'english',
    'иностр',
)

STEM_KEYWORDS = (
    'матем',
    'math',
    'алгебр',
    'геометр',
    'физик',
    'physics',
    'хим',
    'chem',
    'биолог',
    'biology',
    'информ',
    'computer',
    'программ',
)

DOUBLE_ALLOWED_KEYWORDS = (
    'лаборатор',
    'практикум',
    'контроль',
    'технолог',
    'труд',
    'lab',
)


def grade_limits(grade: int) -> SanPinLimits:
    if grade < 1:
        return SANPIN_LIMITS_5_DAY[1]
    if grade > 11:
        return SANPIN_LIMITS_5_DAY[11]
    return SANPIN_LIMITS_5_DAY[grade]


def is_primary_grade(grade: int) -> bool:
    return grade in PRIMARY_GRADES


def is_middle_grade(grade: int) -> bool:
    return grade in MIDDLE_GRADES


def alternation_group(subject_name: str, grade: int) -> str:
    normalized = normalize_text(subject_name)

    if is_primary_grade(grade):
        if is_hard_subject(subject_name, grade):
            return 'hard'
        if any(keyword in normalized for keyword in LIGHT_KEYWORDS):
            return 'light'
        return 'neutral'

    if any(keyword in normalized for keyword in STEM_KEYWORDS):
        return 'stem'
    if any(keyword in normalized for keyword in HUMANITIES_KEYWORDS):
        return 'humanities'
    return 'neutral'


def allows_double_lesson(grade: int, subject_name: str, required_room_type: str) -> bool:
    normalized = normalize_text(subject_name)
    pe_like = is_pe_subject(subject_name)

    if is_primary_grade(grade):
        return pe_like

    if is_middle_grade(grade):
        if pe_like or required_room_type == 'lab':
            return True
        return any(keyword in normalized for keyword in DOUBLE_ALLOWED_KEYWORDS)

    return True
