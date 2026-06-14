from collections import defaultdict
from datetime import date, timedelta
import json
import os
from pathlib import Path
import threading

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, close_old_connections, transaction
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .forms import (
    ScheduleEntryForm,
    ScheduleFilterForm,
    ScheduleGenerationForm,
    TeacherWeeklyAvailabilityForm,
    current_monday,
)
from .generation_jobs import (
    GenerationAlreadyRunningError,
    get_active_generation_job,
    get_generation_job,
    start_generation_job,
    update_generation_job,
    wait_for_generation_job_update,
)
from .models import (
    AvailabilityStatus,
    Class,
    ClassSubject,
    Classroom,
    Schedule,
    ScheduleChange,
    ScheduleChangeType,
    Teacher,
    TeacherAvailability,
    TeachingAssignment,
    TimeSlot,
    UserRole,
    WeeklyClassSubjectLoad,
    Weekday,
    normalize_week_start,
    teacher_availability_map_for_week,
)
from .exports import ExportContext, SUPPORTED_FORMATS, export_grid
from .permissions import (
    can_edit_teacher_availability,
    dispatcher_required,
)
from .services.schedule_generator import GeneticScheduleGenerator

if os.name == 'nt':
    import msvcrt
else:
    import fcntl

GENERATION_LOCK = threading.Lock()
GENERATION_LOCKFILE = Path(settings.BASE_DIR) / '.generation.run.lock'


def _acquire_generation_process_lock():
    lock_handle = open(GENERATION_LOCKFILE, 'a+b')
    lock_handle.seek(0, os.SEEK_END)
    if lock_handle.tell() == 0:
        lock_handle.write(b'0')
        lock_handle.flush()
    lock_handle.seek(0)

    try:
        if os.name == 'nt':
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_handle.close()
        return None

    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(f'pid={os.getpid()}'.encode('ascii', errors='ignore'))
    lock_handle.flush()
    return lock_handle


def _release_generation_process_lock(lock_handle) -> None:
    if lock_handle is None:
        return
    try:
        lock_handle.seek(0)
        lock_handle.truncate()
        lock_handle.write(b'0')
        lock_handle.flush()
    except OSError:
        pass
    try:
        if os.name == 'nt':
            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        lock_handle.close()
    except OSError:
        pass


def dashboard(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        return redirect('scheduler:timetable')
    filter_form = ScheduleFilterForm(request.GET or None)
    filter_form.is_valid()
    week_start = _filter_week_start(filter_form)
    week_end = week_start + timedelta(days=5)
    month_start = week_start.replace(day=1)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1, day=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1, day=1)
    summary = {
        'classes': Class.objects.count(),
        'teachers': Teacher.objects.count(),
        'lessons_this_week': Schedule.objects.filter(
            lesson_date__gte=week_start,
            lesson_date__lt=week_end,
        ).count(),
        'locked_lessons': Schedule.objects.filter(is_locked=True).count(),
    }
    busiest_teachers = (
        Teacher.objects.annotate(
            total_lessons=Count('schedules'),
            lessons_week=Count(
                'schedules',
                filter=Q(
                    schedules__lesson_date__gte=week_start,
                    schedules__lesson_date__lt=week_end,
                ),
            ),
            lessons_month=Count(
                'schedules',
                filter=Q(
                    schedules__lesson_date__gte=month_start,
                    schedules__lesson_date__lt=month_end,
                ),
            ),
        )
        .select_related('user')
        .order_by('-lessons_week', '-lessons_month', '-total_lessons', 'user__full_name')[:8]
    )
    generation_form = ScheduleGenerationForm(initial={'week_start': week_start})
    workload_classes = _build_workload_classes(week_start=week_start)
    return render(
        request,
        'scheduler/dashboard.html',
        {
            'summary': summary,
            'generation_form': generation_form,
            'workload_classes': workload_classes,
            'selected_generation_class_ids': [],
            'filter_form': filter_form,
            'busiest_teachers': busiest_teachers,
            'active_generation_job': get_active_generation_job(),
        },
    )


def timetable(request: HttpRequest) -> HttpResponse:
    filter_form = ScheduleFilterForm(request.GET or None)
    filter_form.is_valid()
    week_start = _filter_week_start(filter_form)
    selected_class = _filter_value(filter_form, 'class_obj')
    selected_teacher = _filter_value(filter_form, 'teacher')

    schedules_qs = (
        Schedule.objects.select_related('class_obj', 'subject', 'teacher__user', 'classroom', 'time_slot__lesson_time')
        .filter(lesson_date__gte=week_start, lesson_date__lt=week_start + timedelta(days=5))
        .order_by('time_slot__lesson_time__lesson_number', 'lesson_date')
    )

    # --- Общее расписание (видно всем, в том числе неавторизованным) ---
    general_schedules = schedules_qs
    if selected_class:
        general_schedules = general_schedules.filter(class_obj=selected_class)
    if selected_teacher:
        general_schedules = general_schedules.filter(teacher=selected_teacher)
    if not selected_class and not selected_teacher:
        first_class = Class.objects.order_by('grade', 'parallel').first()
        if first_class:
            selected_class = first_class
            general_schedules = general_schedules.filter(class_obj=first_class)

    grid = build_week_grid(general_schedules, week_start)
    timetable_scope_label = _build_timetable_scope_label(selected_class, selected_teacher)
    workload_classes = _build_workload_classes(week_start=week_start)

    # --- Личное расписание для авторизованных пользователей ---
    personal_grid = None
    personal_scope_label = ''
    personal_role = None
    personal_target = None
    user = request.user
    if user.is_authenticated:
        if user.role == UserRole.STUDENT and user.class_obj_id:
            personal_role = 'student'
            personal_target = user.class_obj
            personal_schedules = (
                schedules_qs.filter(class_obj=user.class_obj)
            )
            personal_grid = build_week_grid(personal_schedules, week_start)
            personal_scope_label = (
                f'Ваше личное расписание (класс {user.class_obj.name}).'
            )
        elif user.role == UserRole.TEACHER:
            teacher_profile = Teacher.objects.select_related('user').filter(user=user).first()
            if teacher_profile is not None:
                personal_role = 'teacher'
                personal_target = teacher_profile
                personal_schedules = (
                    schedules_qs.filter(teacher=teacher_profile)
                )
                personal_grid = build_week_grid(personal_schedules, week_start)
                personal_scope_label = (
                    'Ваше личное расписание (уроки, которые вы должны провести).'
                )

    return render(
        request,
        'scheduler/timetable.html',
        {
            'filter_form': filter_form,
            'week_start': week_start,
            'grid': grid,
            'selected_class': selected_class,
            'selected_teacher': selected_teacher,
            'timetable_scope_label': timetable_scope_label,
            'workload_classes': workload_classes,
            'selected_generation_class_ids': [],
            'generation_form': ScheduleGenerationForm(
                initial={
                    'week_start': week_start,
                }
            ),
            'active_generation_job': get_active_generation_job(),
            'personal_grid': personal_grid,
            'personal_scope_label': personal_scope_label,
            'personal_role': personal_role,
            'personal_target': personal_target,
        },
    )


