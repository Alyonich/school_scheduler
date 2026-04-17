from datetime import date, timedelta
import os
from pathlib import Path
import threading

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import OperationalError, transaction
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .forms import ScheduleEntryForm, ScheduleFilterForm, ScheduleGenerationForm, current_monday
from .models import Class, ClassSubject, Schedule, ScheduleChange, ScheduleChangeType, Teacher, TimeSlot, WeeklyClassSubjectLoad, Weekday
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
        },
    )


def timetable(request: HttpRequest) -> HttpResponse:
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
    timetable_scope_label = _build_timetable_scope_label(selected_class, selected_teacher)
    workload_classes = _build_workload_classes(week_start=week_start)
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
                    'generation_mode': ScheduleGenerationForm.GenerationMode.BALANCED,
                }
            ),
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


def build_week_grid(schedules, week_start: date) -> dict:
    weekday_names = {
        1: 'Понедельник',
        2: 'Вторник',
        3: 'Среда',
        4: 'Четверг',
        5: 'Пятница',
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
        weekdays.append({
            'date': current_day,
            'weekday': current_day.isoweekday(),
            'label': f"{weekday_names[current_day.isoweekday()]}, {current_day.day:02d} {month_names[current_day.month]}",
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
