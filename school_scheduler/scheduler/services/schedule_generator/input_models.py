from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


ROOM_TYPES = {'ordinary', 'lab', 'computer', 'gym', 'assembly', 'language'}


class TeacherPreferenceModel(BaseModel):
    avoid_first_lesson: bool = False
    avoid_last_lesson: bool = False
    preferred_weekdays: list[str] = Field(default_factory=list)
    avoid_weekdays: list[str] = Field(default_factory=list)
    preferred_lesson_numbers: list[int] = Field(default_factory=list)
    avoid_lesson_numbers: list[int] = Field(default_factory=list)

    @field_validator('preferred_weekdays', 'avoid_weekdays')
    @classmethod
    def _normalize_weekdays(cls, value: list[str]) -> list[str]:
        return [item.strip().casefold() for item in value if str(item).strip()]


class TeacherInputModel(BaseModel):
    full_name: str
    subjects: list[str] = Field(default_factory=list)
    max_weekly_load: int = Field(ge=1)
    max_lessons_per_day: int = Field(default=6, ge=1)
    unavailable_slots: list[str] = Field(default_factory=list)
    preferences: TeacherPreferenceModel = Field(default_factory=TeacherPreferenceModel)

    @field_validator('subjects', mode='before')
    @classmethod
    def _normalize_subjects(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(',') if item.strip()]
        return [str(item).strip() for item in value if str(item).strip()]

    @field_validator('unavailable_slots', mode='before')
    @classmethod
    def _normalize_slots(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(',') if item.strip()]
        return [str(item).strip() for item in value if str(item).strip()]


class SubjectInputModel(BaseModel):
    name: str
    difficulty: int | None = Field(default=None, ge=1, le=20)
    requires_special_room: bool = False
    required_room_type: str = 'ordinary'
    max_lessons_per_day: int = Field(default=2, ge=1, le=4)
    allows_double_lesson: bool = False

    @field_validator('required_room_type')
    @classmethod
    def _validate_room_type(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in ROOM_TYPES:
            raise ValueError(f'Unsupported room type: {value}')
        return normalized


class ClassInputModel(BaseModel):
    name: str
    grade: int = Field(ge=1, le=11)
    students_count: int = Field(ge=1)
    parallel: str = 'A'
    weekly_subject_hours: dict[str, int] = Field(default_factory=dict)

    @field_validator('weekly_subject_hours', mode='before')
    @classmethod
    def _normalize_hours(cls, value: Any) -> dict[str, int]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return {str(key).strip(): int(hours) for key, hours in value.items() if str(key).strip()}
        raise ValueError('weekly_subject_hours must be a mapping subject -> hours')


class ClassroomInputModel(BaseModel):
    name: str
    capacity: int = Field(ge=1)
    room_type: str = 'ordinary'
    available_slots: list[str] = Field(default_factory=list)

    @field_validator('room_type')
    @classmethod
    def _normalize_room_type(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in ROOM_TYPES:
            raise ValueError(f'Unsupported room type: {value}')
        return normalized


class SchoolMetaInputModel(BaseModel):
    name: str
    shifts: int = Field(default=1, ge=1, le=2)
    start_time: time = time(hour=8, minute=30)
    lesson_duration_minutes: int = Field(default=45, ge=35, le=60)
    has_extracurricular: bool = False

    @field_validator('start_time', mode='before')
    @classmethod
    def _parse_start_time(cls, value: Any) -> time:
        if isinstance(value, time):
            return value
        raw = str(value).strip()
        hours, minutes = raw.split(':', 1)
        return time(hour=int(hours), minute=int(minutes))


class SchoolInputModel(BaseModel):
    school: SchoolMetaInputModel
    teachers: list[TeacherInputModel]
    classes: list[ClassInputModel]
    subjects: list[SubjectInputModel]
    classrooms: list[ClassroomInputModel]

    @model_validator(mode='after')
    def _validate_cross_refs(self) -> 'SchoolInputModel':
        subject_names = {subject.name for subject in self.subjects}
        teacher_subjects = {subject for teacher in self.teachers for subject in teacher.subjects}

        missing_teacher_subjects = sorted(teacher_subjects - subject_names)
        if missing_teacher_subjects:
            raise ValueError(
                f'Teacher qualifications refer to unknown subjects: {", ".join(missing_teacher_subjects)}'
            )

        class_subjects = {
            subject
            for class_item in self.classes
            for subject, hours in class_item.weekly_subject_hours.items()
            if hours > 0
        }
        missing_class_subjects = sorted(class_subjects - subject_names)
        if missing_class_subjects:
            raise ValueError(
                f'Class study plan refers to unknown subjects: {", ".join(missing_class_subjects)}'
            )
        return self


def load_school_input_from_yaml(path: str | Path) -> SchoolInputModel:
    with Path(path).open('r', encoding='utf-8') as file_obj:
        payload = yaml.safe_load(file_obj) or {}
    return SchoolInputModel.model_validate(payload)


def load_school_input_from_excel(path: str | Path) -> SchoolInputModel:
    workbook = pd.read_excel(path, sheet_name=None)
    school_sheet = workbook.get('school')
    if school_sheet is None or school_sheet.empty:
        raise ValueError('Excel workbook must contain a non-empty "school" sheet')

    school_record = school_sheet.iloc[0].dropna().to_dict()
    subjects = _records_from_sheet(workbook, 'subjects')
    teachers = _records_from_sheet(workbook, 'teachers')
    classes = _records_from_sheet(workbook, 'classes')
    classrooms = _records_from_sheet(workbook, 'classrooms')

    if 'class_subject_hours' in workbook:
        hour_rows = workbook['class_subject_hours'].fillna('').to_dict(orient='records')
        hours_by_class: dict[str, dict[str, int]] = {}
        for row in hour_rows:
            class_name = str(row.get('class') or '').strip()
            subject_name = str(row.get('subject') or '').strip()
            hours = int(row.get('hours') or 0)
            if not class_name or not subject_name:
                continue
            hours_by_class.setdefault(class_name, {})[subject_name] = hours
        for class_record in classes:
            class_record['weekly_subject_hours'] = hours_by_class.get(class_record['name'], {})

    return SchoolInputModel.model_validate(
        {
            'school': school_record,
            'subjects': subjects,
            'teachers': teachers,
            'classes': classes,
            'classrooms': classrooms,
        }
    )


def _records_from_sheet(workbook: dict[str, pd.DataFrame], sheet_name: str) -> list[dict[str, Any]]:
    sheet = workbook.get(sheet_name)
    if sheet is None or sheet.empty:
        return []
    records: list[dict[str, Any]] = []
    for row in sheet.fillna('').to_dict(orient='records'):
        clean_row = {str(key).strip(): value for key, value in row.items() if str(key).strip()}
        records.append(clean_row)
    return records