def timetable_export(request: HttpRequest) -> HttpResponse:
    """Экспорт текущего вида расписания в Excel или PDF.

    Использует те же фильтры, что и view `timetable`. Формат файла берётся
    из query-параметра ?format=xlsx|pdf (по умолчанию xlsx).
    """
    fmt = (request.GET.get('format') or 'xlsx').lower().strip()
    if fmt not in SUPPORTED_FORMATS:
        messages.error(
            request,
            f'Неподдерживаемый формат экспорта: {fmt!r}. Доступны: {", ".join(SUPPORTED_FORMATS)}.',
        )
        redirect_url = reverse('scheduler:timetable')
        query_string = request.GET.urlencode()
        if query_string:
            redirect_url = f'{redirect_url}?{query_string}'
        return redirect(redirect_url)

    filter_form = ScheduleFilterForm(request.GET or None)
    filter_form.is_valid()
    week_start = _filter_week_start(filter_form)
    selected_class = _filter_value(filter_form, 'class_obj')
    selected_teacher = _filter_value(filter_form, 'teacher')

    schedules = (
        Schedule.objects.select_related('class_obj', 'subject', 'teacher__user', 'classroom', 'time_slot__lesson_time')
        .filter(lesson_date__gte=week_start, lesson_date__lt=week_start + timedelta(days=5))
        .order_by('time_slot__lesson_time__lesson_number', 'lesson_date')
    )
    if selected_class:
        schedules = schedules.filter(class_obj=selected_class)
    if selected_teacher:
        schedules = schedules.filter(teacher=selected_teacher)
    if not selected_class and not selected_teacher:
        selected_class = Class.objects.order_by('grade', 'parallel').first()
        if selected_class:
            schedules = schedules.filter(class_obj=selected_class)

    grid = build_week_grid(schedules, week_start)
    scope_label = _build_timetable_scope_label(selected_class, selected_teacher)
    ctx = ExportContext(
        week_start=week_start,
        scope_label=scope_label,
        show_class_in_cell=selected_class is None,
    )

    try:
        data, filename, content_type = export_grid(grid, ctx, fmt)
    except Exception as exc:  # noqa: BLE001 — пользователю даём дружелюбную ошибку
        messages.error(
            request,
            f'Не удалось сформировать файл расписания: {exc}',
        )
        redirect_url = reverse('scheduler:timetable')
        query_string = request.GET.urlencode()
        if query_string:
            redirect_url = f'{redirect_url}?{query_string}'
        return redirect(redirect_url)

    response = HttpResponse(data, content_type=content_type)
    # RFC 5987 — корректное имя файла с кириллицей.
    from urllib.parse import quote
    response['Content-Disposition'] = (
        f"attachment; filename=\"{filename}\"; "
        f"filename*=UTF-8''{quote(filename)}"
    )
    response['Content-Length'] = str(len(data))
    return response


@login_required
def teacher_detail(request: HttpRequest, pk: int) -> HttpResponse:
    teacher = get_object_or_404(
        Teacher.objects.select_related('user'),
        pk=pk,
    )
    week_start = _query_week_start(request)
    weekday_choices = _teacher_weekday_choices()
    day_statuses, mixed_days = _teacher_day_statuses(
        teacher=teacher,
        week_start=week_start,
        weekday_numbers=[weekday for weekday, _label in weekday_choices],
    )

    # Право менять доступность: только сам преподаватель, диспетчер или администратор.
    can_edit_availability = can_edit_teacher_availability(request.user, teacher)

    form = TeacherWeeklyAvailabilityForm(
        request.POST or None,
        weekday_choices=weekday_choices,
        initial_statuses=day_statuses,
    )
    if request.method == 'POST':
        if not can_edit_availability:
            raise PermissionDenied(
                'Изменять доступность можно только для своего профиля.'
            )
        if form.is_valid():
            _save_teacher_weekday_statuses(
                teacher=teacher,
                week_start=week_start,
                day_statuses=form.cleaned_statuses(),
            )
            messages.success(
                request,
                'Доступность преподавателя для выбранной недели обновлена. Эти настройки уже влияют на генерацию и ручное редактирование расписания.',
            )
            redirect_url = reverse('scheduler:teacher_detail', args=[teacher.pk])
            return redirect(f'{redirect_url}?week_start={week_start.isoformat()}')

    assignments = list(
        teacher.teaching_assignments.select_related('class_obj', 'subject')
        .order_by('class_obj__grade', 'class_obj__parallel', 'subject__name')
    )
    assigned_classes: list[Class] = []
    seen_classes: set[int] = set()
    for assignment in assignments:
        if assignment.class_obj_id in seen_classes:
            continue
        assigned_classes.append(assignment.class_obj)
        seen_classes.add(assignment.class_obj_id)

    week_schedule = list(
        teacher.schedules.select_related('class_obj', 'subject', 'classroom', 'time_slot__lesson_time')
        .filter(
            lesson_date__gte=week_start,
            lesson_date__lt=week_start + timedelta(days=7),
        )
        .order_by('lesson_date', 'time_slot__lesson_time__lesson_number')
    )
    scheduled_by_weekday: dict[int, list[Schedule]] = defaultdict(list)
    for lesson in week_schedule:
        scheduled_by_weekday[lesson.lesson_date.isoweekday()].append(lesson)

    status_labels = dict(TeacherWeeklyAvailabilityForm.STATUS_CHOICES)
    availability_days = [
        {
            'weekday': weekday,
            'label': label,
            'status': day_statuses.get(weekday, AvailabilityStatus.WORKING),
            'status_label': status_labels.get(day_statuses.get(weekday, AvailabilityStatus.WORKING), 'Работает'),
            'lessons': scheduled_by_weekday.get(weekday, []),
            'lessons_count': len(scheduled_by_weekday.get(weekday, [])),
        }
        for weekday, label in weekday_choices
    ]

    return render(
        request,
        'scheduler/teacher_detail.html',
        {
            'teacher': teacher,
            'week_start': week_start,
            'availability_form': form,
            'availability_days': availability_days,
            'mixed_days': [_weekday_label(weekday) for weekday in mixed_days],
            'assigned_classes': assigned_classes,
            'assignments': assignments,
            'week_schedule': week_schedule,
            'previous_week_start': week_start - timedelta(days=7),
            'next_week_start': week_start + timedelta(days=7),
            'can_edit_availability': can_edit_availability,
        },
    )


def generate_timetable(request: HttpRequest) -> HttpResponse:
    if request.method != 'POST':
        return redirect('scheduler:dashboard')

    form = ScheduleGenerationForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Пожалуйста, исправьте поля формы генерации.')
        fallback_week_start = _posted_week_start(request)
        return render(
            request,
            'scheduler/dashboard.html',
            {
                'generation_form': form,
                'workload_classes': _build_workload_classes(week_start=fallback_week_start),
                'selected_generation_class_ids': _posted_generation_class_ids(request),
                'summary': _dashboard_summary(week_start=fallback_week_start),
                'filter_form': ScheduleFilterForm(initial={'week_start': fallback_week_start}),
                'busiest_teachers': [],
            },
        )

    class_ids = list(form.cleaned_data['classes'].values_list('id', flat=True))
    week_start = form.cleaned_data['week_start']
    changed_overrides, workload_errors = _apply_weekly_workload_overrides(
        request=request,
        week_start=week_start,
        class_ids=class_ids,
        save_as_default=form.cleaned_data.get('save_workload_as_default', False),
    )
    if changed_overrides:
        messages.success(
            request,
            f'Нагрузка обновлена: {changed_overrides}. Генерация выполняется с новыми значениями.'
        )
    for warning in workload_errors:
        messages.warning(request, warning)

    generator_settings = form.get_generator_settings()
    generator = GeneticScheduleGenerator(**generator_settings)
    redirect_target = f'{reverse("scheduler:timetable")}?week_start={week_start.isoformat()}'

    if not GENERATION_LOCK.acquire(blocking=False):
        messages.warning(
            request,
            'Сейчас уже выполняется пересчёт расписания. Дождитесь завершения и повторите попытку.'
        )
        return redirect(redirect_target)

    process_lock = _acquire_generation_process_lock()
    if process_lock is None:
        GENERATION_LOCK.release()
        messages.warning(
            request,
            'Сейчас уже выполняется пересчёт расписания. Дождитесь завершения и попробуйте снова.'
        )
        return redirect(redirect_target)

    try:
        result = generator.generate(week_start, class_ids=class_ids)
    except ValidationError:
        messages.error(
            request,
            'Не удалось пересчитать неделю из-за конфликтующих ограничений. '
            'Проверьте доступность преподавателей и ограничения по кабинетам.'
        )
        return redirect(redirect_target)
    except OperationalError as exc:
        if 'locked' in str(exc).lower():
            messages.error(
                request,
                'База данных временно занята. Подождите 10-20 секунд и попробуйте снова.'
            )
        else:
            messages.error(
                request,
                'Во время пересчёта произошла ошибка базы данных. Попробуйте ещё раз.'
            )
        return redirect(redirect_target)
    finally:
        _release_generation_process_lock(process_lock)
        GENERATION_LOCK.release()
    if result.hard_penalty == 0:
        messages.success(
            request,
            f'Сгенерировано {result.created_lessons} занятий. Мягкий штраф: {result.soft_penalty}.',
        )
    else:
        messages.warning(
            request,
            f'Сгенерировано {result.created_lessons} занятий, но остался жёсткий штраф {result.hard_penalty}.',
        )

    for warning in result.warnings:
        messages.warning(request, warning)

    if result.diagnostics.get('class_gap'):
        messages.warning(
            request,
            f"В сгенерированном варианте найдены окна у классов: {result.diagnostics['class_gap']}. "
            "Попробуйте режим «Максимальное качество»."
        )
    if result.diagnostics.get('class_late_start'):
        messages.warning(
            request,
            f"Есть дни, где занятия начинаются не с первого урока: {result.diagnostics['class_late_start']}."
        )
    if result.diagnostics.get('class_daily_overload'):
        messages.warning(
            request,
            f"Есть дни с перегрузкой класса выше дневного лимита: {result.diagnostics['class_daily_overload']}."
        )
    if result.diagnostics.get('class_weekly_overload'):
        messages.warning(
            request,
            f"Превышена недельная нагрузка по отдельным классам: {result.diagnostics['class_weekly_overload']}."
        )
    if result.diagnostics.get('forbidden_double_lesson'):
        messages.warning(
            request,
            f"Обнаружены недопустимые сдвоенные уроки: {result.diagnostics['forbidden_double_lesson']}."
        )
    if result.diagnostics.get('class_daily_imbalance'):
        messages.warning(
            request,
            f"Нагрузка распределена неравномерно по дням: {result.diagnostics['class_daily_imbalance']}."
        )
    if result.diagnostics.get('class_sparse_days'):
        messages.warning(
            request,
            f"Есть слишком лёгкие дни при большой недельной нагрузке: {result.diagnostics['class_sparse_days']}."
        )
    if result.diagnostics.get('hard_subject_weekday_mismatch'):
        messages.warning(
            request,
            f"Сложные предметы неидеально распределены по дням (лучше вторник/среда): {result.diagnostics['hard_subject_weekday_mismatch']}."
        )
    if result.diagnostics.get('subject_alternation'):
        messages.warning(
            request,
            f"Есть проблемы с чередованием предметов в течение дня: {result.diagnostics['subject_alternation']}."
        )

    redirect_url = reverse('scheduler:timetable')
    if class_ids:
        redirect_url += f'?class_obj={class_ids[0]}&week_start={week_start.isoformat()}'
    else:
        redirect_url += f'?week_start={week_start.isoformat()}'
    return redirect(redirect_url)


