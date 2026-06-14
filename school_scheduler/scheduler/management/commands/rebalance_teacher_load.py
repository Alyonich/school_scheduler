from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from scheduler.management.commands.seed_demo_data import TEACHER_SPEC
from scheduler.models import ClassSubject, Teacher, TeachingAssignment


ADDITIONAL_CAPABILITIES: dict[str, tuple[str, ...]] = {
    # Adjacent competencies for demo balancing (to avoid hard concentration on 1-2 teachers).
    "ext_teacher_01": ("Информатика",),
    "ext_teacher_02": ("Информатика",),
    "ext_teacher_03": ("Информатика",),
    "ext_teacher_04": ("География",),
    "ext_teacher_05": ("География",),
    "ext_teacher_06": ("История", "Обществознание"),
    "ext_teacher_07": ("История", "Обществознание"),
    "ext_teacher_10": ("ОБЗР",),
    "ext_teacher_11": ("ОБЗР",),
    "ext_teacher_13": ("Физика",),
    "zaitsev": ("Технология",),
}

EXT_TEACHER_BASE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "ext_teacher_01": ("Алгебра", "Геометрия"),
    "ext_teacher_02": ("Алгебра", "Геометрия"),
    "ext_teacher_03": ("Алгебра", "Геометрия"),
    "ext_teacher_04": ("Русский язык", "Литература"),
    "ext_teacher_05": ("Русский язык", "Литература"),
    "ext_teacher_06": ("Литература",),
    "ext_teacher_07": ("Литература",),
    "ext_teacher_08": ("Английский язык",),
    "ext_teacher_09": ("Английский язык",),
    "ext_teacher_10": ("Физическая культура",),
    "ext_teacher_11": ("Физическая культура",),
    "ext_teacher_12": ("История", "Обществознание", "ОБЗР"),
    "ext_teacher_13": ("Биология", "География", "ОБЗР", "Химия"),
    "ext_teacher_14": ("Биология", "География", "Информатика", "Физика"),
}


@dataclass(slots=True)
class _TeacherState:
    hours: int = 0
    assignments: int = 0
    unique_classes: set[int] | None = None

    def __post_init__(self) -> None:
        if self.unique_classes is None:
            self.unique_classes = set()


