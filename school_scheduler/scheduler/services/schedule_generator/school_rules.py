from dataclasses import dataclass


PRIMARY_GRADES = {1, 2, 3, 4}
MIDDLE_GRADES = {5, 6, 7, 8, 9}


@dataclass(frozen=True)
class SanPinLimits:
    daily_max: int
    weekly_max: int
    pe_bonus_daily: int = 0


SANPIN_LIMITS: dict[int, SanPinLimits] = {
    1: SanPinLimits(daily_max=4, weekly_max=21, pe_bonus_daily=1),
    2: SanPinLimits(daily_max=5, weekly_max=23, pe_bonus_daily=1),
    3: SanPinLimits(daily_max=5, weekly_max=23, pe_bonus_daily=1),
    4: SanPinLimits(daily_max=5, weekly_max=23, pe_bonus_daily=1),
    5: SanPinLimits(daily_max=6, weekly_max=29),
    6: SanPinLimits(daily_max=6, weekly_max=30),
    7: SanPinLimits(daily_max=7, weekly_max=32),
    8: SanPinLimits(daily_max=7, weekly_max=33),
    9: SanPinLimits(daily_max=7, weekly_max=33),
    10: SanPinLimits(daily_max=7, weekly_max=24),
    11: SanPinLimits(daily_max=7, weekly_max=24),
}


PRIMARY_HARD_KEYWORDS = (
    'матем',
    'math',
    'алгебр',
    'геометр',
    'русск',
    'язык',
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

HUMANITIES_KEYWORDS = (
    'литер',
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

PE_KEYWORDS = (
    'физкульт',
    'physical education',
    'pe',
    'спорт',
    'лыж',
    'плав',
)

DOUBLE_ALLOWED_KEYWORDS = (
    'лаборатор',
    'практикум',
    'контроль',
    'технолог',
    'труд',
    'lab',
)


def normalize_text(value: str) -> str:
    return value.casefold().replace('ё', 'е').strip()


def grade_limits(grade: int) -> SanPinLimits:
    if grade < 1:
        return SANPIN_LIMITS[1]
    if grade > 11:
        return SANPIN_LIMITS[11]
    return SANPIN_LIMITS[grade]


def is_primary_grade(grade: int) -> bool:
    return grade in PRIMARY_GRADES


def is_middle_grade(grade: int) -> bool:
    return grade in MIDDLE_GRADES


def is_pe_subject(subject_name: str) -> bool:
    normalized = normalize_text(subject_name)
    return any(keyword in normalized for keyword in PE_KEYWORDS)


def is_hard_subject(subject_name: str, grade: int) -> bool:
    normalized = normalize_text(subject_name)
    if is_primary_grade(grade):
        return any(keyword in normalized for keyword in PRIMARY_HARD_KEYWORDS)
    return any(keyword in normalized for keyword in STEM_KEYWORDS)


def alternation_group(subject_name: str, grade: int) -> str:
    normalized = normalize_text(subject_name)

    if is_primary_grade(grade):
        if any(keyword in normalized for keyword in PRIMARY_HARD_KEYWORDS):
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
    pe_like = is_pe_subject(normalized)

    if is_primary_grade(grade):
        return pe_like

    if is_middle_grade(grade):
        if pe_like or required_room_type == 'lab':
            return True
        return any(keyword in normalized for keyword in DOUBLE_ALLOWED_KEYWORDS)

    return True


# Re-declare keyword tables in ASCII-safe form to avoid locale/encoding issues.
PRIMARY_HARD_KEYWORDS = (
    '\u043c\u0430\u0442\u0435\u043c',
    'math',
    '\u0430\u043b\u0433\u0435\u0431\u0440',
    '\u0433\u0435\u043e\u043c\u0435\u0442\u0440',
    '\u0440\u0443\u0441\u0441\u043a',
    '\u044f\u0437\u044b\u043a',
    'language',
    '\u0430\u043d\u0433\u043b',
    'english',
    '\u0438\u043d\u043e\u0441\u0442\u0440',
)

STEM_KEYWORDS = (
    '\u043c\u0430\u0442\u0435\u043c',
    'math',
    '\u0430\u043b\u0433\u0435\u0431\u0440',
    '\u0433\u0435\u043e\u043c\u0435\u0442\u0440',
    '\u0444\u0438\u0437\u0438\u043a',
    'physics',
    '\u0445\u0438\u043c',
    'chem',
    '\u0431\u0438\u043e\u043b\u043e\u0433',
    'biology',
    '\u0438\u043d\u0444\u043e\u0440\u043c',
    'computer',
    '\u043f\u0440\u043e\u0433\u0440\u0430\u043c\u043c',
)

HUMANITIES_KEYWORDS = (
    '\u043b\u0438\u0442\u0435\u0440',
    'literature',
    '\u0438\u0441\u0442\u043e\u0440',
    'history',
    '\u043e\u0431\u0449\u0435\u0441\u0442\u0432',
    'social',
    '\u0433\u0435\u043e\u0433\u0440\u0430\u0444',
    'geography',
    '\u0440\u0443\u0441\u0441\u043a',
    'language',
    '\u0430\u043d\u0433\u043b',
    'english',
    '\u0438\u043d\u043e\u0441\u0442\u0440',
)

LIGHT_KEYWORDS = (
    '\u043c\u0443\u0437\u044b\u043a',
    'music',
    '\u0438\u0437\u043e',
    '\u0440\u0438\u0441\u043e\u0432\u0430\u043d',
    'art',
    '\u0442\u0435\u0445\u043d\u043e\u043b\u043e\u0433',
    'technology',
    '\u0444\u0438\u0437\u043a\u0443\u043b\u044c\u0442',
    'physical education',
    'pe',
    '\u0442\u0440\u0443\u0434',
)

PE_KEYWORDS = (
    '\u0444\u0438\u0437\u043a\u0443\u043b\u044c\u0442',
    'physical education',
    'pe',
    '\u0441\u043f\u043e\u0440\u0442',
    '\u043b\u044b\u0436',
    '\u043f\u043b\u0430\u0432',
)

DOUBLE_ALLOWED_KEYWORDS = (
    '\u043b\u0430\u0431\u043e\u0440\u0430\u0442\u043e\u0440',
    '\u043f\u0440\u0430\u043a\u0442\u0438\u043a\u0443\u043c',
    '\u043a\u043e\u043d\u0442\u0440\u043e\u043b\u044c',
    '\u0442\u0435\u0445\u043d\u043e\u043b\u043e\u0433',
    '\u0442\u0440\u0443\u0434',
    'lab',
)


def normalize_text(value: str) -> str:
    return value.casefold().replace('\u0451', '\u0435').strip()