@dispatcher_required
def start_generation(request: HttpRequest) -> HttpResponse:
    if request.method != 'POST':
        return redirect('scheduler:dashboard')

    form = ScheduleGenerationForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Пожалуйста, исправьте поля формы генерации.')
        fallback_week_start = _posted_week_start(request)
        return render(
            request,
            'scheduler/dashboard.html',
            {
                'generation_form': form,
                'workload_classes': _build_workload_classes(week_start=fallback_week_start),
                'selected_generation_class_ids': _posted_generation_class_ids(request),
                'summary': _dashboard_summary(week_start=fallback_week_start),
                'filter_form': ScheduleFilterForm(initial={'week_start': fallback_week_start}),
                'busiest_teachers': [],
                'active_generation_job': get_active_generation_job(),
            },
        )

    class_ids = list(form.cleaned_data['classes'].values_list('id', flat=True))
    week_start = form.cleaned_data['week_start']
    changed_overrides, workload_errors = _apply_weekly_workload_overrides(
        request=request,
        week_start=week_start,
        class_ids=class_ids,
        save_as_default=form.cleaned_data.get('save_workload_as_default', False),
    )
    if changed_overrides:
        messages.success(
            request,
            f'Нагрузка обновлена: {changed_overrides}. Генерация выполнится с новыми значениями.'
        )
    for warning in workload_errors:
        messages.warning(request, warning)

    redirect_target = _build_generation_result_url(week_start=week_start, class_ids=class_ids)
    generator_settings = form.get_generator_settings()
    run_inline = getattr(settings, 'SCHEDULER_GENERATION_RUN_INLINE', False)

    if not GENERATION_LOCK.acquire(blocking=False):
        active_job = get_active_generation_job()
        messages.warning(
            request,
            'Сейчас уже выполняется пересчет расписания. Откройте экран прогресса и дождитесь завершения.'
        )
        if active_job is not None:
            return redirect('scheduler:generation_progress', job_id=active_job.job_id)
        return redirect(redirect_target)

    process_lock = None
    try:
        process_lock = _acquire_generation_process_lock()
        if process_lock is None:
            active_job = get_active_generation_job()
            messages.warning(
                request,
                'Сейчас уже выполняется пересчет расписания. Дождитесь завершения текущей генерации.'
            )
            if active_job is not None:
                return redirect('scheduler:generation_progress', job_id=active_job.job_id)
            return redirect(redirect_target)

        job = start_generation_job(
            week_start=week_start,
            class_ids=class_ids,
            result_url=redirect_target,
            worker=lambda job_id: _run_generation_job(
                job_id=job_id,
                week_start=week_start,
                class_ids=class_ids,
                generator_settings=generator_settings,
                process_lock=process_lock,
                manage_db_connections=not run_inline,
            ),
            run_inline=run_inline,
        )
    except GenerationAlreadyRunningError as exc:
        if process_lock is not None:
            _release_generation_process_lock(process_lock)
        active_job = get_generation_job(exc.job_id)
        messages.warning(
            request,
            'Генерация уже запущена. Показываю текущий прогресс по активной задаче.'
        )
        if active_job is not None:
            return redirect('scheduler:generation_progress', job_id=active_job.job_id)
        return redirect(redirect_target)
    finally:
        GENERATION_LOCK.release()

    messages.success(
        request,
        'Генерация запущена. На следующем экране будет виден текущий этап расчета и общий прогресс.'
    )
    return redirect('scheduler:generation_progress', job_id=job.job_id)


@dispatcher_required
def generation_progress(request: HttpRequest, job_id: str) -> HttpResponse:
    job = get_generation_job(job_id)
    if job is None:
        messages.warning(request, 'Задача генерации не найдена. Возможно, сервер уже перезапускался.')
        return redirect('scheduler:dashboard')
    return render(
        request,
        'scheduler/generation_progress.html',
        {
            'job': job,
            'job_payload': job.to_payload(),
            'active_generation_job': get_active_generation_job(),
        },
    )


@dispatcher_required
def generation_status(request: HttpRequest, job_id: str) -> JsonResponse:
    job = get_generation_job(job_id)
    if job is None:
        return JsonResponse({'detail': 'generation job not found'}, status=404)
    return JsonResponse(job.to_payload())


@dispatcher_required
def generation_events(request: HttpRequest, job_id: str) -> HttpResponse:
    job = get_generation_job(job_id)
    if job is None:
        return JsonResponse({'detail': 'generation job not found'}, status=404)

    def event_stream():
        current_revision = job.revision
        initial_payload = json.dumps(job.to_payload(), ensure_ascii=False)
        yield 'retry: 5000\n'
        yield f'event: status\ndata: {initial_payload}\n\n'
        if job.state in {'completed', 'failed', 'cancelled'}:
            return

        while True:
            updated_job = wait_for_generation_job_update(
                job_id,
                known_revision=current_revision,
                timeout_seconds=25.0,
            )
            if updated_job is None:
                yield 'event: missing\ndata: {}\n\n'
                break
            if updated_job.revision == current_revision:
                if updated_job.state in {'completed', 'failed', 'cancelled'}:
                    break
                yield ': keepalive\n\n'
                continue

            current_revision = updated_job.revision
            payload = json.dumps(updated_job.to_payload(), ensure_ascii=False)
            yield f'event: status\ndata: {payload}\n\n'
            if updated_job.state in {'completed', 'failed', 'cancelled'}:
                break

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