class Command(BaseCommand):
    help = (
        "Rebalance TeachingAssignment load across teachers. "
        "Uses canonical subject capabilities (seed profiles + extended demo overrides)."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Build plan and print stats without saving changes.",
        )

    def handle(self, *args, **options) -> None:
        dry_run = bool(options.get("dry_run"))

        teachers = list(Teacher.objects.select_related("user").all())
        if not teachers:
            raise CommandError("No teachers found.")

        class_subjects = list(
            ClassSubject.objects.select_related("class_obj", "subject").all()
        )
        if not class_subjects:
            raise CommandError("No ClassSubject rows found.")

        current_assignments = list(
            TeachingAssignment.objects.select_related("teacher", "class_obj", "subject").all()
        )
        if not current_assignments:
            raise CommandError("No TeachingAssignment rows found.")

        # Subject specialization graph:
        # canonical capabilities first (seed + ext profiles), current assignments only as fallback.
        teacher_subjects: dict[int, set[int]] = defaultdict(set)
        subject_candidates: dict[int, list[Teacher]] = defaultdict(list)
        additional_capability_pairs: set[tuple[int, int]] = set()
        subject_id_by_name = {
            row.subject.name: row.subject_id
            for row in class_subjects
        }

        current_subjects_by_username: dict[str, set[int]] = defaultdict(set)
        teacher_by_id = {teacher.id: teacher for teacher in teachers}
        for assignment in current_assignments:
            teacher_obj = teacher_by_id.get(assignment.teacher_id)
            if teacher_obj:
                current_subjects_by_username[teacher_obj.user.username].add(assignment.subject_id)

        # Canonical capabilities from seed teachers.
        seed_capabilities_by_username: dict[str, set[int]] = defaultdict(set)
        for username, _full_name, _qualification, _workload, _daily_limit, subject_names in TEACHER_SPEC:
            for subject_name in subject_names:
                subject_id = subject_id_by_name.get(subject_name)
                if subject_id:
                    seed_capabilities_by_username[username].add(subject_id)

        ext_base_capabilities_by_username: dict[str, set[int]] = defaultdict(set)
        for username, subject_names in EXT_TEACHER_BASE_CAPABILITIES.items():
            for subject_name in subject_names:
                subject_id = subject_id_by_name.get(subject_name)
                if subject_id:
                    ext_base_capabilities_by_username[username].add(subject_id)

        for teacher in teachers:
            username = teacher.user.username
            canonical_subjects = (
                seed_capabilities_by_username.get(username, set())
                | ext_base_capabilities_by_username.get(username, set())
            )
            if canonical_subjects:
                teacher_subjects[teacher.id].update(canonical_subjects)
            else:
                teacher_subjects[teacher.id].update(current_subjects_by_username.get(username, set()))

        for teacher in teachers:
            for subject_name in ADDITIONAL_CAPABILITIES.get(teacher.user.username, ()):
                subject_id = subject_id_by_name.get(subject_name)
                if subject_id:
                    if subject_id not in teacher_subjects[teacher.id]:
                        additional_capability_pairs.add((teacher.id, subject_id))
                    teacher_subjects[teacher.id].add(subject_id)
        for teacher in teachers:
            for subject_id in teacher_subjects.get(teacher.id, set()):
                subject_candidates[subject_id].append(teacher)

        missing_subject_ids = sorted(
            {
                cs.subject_id
                for cs in class_subjects
                if not subject_candidates.get(cs.subject_id)
            }
        )
        if missing_subject_ids:
            raise CommandError(
                "Cannot rebalance: there are subjects without candidate teachers in current assignments. "
                f"subject_ids={missing_subject_ids}"
            )

        total_hours_by_subject: dict[int, int] = defaultdict(int)
        total_classes_by_subject: dict[int, int] = defaultdict(int)
        for cs in class_subjects:
            total_hours_by_subject[cs.subject_id] += cs.weekly_hours
            total_classes_by_subject[cs.subject_id] += 1

        # Higher scarcity first, then harder/longer rows.
        scarcity_by_subject: dict[int, float] = {}
        for subject_id, required in total_hours_by_subject.items():
            capacity = sum(t.workload_hours for t in subject_candidates[subject_id])
            scarcity_by_subject[subject_id] = (required / capacity) if capacity else 9999.0

        class_subjects_sorted = sorted(
            class_subjects,
            key=lambda cs: (
                -scarcity_by_subject[cs.subject_id],
                len(subject_candidates[cs.subject_id]),
                -cs.weekly_hours,
                cs.class_obj.grade,
                cs.class_obj.parallel,
                cs.class_obj.name,
            ),
        )

        state_by_teacher: dict[int, _TeacherState] = {teacher.id: _TeacherState() for teacher in teachers}
        subject_hours_by_teacher: dict[tuple[int, int], int] = defaultdict(int)
        subject_classes_by_teacher: dict[tuple[int, int], int] = defaultdict(int)

        new_plan: list[tuple[Teacher, ClassSubject, int]] = []
        unresolved: list[str] = []

        for cs in class_subjects_sorted:
            candidates = subject_candidates[cs.subject_id]
            target_hours = total_hours_by_subject[cs.subject_id] / max(len(candidates), 1)
            target_classes = total_classes_by_subject[cs.subject_id] / max(len(candidates), 1)

            best_teacher: Teacher | None = None
            best_score: float | None = None

            for teacher in candidates:
                state = state_by_teacher[teacher.id]
                projected_hours = state.hours + cs.weekly_hours
                if projected_hours > teacher.workload_hours:
                    continue

                key = (cs.subject_id, teacher.id)
                projected_subject_hours = subject_hours_by_teacher[key] + cs.weekly_hours
                projected_subject_classes = subject_classes_by_teacher[key] + 1

                load_ratio = projected_hours / max(teacher.workload_hours, 1)
                hour_dev = abs(projected_subject_hours - target_hours) / max(target_hours, 1.0)
                class_dev = abs(projected_subject_classes - target_classes) / max(target_classes, 1.0)
                unique_classes_after = len(state.unique_classes | {cs.class_obj_id})

                # Lower is better.
                score = (
                    (3.2 * load_ratio)
                    + (1.6 * hour_dev)
                    + (1.2 * class_dev)
                    + (0.22 * unique_classes_after)
                )
                if (teacher.id, cs.subject_id) in additional_capability_pairs:
                    score += 0.45

                if best_score is None or score < best_score:
                    best_score = score
                    best_teacher = teacher

            if best_teacher is None:
                unresolved.append(
                    f"{cs.class_obj.name} / {cs.subject.name} ({cs.weekly_hours}h)"
                )
                continue

            new_plan.append((best_teacher, cs, cs.weekly_hours))
            best_state = state_by_teacher[best_teacher.id]
            best_state.hours += cs.weekly_hours
            best_state.assignments += 1
            best_state.unique_classes.add(cs.class_obj_id)
            best_key = (cs.subject_id, best_teacher.id)
            subject_hours_by_teacher[best_key] += cs.weekly_hours
            subject_classes_by_teacher[best_key] += 1

        if unresolved:
            example = ", ".join(unresolved[:8])
            raise CommandError(
                "Failed to build a complete plan with workload constraints. "
                f"Unresolved rows: {len(unresolved)}. Examples: {example}"
            )

        old_primary = self._build_primary_owner_map(current_assignments)
        changed_pairs = sum(
            1
            for teacher, cs, _hours in new_plan
            if old_primary.get((cs.class_obj_id, cs.subject_id)) != teacher.id
        )

        before_rows = self._collect_teacher_summary(teachers)
        after_rows = self._collect_teacher_summary_from_plan(teachers, new_plan)

        self.stdout.write(self.style.NOTICE("Teacher load summary (before):"))
        for row in before_rows:
            self.stdout.write(row)
        self.stdout.write("")
        self.stdout.write(self.style.NOTICE("Teacher load summary (planned after):"))
        for row in after_rows:
            self.stdout.write(row)
        self.stdout.write("")
        self.stdout.write(
            self.style.NOTICE(
                f"Planned assignments: {len(new_plan)}. Changed class-subject owners: {changed_pairs}."
            )
        )

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry-run mode: no database changes were applied."))
            return

        with transaction.atomic():
            TeachingAssignment.objects.all().delete()
            TeachingAssignment.objects.bulk_create(
                [
                    TeachingAssignment(
                        teacher=teacher,
                        subject=cs.subject,
                        class_obj=cs.class_obj,
                        hours_per_week=hours,
                    )
                    for teacher, cs, hours in new_plan
                ],
                batch_size=500,
            )

        self.stdout.write(self.style.SUCCESS("TeachingAssignment successfully rebalanced."))

    @staticmethod
    def _build_primary_owner_map(
        assignments: Iterable[TeachingAssignment],
    ) -> dict[tuple[int, int], int]:
        # If a class-subject was split, "owner" is teacher with max hours in that pair.
        by_pair: dict[tuple[int, int], tuple[int, int]] = {}
        for assignment in assignments:
            pair = (assignment.class_obj_id, assignment.subject_id)
            current = by_pair.get(pair)
            payload = (assignment.teacher_id, assignment.hours_per_week or 0)
            if current is None or payload[1] > current[1]:
                by_pair[pair] = payload
        return {pair: payload[0] for pair, payload in by_pair.items()}

    @staticmethod
    def _collect_teacher_summary(teachers: list[Teacher]) -> list[str]:
        rows: list[tuple[float, str]] = []
        for teacher in teachers:
            assignments = TeachingAssignment.objects.filter(teacher=teacher)
            hours = sum(assignments.values_list("hours_per_week", flat=True))
            unique_classes = assignments.values("class_obj_id").distinct().count()
            ratio = (hours / teacher.workload_hours) if teacher.workload_hours else 0.0
            label = teacher.user.full_name or teacher.user.username
            row = (
                f"{label}: {hours}/{teacher.workload_hours}h ({ratio:.2%}), "
                f"classes={unique_classes}, rows={assignments.count()}"
            )
            rows.append((ratio, row))
        rows.sort(reverse=True, key=lambda item: item[0])
        return [row for _ratio, row in rows]

    @staticmethod
    def _collect_teacher_summary_from_plan(
        teachers: list[Teacher],
        plan: list[tuple[Teacher, ClassSubject, int]],
    ) -> list[str]:
        by_teacher_hours: dict[int, int] = defaultdict(int)
        by_teacher_rows: dict[int, int] = defaultdict(int)
        by_teacher_classes: dict[int, set[int]] = defaultdict(set)
        for teacher, cs, hours in plan:
            by_teacher_hours[teacher.id] += hours
            by_teacher_rows[teacher.id] += 1
            by_teacher_classes[teacher.id].add(cs.class_obj_id)

        rows: list[tuple[float, str]] = []
        for teacher in teachers:
            hours = by_teacher_hours.get(teacher.id, 0)
            unique_classes = len(by_teacher_classes.get(teacher.id, set()))
            ratio = (hours / teacher.workload_hours) if teacher.workload_hours else 0.0
            label = teacher.user.full_name or teacher.user.username
            row = (
                f"{label}: {hours}/{teacher.workload_hours}h ({ratio:.2%}), "
                f"classes={unique_classes}, rows={by_teacher_rows.get(teacher.id, 0)}"
            )
            rows.append((ratio, row))
        rows.sort(reverse=True, key=lambda item: item[0])
        return [row for _ratio, row in rows]
