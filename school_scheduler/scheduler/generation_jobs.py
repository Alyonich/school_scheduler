from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from threading import Condition, Lock, Thread
from typing import Callable
from uuid import uuid4


FINAL_STATES = {'completed', 'failed', 'cancelled'}


@dataclass
class GenerationEvent:
    timestamp: str
    stage: str
    message: str
    progress_percent: int


@dataclass
class GenerationJob:
    job_id: str
    week_start: str
    class_ids: list[int]
    result_url: str
    revision: int = 0
    state: str = 'queued'
    stage: str = 'queued'
    stage_label: str = 'Подготовка'
    message: str = 'Готовим задачу генерации.'
    progress_percent: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec='seconds'))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec='seconds'))
    finished_at: str | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    created_lessons: int | None = None
    hard_penalty: int | None = None
    soft_penalty: int | None = None
    diagnostics: dict[str, int] = field(default_factory=dict)
    events: list[GenerationEvent] = field(default_factory=list)

    def to_payload(self) -> dict:
        payload = asdict(self)
        payload['events'] = [asdict(event) for event in self.events]
        return payload


class GenerationAlreadyRunningError(RuntimeError):
    def __init__(self, job_id: str) -> None:
        super().__init__(job_id)
        self.job_id = job_id


_jobs: dict[str, GenerationJob] = {}
_active_job_id: str | None = None
_jobs_lock = Lock()
_jobs_condition = Condition(_jobs_lock)


def start_generation_job(
    *,
    week_start,
    class_ids: list[int],
    result_url: str,
    worker: Callable[[str], None],
    run_inline: bool = False,
) -> GenerationJob:
    with _jobs_condition:
        active_job = _get_active_job_unlocked()
        if active_job is not None:
            raise GenerationAlreadyRunningError(active_job.job_id)

        job = GenerationJob(
            job_id=uuid4().hex,
            week_start=week_start.isoformat(),
            class_ids=list(class_ids),
            result_url=result_url,
        )
        job.events.append(
            GenerationEvent(
                timestamp=job.created_at,
                stage=job.stage,
                message=job.message,
                progress_percent=job.progress_percent,
            )
        )
        _jobs[job.job_id] = job
        global _active_job_id
        _active_job_id = job.job_id
        _jobs_condition.notify_all()

    if run_inline:
        worker(job.job_id)
    else:
        Thread(target=worker, args=(job.job_id,), daemon=True).start()
    return get_generation_job(job.job_id)


def get_generation_job(job_id: str) -> GenerationJob | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        return deepcopy(job)


def get_active_generation_job() -> GenerationJob | None:
    with _jobs_lock:
        active_job = _get_active_job_unlocked()
        if active_job is None:
            return None
        return deepcopy(active_job)


def wait_for_generation_job_update(
    job_id: str,
    *,
    known_revision: int,
    timeout_seconds: float = 25.0,
) -> GenerationJob | None:
    def has_update() -> bool:
        job = _jobs.get(job_id)
        if job is None:
            return True
        return job.revision > known_revision or job.state in FINAL_STATES

    with _jobs_condition:
        _jobs_condition.wait_for(has_update, timeout=timeout_seconds)
        job = _jobs.get(job_id)
        if job is None:
            return None
        return deepcopy(job)


def update_generation_job(
    job_id: str,
    *,
    state: str | None = None,
    stage: str | None = None,
    stage_label: str | None = None,
    message: str | None = None,
    progress_percent: int | None = None,
    error: str | None = None,
    warnings: list[str] | None = None,
    created_lessons: int | None = None,
    hard_penalty: int | None = None,
    soft_penalty: int | None = None,
    diagnostics: dict[str, int] | None = None,
) -> None:
    now = datetime.now().isoformat(timespec='seconds')
    with _jobs_condition:
        job = _jobs.get(job_id)
        if job is None:
            return

        if state is not None:
            job.state = state
        if stage is not None:
            job.stage = stage
        if stage_label is not None:
            job.stage_label = stage_label
        if message is not None:
            job.message = message
        if progress_percent is not None:
            job.progress_percent = max(0, min(100, int(progress_percent)))
        if error is not None:
            job.error = error
        if warnings is not None:
            job.warnings = list(warnings)
        if created_lessons is not None:
            job.created_lessons = created_lessons
        if hard_penalty is not None:
            job.hard_penalty = hard_penalty
        if soft_penalty is not None:
            job.soft_penalty = soft_penalty
        if diagnostics is not None:
            job.diagnostics = dict(diagnostics)

        job.revision += 1
        job.updated_at = now
        if job.state in FINAL_STATES:
            job.finished_at = now
            global _active_job_id
            if _active_job_id == job_id:
                _active_job_id = None

        should_add_event = bool(message) and (
            not job.events
            or job.events[-1].message != job.message
            or job.events[-1].stage != job.stage
            or job.events[-1].progress_percent != job.progress_percent
        )
        if should_add_event:
            job.events.append(
                GenerationEvent(
                    timestamp=now,
                    stage=job.stage,
                    message=job.message,
                    progress_percent=job.progress_percent,
                )
            )
            job.events = job.events[-12:]
        _jobs_condition.notify_all()


def _get_active_job_unlocked() -> GenerationJob | None:
    if _active_job_id is None:
        return None
    active_job = _jobs.get(_active_job_id)
    if active_job is None:
        return None
    if active_job.state in FINAL_STATES:
        return None
    return active_job