def _run_generation_job(
    *,
    job_id: str,
    week_start: date,
    class_ids: list[int],
    generator_settings: dict[str, int | float],
    process_lock,
    manage_db_connections: bool,
) -> None:
    if manage_db_connections:
        close_old_connections()
    update_generation_job(
        job_id,
        state='running',
        stage='preparing',
        stage_label='Подготовка данных',
        message='Запускаем генератор и готовим данные для расчета.',
        progress_percent=1,
    )
    try:
        generator = GeneticScheduleGenerator(**generator_settings)
        result = generator.generate(
            week_start,
            class_ids=class_ids,
            progress_callback=lambda stage, stage_label, message, progress_percent: update_generation_job(
                job_id,
                state='running',
                stage=stage,
                stage_label=stage_label,
                message=message,
                progress_percent=progress_percent,
            ),
        )
    except ValidationError:
        update_generation_job(
            job_id,
            state='failed',
            stage='failed',
            stage_label='Не удалось построить расписание',
            message='Ограничения противоречат друг другу. Проверьте доступность учителей и кабинетов.',
            progress_percent=100,
            error='validation_error',
        )
    except OperationalError as exc:
        if 'locked' in str(exc).lower():
            message = 'База данных временно занята. Подождите 10-20 секунд и попробуйте снова.'
        else:
            message = 'Во время генерации произошла ошибка базы данных. Попробуйте еще раз.'
        update_generation_job(
            job_id,
            state='failed',
            stage='failed',
            stage_label='Ошибка сохранения',
            message=message,
            progress_percent=100,
            error=str(exc),
        )
    except Exception as exc:
        update_generation_job(
            job_id,
            state='failed',
            stage='failed',
            stage_label='Непредвиденная ошибка',
            message='Генерация прервалась из-за внутренней ошибки. Детали сохранены в задаче.',
            progress_percent=100,
            error=str(exc),
        )
    else:
        summary_message = (
            f'Готово: создано {result.created_lessons} занятий. '
            f'Жесткий штраф {result.hard_penalty}, мягкий штраф {result.soft_penalty}.'
        )
        update_generation_job(
            job_id,
            state='completed',
            stage='completed',
            stage_label='Готово',
            message=summary_message,
            progress_percent=100,
            warnings=result.warnings,
            created_lessons=result.created_lessons,
            hard_penalty=result.hard_penalty,
            soft_penalty=result.soft_penalty,
            diagnostics=result.diagnostics,
        )
    finally:
        if manage_db_connections:
            close_old_connections()
        _release_generation_process_lock(process_lock)


def _build_generation_result_url(*, week_start: date, class_ids: list[int]) -> str:
    redirect_url = reverse('scheduler:timetable')
    if class_ids:
        return f'{redirect_url}?class_obj={class_ids[0]}&week_start={week_start.isoformat()}'
    return f'{redirect_url}?week_start={week_start.isoformat()}'


@dispatcher_required
def schedule_create(request: HttpRequest) -> HttpResponse:
    initial = _entry_initial(request)
    form = ScheduleEntryForm(request.POST or None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        schedule = form.save()
        if schedule.note:
            ScheduleChange.objects.create(
                schedule=schedule,
                change_type=ScheduleChangeType.RESCHEDULE,
                description=schedule.note,
            )
        messages.success(request, 'Занятие успешно создано.')
        return redirect(_timetable_redirect(schedule))
    return render(request, 'scheduler/schedule_form.html', {'form': form, 'title': 'Создать занятие'})


@dispatcher_required
def schedule_edit(request: HttpRequest, pk: int) -> HttpResponse:
    schedule = get_object_or_404(Schedule, pk=pk)
    original_teacher_id = schedule.teacher_id
    original_slot_id = schedule.time_slot_id
    original_date = schedule.lesson_date
    form = ScheduleEntryForm(request.POST or None, instance=schedule)
    if request.method == 'POST' and form.is_valid():
        updated = form.save()
        change_type = ScheduleChangeType.RESCHEDULE
        if original_teacher_id != updated.teacher_id:
            change_type = ScheduleChangeType.TEACHER_SUBSTITUTION
        if original_slot_id != updated.time_slot_id or original_date != updated.lesson_date or updated.note:
            ScheduleChange.objects.create(
                schedule=updated,
                change_type=change_type,
                description=updated.note or 'Расписание вручную изменено через веб-интерфейс.',
            )
        messages.success(request, 'Занятие успешно обновлено.')
        return redirect(_timetable_redirect(updated))
    return render(request, 'scheduler/schedule_form.html', {'form': form, 'title': 'Редактировать занятие', 'schedule': schedule})


@dispatcher_required
def schedule_delete(request: HttpRequest, pk: int) -> HttpResponse:
    schedule = get_object_or_404(Schedule, pk=pk)
    if request.method == 'POST':
        redirect_target = _timetable_redirect(schedule)
        ScheduleChange.objects.create(
            schedule=schedule,
            change_type=ScheduleChangeType.CANCEL,
            description=schedule.note or 'Занятие вручную удалено из редактора расписания.',
        )
        schedule.delete()
        messages.success(request, 'Занятие удалено.')
        return redirect(redirect_target)
    return render(request, 'scheduler/schedule_confirm_delete.html', {'schedule': schedule})


# === Страница «Конфликты и окна» ============================================

CONFLICTS_SESSION_KEY = 'conflicts_unlocked_for_week'
CONFLICT_DAY_PREVIEW_SESSION_KEY = 'conflict_day_substitution_preview'


@dispatcher_required
def schedule_conflicts(request: HttpRequest) -> HttpResponse:
    """Показывает классы, у которых в недельном расписании есть окна между
    уроками или поздний старт дня (первый урок не №1).

    Побочный эффект: записывает в сессию маркер `conflicts_unlocked_for_week`
    — он используется в `conflict_day_view`, чтобы запретить прямой доступ к
    специальному сплит-виду дня без перехода через эту страницу.
    """
    week_start = _query_week_start(request)
    week_end = week_start + timedelta(days=5)

    schedules = list(
        Schedule.objects.select_related(
            'class_obj',
            'subject',
            'teacher__user',
            'classroom',
            'time_slot__lesson_time',
        )
        .filter(lesson_date__gte=week_start, lesson_date__lt=week_end)
        .order_by('class_obj__grade', 'class_obj__parallel', 'lesson_date', 'time_slot__lesson_time__lesson_number')
    )

    class_day_map: dict[int, dict[date, list]] = defaultdict(lambda: defaultdict(list))
    class_lookup: dict[int, Class] = {}
    for schedule in schedules:
        class_day_map[schedule.class_obj_id][schedule.lesson_date].append(schedule)
        class_lookup[schedule.class_obj_id] = schedule.class_obj

    report_rows: list[dict] = []
    for class_id, days in sorted(class_day_map.items(), key=lambda item: (class_lookup[item[0]].grade, class_lookup[item[0]].parallel)):
        problem_days = []
        for lesson_date in sorted(days.keys()):
            day_schedules = days[lesson_date]
            numbers = sorted({item.time_slot.lesson_time.lesson_number for item in day_schedules})
            if not numbers:
                continue
            has_gap = (numbers[-1] - numbers[0] + 1) - len(numbers) > 0
            has_late_start = numbers[0] > 1
            if not (has_gap or has_late_start):
                continue
            gap_numbers = sorted(set(range(numbers[0], numbers[-1] + 1)) - set(numbers))
            problem_days.append({
                'date': lesson_date,
                'weekday_label': _weekday_label(lesson_date.isoweekday()),
                'lesson_numbers': numbers,
                'has_gap': has_gap,
                'has_late_start': has_late_start,
                'gap_numbers': gap_numbers,
                'first_lesson_number': numbers[0],
            })
        if problem_days:
            report_rows.append({
                'class_obj': class_lookup[class_id],
                'problem_days': problem_days,
            })

    # Маркер сессии: разрешаем доступ к /conflicts/day/... только если пользователь
    # реально побывал на этой странице за актуальную неделю.
    request.session[CONFLICTS_SESSION_KEY] = week_start.isoformat()
    request.session.modified = True

    return render(
        request,
        'scheduler/conflicts.html',
        {
            'week_start': week_start,
            'report_rows': report_rows,
            'has_problems': bool(report_rows),
        },
    )


def _conflict_day_url(*, class_id: int, lesson_date: date) -> str:
    return reverse(
        'scheduler:conflict_day_view',
        kwargs={'class_id': class_id, 'lesson_date': lesson_date.isoformat()},
    )


def _get_conflict_day_preview(
    request: HttpRequest,
    *,
    class_id: int,
    lesson_date: date,
) -> dict | None:
    payload = request.session.get(CONFLICT_DAY_PREVIEW_SESSION_KEY)
    if not isinstance(payload, dict):
        return None
    if payload.get('class_id') != class_id:
        return None
    if payload.get('lesson_date') != lesson_date.isoformat():
        return None
    items = payload.get('items')
    if not isinstance(items, list) or not items:
        return None
    return payload


def _set_conflict_day_preview(request: HttpRequest, payload: dict) -> None:
    request.session[CONFLICT_DAY_PREVIEW_SESSION_KEY] = payload
    request.session.modified = True


def _clear_conflict_day_preview(request: HttpRequest) -> None:
    request.session.pop(CONFLICT_DAY_PREVIEW_SESSION_KEY, None)
    request.session.modified = True


def _build_conflict_day_substitution_preview(*, class_obj: Class, target_date: date) -> tuple[dict | None, str | None]:
    class_day_lessons = list(
        Schedule.objects.select_related('subject', 'teacher__user', 'classroom', 'time_slot__lesson_time')
        .filter(class_obj=class_obj, lesson_date=target_date)
        .order_by('time_slot__lesson_time__lesson_number', 'id')
    )
    if not class_day_lessons:
        return None, 'Для этого класса в выбранный день нет занятий.'

    weekday = target_date.isoweekday()
    day_slots = list(
        TimeSlot.objects.select_related('lesson_time')
        .filter(weekday=weekday, lesson_time__day_type='normal')
        .order_by('lesson_time__lesson_number')
    )
    if len(day_slots) < len(class_day_lessons):
        return None, 'На этот день не хватает временных слотов для уплотнения расписания.'

    target_slots = day_slots[:len(class_day_lessons)]
    target_slot_ids = [slot.id for slot in target_slots]
    lesson_ids = [item.id for item in class_day_lessons]
    subject_ids = sorted({item.subject_id for item in class_day_lessons})

    class_assignment_pairs = set(
        TeachingAssignment.objects.filter(class_obj=class_obj, subject_id__in=subject_ids)
        .values_list('subject_id', 'teacher_id')
    )

    # Кандидаты по предмету: сначала «родной» учитель урока, затем остальные предметники.
    subject_teacher_candidates: dict[int, list[int]] = defaultdict(list)
    for subject_id, teacher_id in (
        TeachingAssignment.objects.filter(subject_id__in=subject_ids)
        .values_list('subject_id', 'teacher_id')
        .distinct()
    ):
        subject_teacher_candidates[subject_id].append(teacher_id)
    for lesson in class_day_lessons:
        teacher_list = subject_teacher_candidates[lesson.subject_id]
        if lesson.teacher_id in teacher_list:
            teacher_list.remove(lesson.teacher_id)
        teacher_list.insert(0, lesson.teacher_id)

    other_day_lessons = list(
        Schedule.objects.select_related('class_obj', 'subject', 'teacher', 'classroom', 'time_slot__lesson_time')
        .filter(lesson_date=target_date)
        .exclude(pk__in=lesson_ids)
    )
    teacher_busy_slots = {(item.teacher_id, item.time_slot_id) for item in other_day_lessons}
    room_busy_slots = {(item.classroom_id, item.time_slot_id) for item in other_day_lessons}

    teacher_day_load: dict[int, int] = defaultdict(int)
    for item in other_day_lessons:
        teacher_day_load[item.teacher_id] += 1

    all_candidate_teacher_ids = sorted(
        {
            teacher_id
            for teacher_ids in subject_teacher_candidates.values()
            for teacher_id in teacher_ids
        }
    )
    availability_map = teacher_availability_map_for_week(
        week_start=target_date,
        teacher_ids=all_candidate_teacher_ids,
        time_slot_ids=target_slot_ids,
    )

    teacher_lookup = {
        teacher.id: teacher
        for teacher in Teacher.objects.select_related('user').filter(id__in=all_candidate_teacher_ids)
    }
    room_lookup = {room.id: room for room in Classroom.objects.all()}

    lessons_pool = class_day_lessons.copy()
    assignments_by_slot: dict[int, tuple[int, int, int]] = {}

    def _teacher_candidates_for(lesson: Schedule, slot_id: int) -> list[int]:
        candidates = []
        for teacher_id in subject_teacher_candidates.get(lesson.subject_id, []):
            if (teacher_id, slot_id) in teacher_busy_slots:
                continue
            status = availability_map.get((teacher_id, slot_id), AvailabilityStatus.WORKING)
            if status != AvailabilityStatus.WORKING:
                continue
            candidates.append(teacher_id)
        candidates.sort(
            key=lambda teacher_id: (
                teacher_id != lesson.teacher_id,
                (lesson.subject_id, teacher_id) not in class_assignment_pairs,
                teacher_day_load.get(teacher_id, 0),
            )
        )
        return candidates

    def _room_candidates_for(lesson: Schedule, slot_id: int) -> list[int]:
        subject = lesson.subject
        candidates = []
        for room in room_lookup.values():
            if room.room_type != subject.required_room_type:
                continue
            if room.capacity < class_obj.students_count:
                continue
            if (room.id, slot_id) in room_busy_slots:
                continue
            candidates.append(room.id)
        candidates.sort(key=lambda room_id: (room_id != lesson.classroom_id, str(room_lookup[room_id].name)))
        return candidates

    def _build_slot_options(lesson_index: int, slot_id: int) -> list[tuple[int, int]]:
        lesson = lessons_pool[lesson_index]
        teacher_ids = _teacher_candidates_for(lesson, slot_id)
        room_ids = _room_candidates_for(lesson, slot_id)
        options: list[tuple[int, int]] = []
        for teacher_id in teacher_ids:
            for room_id in room_ids:
                options.append((teacher_id, room_id))
        return options

    def _search(slot_index: int, remaining_lesson_indexes: list[int]) -> bool:
        if slot_index >= len(target_slots):
            return True

        slot = target_slots[slot_index]
        lesson_options: list[tuple[int, list[tuple[int, int]]]] = []
        for lesson_index in remaining_lesson_indexes:
            options = _build_slot_options(lesson_index, slot.id)
            if options:
                lesson_options.append((lesson_index, options))

        if not lesson_options:
            return False

        lesson_options.sort(key=lambda item: len(item[1]))
        for lesson_index, options in lesson_options:
            for teacher_id, room_id in options:
                assignments_by_slot[slot.id] = (lesson_index, teacher_id, room_id)
                next_remaining = [idx for idx in remaining_lesson_indexes if idx != lesson_index]
                if _search(slot_index + 1, next_remaining):
                    return True
                assignments_by_slot.pop(slot.id, None)
        return False

    if not _search(0, list(range(len(lessons_pool)))):
        return None, (
            'Не удалось подобрать вариант без окон на этот день. '
            'Проверьте занятость учителей и кабинетов в выбранную дату.'
        )

    items: list[dict] = []
    teacher_substitutions = 0
    external_substitutions = 0
    for slot in target_slots:
        lesson_index, teacher_id, room_id = assignments_by_slot[slot.id]
        source_lesson = lessons_pool[lesson_index]
        teacher_obj = teacher_lookup.get(teacher_id)
        room_obj = room_lookup.get(room_id)
        is_teacher_substitution = teacher_id != source_lesson.teacher_id
        is_external_substitution = (source_lesson.subject_id, teacher_id) not in class_assignment_pairs
        if is_teacher_substitution:
            teacher_substitutions += 1
        if is_external_substitution:
            external_substitutions += 1
        items.append(
            {
                'schedule_id': source_lesson.id,
                'subject_id': source_lesson.subject_id,
                'subject_name': source_lesson.subject.name,
                'old_teacher_id': source_lesson.teacher_id,
                'old_teacher_name': str(source_lesson.teacher),
                'new_teacher_id': teacher_id,
                'new_teacher_name': str(teacher_obj) if teacher_obj else f'ID {teacher_id}',
                'old_time_slot_id': source_lesson.time_slot_id,
                'old_lesson_number': source_lesson.time_slot.lesson_time.lesson_number,
                'new_time_slot_id': slot.id,
                'new_lesson_number': slot.lesson_time.lesson_number,
                'old_classroom_id': source_lesson.classroom_id,
                'old_classroom_name': source_lesson.classroom.name,
                'new_classroom_id': room_id,
                'new_classroom_name': room_obj.name if room_obj else f'ID {room_id}',
                'is_teacher_substitution': is_teacher_substitution,
                'is_external_substitution': is_external_substitution,
            }
        )

    items.sort(key=lambda row: (row['new_lesson_number'], row['subject_name'], row['schedule_id']))
    payload = {
        'class_id': class_obj.id,
        'class_name': class_obj.name,
        'lesson_date': target_date.isoformat(),
        'weekday_label': _weekday_label(target_date.isoweekday()),
        'items': items,
        'summary': {
            'lessons_total': len(items),
            'teacher_substitutions': teacher_substitutions,
            'external_substitutions': external_substitutions,
        },
    }
    return payload, None


def _apply_conflict_day_substitution_preview(*, class_obj: Class, target_date: date, preview_payload: dict) -> tuple[int, int]:
    items = preview_payload.get('items') or []
    if not items:
        raise ValidationError('Пустой предпросмотр замены.')

    preview_ids = {int(item['schedule_id']) for item in items}
    current_day_lessons = list(
        Schedule.objects.select_related('subject', 'teacher__user', 'classroom', 'time_slot__lesson_time')
        .filter(class_obj=class_obj, lesson_date=target_date)
        .order_by('time_slot__lesson_time__lesson_number', 'id')
    )
    current_day_ids = {item.id for item in current_day_lessons}
    if preview_ids != current_day_ids:
        raise ValidationError(
            'Состав расписания этого дня изменился после предпросмотра. '
            'Сформируйте предпросмотр заново.'
        )

    lesson_lookup = {item.id: item for item in current_day_lessons}
    target_slot_ids = [int(item['new_time_slot_id']) for item in items]
    autogenerated_note = (
        f'Автоматическая замена на {target_date.strftime("%d.%m.%Y")} '
        f'через страницу «Конфликты и окна».'
    )

    teacher_substitutions = 0
    created_schedules: list[Schedule] = []
    for item in items:
        source = lesson_lookup[int(item['schedule_id'])]
        if bool(item.get('is_teacher_substitution')):
            teacher_substitutions += 1
        source_note = (source.note or '').strip()
        note_value = f'{source_note} {autogenerated_note}'.strip()
        if len(note_value) > 255:
            note_value = note_value[:255]
        created_schedules.append(
            Schedule(
                class_obj=source.class_obj,
                subject=source.subject,
                teacher_id=int(item['new_teacher_id']),
                classroom_id=int(item['new_classroom_id']),
                time_slot_id=int(item['new_time_slot_id']),
                lesson_date=target_date,
                is_locked=source.is_locked,
                note=note_value,
            )
        )

    with transaction.atomic():
        Schedule.objects.filter(id__in=list(current_day_ids)).delete()
        Schedule.objects.bulk_create(created_schedules, batch_size=100)

        created_by_slot = {
            schedule.time_slot_id: schedule
            for schedule in Schedule.objects.filter(
                class_obj=class_obj,
                lesson_date=target_date,
                time_slot_id__in=target_slot_ids,
            )
        }
        changes: list[ScheduleChange] = []
        for item in items:
            schedule = created_by_slot.get(int(item['new_time_slot_id']))
            if schedule is None:
                continue
            lesson_no = int(item.get('new_lesson_number') or 0)
            change_type = (
                ScheduleChangeType.TEACHER_SUBSTITUTION
                if bool(item.get('is_teacher_substitution'))
                else ScheduleChangeType.RESCHEDULE
            )
            description = (
                f'Автоматическая замена для {class_obj.name} на {target_date.strftime("%d.%m.%Y")}: '
                f'урок №{lesson_no}, предмет «{item.get("subject_name", "")}», '
                f'учитель: {item.get("old_teacher_name", "")} → {item.get("new_teacher_name", "")}.'
            )
            changes.append(
                ScheduleChange(
                    schedule=schedule,
                    change_type=change_type,
                    description=description[:5000],
                )
            )
        if changes:
            ScheduleChange.objects.bulk_create(changes, batch_size=100)

    return len(items), teacher_substitutions


@dispatcher_required
def conflict_day_view(request: HttpRequest, class_id: int, lesson_date: str) -> HttpResponse:
    """Сплит-вид: слева — день одного класса, справа — расписание учителей
    этого класса на тот же день.

    Доступ ТОЛЬКО для пользователей, прошедших через `schedule_conflicts`.
    Это инвариант проверяется по маркеру сессии.
    """
    try:
        target_date = date.fromisoformat(lesson_date)
    except ValueError:
        messages.warning(request, 'Некорректная дата урока.')
        return redirect('scheduler:schedule_conflicts')

    week_start = target_date - timedelta(days=target_date.weekday())
    expected_marker = week_start.isoformat()
    actual_marker = request.session.get(CONFLICTS_SESSION_KEY)
    if actual_marker != expected_marker:
        messages.info(
            request,
            'Этот вид доступен только из страницы «Конфликты и окна». '
            'Откройте её для нужной недели, а затем перейдите на день.',
        )
        return redirect(f"{reverse('scheduler:schedule_conflicts')}?week_start={week_start.isoformat()}")

    class_obj = get_object_or_404(Class, pk=class_id)

    if request.method == 'POST':
        action = (request.POST.get('action') or '').strip()
        if action == 'generate_substitution_preview':
            preview_payload, error_text = _build_conflict_day_substitution_preview(
                class_obj=class_obj,
                target_date=target_date,
            )
            if error_text:
                messages.warning(request, error_text)
                _clear_conflict_day_preview(request)
            else:
                _set_conflict_day_preview(request, preview_payload)
                summary = preview_payload.get('summary', {})
                messages.success(
                    request,
                    'Сформирован предпросмотр на день: '
                    f"{summary.get('lessons_total', 0)} уроков, "
                    f"замен учителя {summary.get('teacher_substitutions', 0)}, "
                    f"временных внешних замен {summary.get('external_substitutions', 0)}. "
                    'Расписание пока не сохранено.',
                )
            return redirect(_conflict_day_url(class_id=class_id, lesson_date=target_date))

        if action == 'apply_substitution_preview':
            preview_payload = _get_conflict_day_preview(
                request,
                class_id=class_id,
                lesson_date=target_date,
            )
            if not preview_payload:
                messages.warning(request, 'Нет актуального предпросмотра для сохранения. Сначала выполните генерацию.')
                return redirect(_conflict_day_url(class_id=class_id, lesson_date=target_date))
            try:
                changed_count, teacher_substitutions = _apply_conflict_day_substitution_preview(
                    class_obj=class_obj,
                    target_date=target_date,
                    preview_payload=preview_payload,
                )
            except ValidationError as exc:
                messages.warning(request, str(exc))
            except (IntegrityError, OperationalError):
                messages.error(
                    request,
                    'Не удалось сохранить сгенерированные замены: данные дня могли измениться параллельно. '
                    'Попробуйте сформировать предпросмотр ещё раз.',
                )
            else:
                messages.success(
                    request,
                    f'Сохранено {changed_count} уроков на этот день. '
                    f'Замен учителя: {teacher_substitutions}.',
                )
                _clear_conflict_day_preview(request)
            return redirect(_conflict_day_url(class_id=class_id, lesson_date=target_date))

        if action == 'discard_substitution_preview':
            _clear_conflict_day_preview(request)
            messages.info(request, 'Предпросмотр замен очищен. Текущее расписание не изменялось.')
            return redirect(_conflict_day_url(class_id=class_id, lesson_date=target_date))

    substitution_preview = _get_conflict_day_preview(
        request,
        class_id=class_id,
        lesson_date=target_date,
    )

    class_day_lessons = list(
        Schedule.objects.select_related('subject', 'teacher__user', 'classroom', 'time_slot__lesson_time')
        .filter(class_obj=class_obj, lesson_date=target_date)
        .order_by('time_slot__lesson_time__lesson_number')
    )

    teacher_ids = list(
        class_obj.teaching_assignments.values_list('teacher_id', flat=True).distinct()
    )
    teachers = list(
        Teacher.objects.select_related('user')
        .filter(pk__in=teacher_ids)
        .order_by('user__full_name')
    )

    teacher_day_schedules = list(
        Schedule.objects.select_related('class_obj', 'subject', 'classroom', 'time_slot__lesson_time')
        .filter(teacher_id__in=teacher_ids, lesson_date=target_date)
        .order_by('teacher_id', 'time_slot__lesson_time__lesson_number')
    )

    teacher_blocks: list[dict] = []
    by_teacher: dict[int, list] = defaultdict(list)
    for item in teacher_day_schedules:
        by_teacher[item.teacher_id].append(item)
    for teacher in teachers:
        teacher_blocks.append({
            'teacher': teacher,
            'lessons': by_teacher.get(teacher.pk, []),
        })

    class_numbers = sorted({lesson.time_slot.lesson_time.lesson_number for lesson in class_day_lessons})
    has_gap = False
    has_late_start = False
    if class_numbers:
        has_gap = (class_numbers[-1] - class_numbers[0] + 1) - len(class_numbers) > 0
        has_late_start = class_numbers[0] > 1

    preview_items: list[dict] = []
    preview_numbers: list[int] = []
    preview_has_gap = False
    preview_has_late_start = False
    if substitution_preview:
        preview_items = sorted(
            substitution_preview.get('items', []),
            key=lambda row: row.get('new_lesson_number', 0),
        )
        preview_numbers = sorted({int(item.get('new_lesson_number', 0)) for item in preview_items if item.get('new_lesson_number')})
        if preview_numbers:
            preview_has_gap = (preview_numbers[-1] - preview_numbers[0] + 1) - len(preview_numbers) > 0
            preview_has_late_start = preview_numbers[0] > 1

    return render(
        request,
        'scheduler/conflicts_day.html',
        {
            'class_obj': class_obj,
            'lesson_date': target_date,
            'weekday_label': _weekday_label(target_date.isoweekday()),
            'week_start': week_start,
            'class_day_lessons': class_day_lessons,
            'teacher_blocks': teacher_blocks,
            'has_gap': has_gap,
            'has_late_start': has_late_start,
            'class_numbers': class_numbers,
            'substitution_preview': substitution_preview,
            'preview_items': preview_items,
            'preview_numbers': preview_numbers,
            'preview_has_gap': preview_has_gap,
            'preview_has_late_start': preview_has_late_start,
        },
    )


def build_week_grid(schedules, week_start: date) -> dict:
    weekday_names = {
        1: 'Понедельник',
        2: 'Вторник',
        3: 'Среда',
        4: 'Четверг',
        5: 'Пятница',
    }
    weekday_short_names = {
        1: 'пн',
        2: 'вт',
        3: 'ср',
        4: 'чт',
        5: 'пт',
    }
    month_names = {
        1: 'янв',
        2: 'фев',
        3: 'мар',
        4: 'апр',
        5: 'май',
        6: 'июн',
        7: 'июл',
        8: 'авг',
        9: 'сен',
        10: 'окт',
        11: 'ноя',
        12: 'дек',
    }
    weekdays = []
    for offset in range(5):
        current_day = week_start + timedelta(days=offset)
        iso_weekday = current_day.isoweekday()
        weekdays.append({
            'date': current_day,
            'weekday': iso_weekday,
            'short_name': weekday_short_names.get(iso_weekday, ''),
            'label': f"{weekday_names[iso_weekday]}, {current_day.day:02d} {month_names[current_day.month]}",
        })

    row_map = {}
    for slot in TimeSlot.objects.select_related('lesson_time').filter(
        weekday=Weekday.MONDAY,
        lesson_time__day_type='normal',
    ).order_by('lesson_time__lesson_number'):
        row_map[slot.lesson_time.lesson_number] = {
            'lesson_number': slot.lesson_time.lesson_number,
            'time': f'{slot.lesson_time.start_time.strftime("%H:%M")} - {slot.lesson_time.end_time.strftime("%H:%M")}',
            'cells': {},
        }

    if not row_map:
        for slot in TimeSlot.objects.select_related('lesson_time').order_by('lesson_time__lesson_number'):
            row_map.setdefault(
                slot.lesson_time.lesson_number,
                {
                    'lesson_number': slot.lesson_time.lesson_number,
                    'time': f'{slot.lesson_time.start_time.strftime("%H:%M")} - {slot.lesson_time.end_time.strftime("%H:%M")}',
                    'cells': {},
                },
            )

    for item in schedules:
        row = row_map.setdefault(
            item.time_slot.lesson_time.lesson_number,
            {
                'lesson_number': item.time_slot.lesson_time.lesson_number,
                'time': f'{item.time_slot.lesson_time.start_time.strftime("%H:%M")} - {item.time_slot.lesson_time.end_time.strftime("%H:%M")}',
                'cells': {},
            },
        )
        row['cells'][item.lesson_date] = item

    rows = [row_map[number] for number in sorted(row_map)]
    return {'weekdays': weekdays, 'rows': rows}


def _entry_initial(request: HttpRequest) -> dict:
    initial = {}
    if request.GET.get('class_obj'):
        initial['class_obj'] = request.GET['class_obj']
    if request.GET.get('lesson_date'):
        initial['lesson_date'] = request.GET['lesson_date']
    if request.GET.get('time_slot'):
        initial['time_slot'] = request.GET['time_slot']
    return initial


def _timetable_redirect(schedule: Schedule) -> str:
    return f'{reverse("scheduler:timetable")}?class_obj={schedule.class_obj_id}&week_start={schedule.lesson_date.isoformat()}'


def _query_week_start(request: HttpRequest) -> date:
    raw_value = (request.GET.get('week_start') or '').strip()
    if not raw_value:
        return current_monday()
    try:
        parsed = date.fromisoformat(raw_value)
    except ValueError:
        return current_monday()
    return parsed - timedelta(days=parsed.weekday())


def _teacher_weekday_choices() -> list[tuple[int, str]]:
    weekday_numbers = list(
        TimeSlot.objects.order_by('weekday').values_list('weekday', flat=True).distinct()
    )
    if not weekday_numbers:
        weekday_numbers = [choice.value for choice in Weekday]
    return [(weekday, _weekday_label(weekday)) for weekday in weekday_numbers]


def _weekday_label(weekday: int) -> str:
    try:
        return str(Weekday(weekday).label)
    except ValueError:
        return f'День {weekday}'


def _teacher_day_statuses(
    *,
    teacher: Teacher,
    week_start: date,
    weekday_numbers: list[int],
) -> tuple[dict[int, str], list[int]]:
    slots = list(
        TimeSlot.objects.filter(weekday__in=weekday_numbers)
        .order_by('weekday', 'lesson_time__lesson_number')
    )
    slots_by_weekday: dict[int, list[int]] = defaultdict(list)
    for slot in slots:
        slots_by_weekday[slot.weekday].append(slot.id)

    availability_map = {
        time_slot_id: status
        for (teacher_id, time_slot_id), status in teacher_availability_map_for_week(
            week_start=week_start,
            teacher_ids=[teacher.pk],
            time_slot_ids=[slot.id for slot in slots],
        ).items()
        if teacher_id == teacher.pk
    }

    day_statuses: dict[int, str] = {}
    mixed_days: list[int] = []
    for weekday in weekday_numbers:
        slot_ids = slots_by_weekday.get(weekday, [])
        if not slot_ids:
            day_statuses[weekday] = AvailabilityStatus.WORKING
            continue
        statuses = {availability_map.get(slot_id, AvailabilityStatus.WORKING) for slot_id in slot_ids}
        if len(statuses) == 1:
            day_statuses[weekday] = next(iter(statuses))
            continue
        mixed_days.append(weekday)
        if AvailabilityStatus.SICK in statuses and AvailabilityStatus.WORKING not in statuses:
            day_statuses[weekday] = AvailabilityStatus.SICK
        elif AvailabilityStatus.DAY_OFF in statuses and AvailabilityStatus.WORKING not in statuses:
            day_statuses[weekday] = AvailabilityStatus.DAY_OFF
        else:
            day_statuses[weekday] = AvailabilityStatus.WORKING
    return day_statuses, mixed_days


def _save_teacher_weekday_statuses(*, teacher: Teacher, week_start: date, day_statuses: dict[int, str]) -> None:
    normalized_week_start = normalize_week_start(week_start)
    slots = list(
        TimeSlot.objects.filter(weekday__in=list(day_statuses.keys()))
        .order_by('weekday', 'lesson_time__lesson_number')
    )
    existing_week = {
        item.time_slot_id: item
        for item in TeacherAvailability.objects.filter(
            teacher=teacher,
            week_start=normalized_week_start,
            time_slot_id__in=[slot.id for slot in slots],
        )
    }
    base_statuses = {
        item.time_slot_id: item.status or (AvailabilityStatus.WORKING if item.is_available else AvailabilityStatus.DAY_OFF)
        for item in TeacherAvailability.objects.filter(
            teacher=teacher,
            week_start__isnull=True,
            time_slot_id__in=[slot.id for slot in slots],
        )
    }

    to_create: list[TeacherAvailability] = []
    to_update: list[TeacherAvailability] = []
    to_delete: list[int] = []
    for slot in slots:
        status = day_statuses.get(slot.weekday, AvailabilityStatus.WORKING)
        base_status = base_statuses.get(slot.id, AvailabilityStatus.WORKING)
        is_available = status == AvailabilityStatus.WORKING
        current = existing_week.get(slot.id)
        if status == base_status:
            if current is not None:
                to_delete.append(current.pk)
            continue
        if current is None:
            to_create.append(
                TeacherAvailability(
                    teacher=teacher,
                    time_slot=slot,
                    week_start=normalized_week_start,
                    is_available=is_available,
                    status=status,
                )
            )
            continue
        if current.status != status or current.is_available != is_available:
            current.status = status
            current.is_available = is_available
            to_update.append(current)

    with transaction.atomic():
        if to_delete:
            TeacherAvailability.objects.filter(pk__in=to_delete).delete()
        if to_create:
            TeacherAvailability.objects.bulk_create(to_create)
        if to_update:
            TeacherAvailability.objects.bulk_update(to_update, fields=['status', 'is_available', 'week_start'])


def _dashboard_summary(week_start: date | None = None) -> dict:
    week_start = week_start or current_monday()
    return {
        'classes': Class.objects.count(),
        'teachers': Teacher.objects.count(),
        'lessons_this_week': Schedule.objects.filter(
            lesson_date__gte=week_start,
            lesson_date__lt=week_start + timedelta(days=5),
        ).count(),
        'locked_lessons': Schedule.objects.filter(is_locked=True).count(),
    }


def _posted_week_start(request: HttpRequest) -> date:
    raw_week_start = (request.POST.get('week_start') or '').strip()
    if not raw_week_start:
        return current_monday()
    try:
        parsed = date.fromisoformat(raw_week_start)
    except ValueError:
        return current_monday()
    return parsed - timedelta(days=parsed.weekday())


def _posted_generation_class_ids(request: HttpRequest) -> list[int]:
    selected: list[int] = []
    for raw in request.POST.getlist('classes'):
        try:
            class_id = int(raw)
        except (TypeError, ValueError):
            continue
        if class_id not in selected:
            selected.append(class_id)
    return selected


def _build_workload_rows(week_start: date, class_ids: list[int] | None = None) -> list[dict]:
    class_subjects_qs = ClassSubject.objects.select_related('class_obj', 'subject').order_by(
        'class_obj__grade',
        'class_obj__parallel',
        'subject__name',
    )
    if class_ids:
        class_subjects_qs = class_subjects_qs.filter(class_obj_id__in=class_ids)

    class_subjects = list(class_subjects_qs)
    if not class_subjects:
        return []

    overrides_map = dict(
        WeeklyClassSubjectLoad.objects.filter(
            week_start=week_start,
            class_subject_id__in=[item.id for item in class_subjects],
        ).values_list('class_subject_id', 'weekly_hours')
    )

    rows = []
    for item in class_subjects:
        effective_hours = overrides_map.get(item.id, item.weekly_hours)
        rows.append(
            {
                'class_subject_id': item.id,
                'class_id': item.class_obj_id,
                'class_name': item.class_obj.name,
                'subject_name': item.subject.name,
                'base_weekly_hours': item.weekly_hours,
                'weekly_hours': effective_hours,
                'has_override': item.id in overrides_map,
            }
        )
    return rows


def _build_workload_classes(week_start: date) -> list[dict]:
    classes: dict[int, dict] = {}
    for row in _build_workload_rows(week_start=week_start):
        class_item = classes.get(row['class_id'])
        if class_item is None:
            class_item = {
                'class_id': row['class_id'],
                'class_name': row['class_name'],
                'subjects': [],
            }
            classes[row['class_id']] = class_item
        class_item['subjects'].append(row)
    return list(classes.values())


def _apply_weekly_workload_overrides(
    request: HttpRequest,
    week_start: date,
    class_ids: list[int] | None,
    save_as_default: bool,
) -> tuple[int, list[str]]:
    class_subjects_qs = ClassSubject.objects.select_related('class_obj', 'subject')
    if class_ids:
        class_subjects_qs = class_subjects_qs.filter(class_obj_id__in=class_ids)

    class_subjects = list(class_subjects_qs)
    if not class_subjects:
        return 0, []

    warnings: list[str] = []
    payload: dict[int, int] = {}
    for item in class_subjects:
        field_name = f'load_{item.id}'
        if field_name not in request.POST:
            continue
        raw_value = (request.POST.get(field_name) or '').strip()
        if raw_value == '':
            continue
        try:
            value = int(raw_value)
        except ValueError:
            warnings.append(f'Нагрузка "{item.class_obj.name} / {item.subject.name}" пропущена: нужно целое число.')
            continue
        if value < 0:
            warnings.append(f'Нагрузка "{item.class_obj.name} / {item.subject.name}" не может быть отрицательной.')
            continue
        if value > 40:
            warnings.append(f'Нагрузка "{item.class_obj.name} / {item.subject.name}" слишком большая (максимум 40).')
            continue
        payload[item.id] = value

    if not payload:
        return 0, warnings

    changed = 0
    with transaction.atomic():
        existing = {
            item.class_subject_id: item
            for item in WeeklyClassSubjectLoad.objects.select_for_update().filter(
                week_start=week_start,
                class_subject_id__in=payload.keys(),
            )
        }
        to_create = []
        to_update = []
        for class_subject in class_subjects:
            hours = payload.get(class_subject.id)
            if hours is None:
                continue
            existing_item = existing.get(class_subject.id)
            if existing_item:
                if existing_item.weekly_hours != hours:
                    existing_item.weekly_hours = hours
                    to_update.append(existing_item)
            else:
                to_create.append(
                    WeeklyClassSubjectLoad(
                        week_start=week_start,
                        class_subject_id=class_subject.id,
                        weekly_hours=hours,
                    )
                )

        if to_create:
            WeeklyClassSubjectLoad.objects.bulk_create(to_create)
            changed += len(to_create)
        if to_update:
            WeeklyClassSubjectLoad.objects.bulk_update(to_update, fields=['weekly_hours'])
            changed += len(to_update)

        if save_as_default:
            base_updates = []
            for class_subject in class_subjects:
                hours = payload.get(class_subject.id)
                if hours is None:
                    continue
                if class_subject.weekly_hours != hours:
                    class_subject.weekly_hours = hours
                    base_updates.append(class_subject)
            if base_updates:
                ClassSubject.objects.bulk_update(base_updates, fields=['weekly_hours'])

    return changed, warnings


def _filter_week_start(filter_form: ScheduleFilterForm) -> date:
    if filter_form.is_bound and hasattr(filter_form, 'cleaned_data'):
        return filter_form.cleaned_data.get('week_start') or current_monday()
    return current_monday()


def _filter_value(filter_form: ScheduleFilterForm, key: str):
    if filter_form.is_bound and hasattr(filter_form, 'cleaned_data'):
        return filter_form.cleaned_data.get(key)
    return None


def _build_timetable_scope_label(selected_class, selected_teacher) -> str:
    if selected_class and selected_teacher:
        return f'Показано расписание преподавателя {selected_teacher} для класса {selected_class.name}.'
    if selected_class:
        return f'Показано расписание класса {selected_class.name}.'
    if selected_teacher:
        return f'Показано расписание преподавателя {selected_teacher}.'
    return 'Показано общее недельное расписание.'
