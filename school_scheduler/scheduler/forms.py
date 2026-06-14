from datetime import date, timedelta

from django import forms

from .models import AvailabilityStatus, Class, Schedule, Teacher


def current_monday() -> date:
    today = date.today()
    return today - timedelta(days=today.weekday())


class ScheduleGenerationForm(forms.Form):
    """Форма запуска генерации расписания.

    Пользователь задаёт только лимит времени работы GA в минутах.
    Поколения и популяции крутятся, пока:
      * не найдена «идеальная» хромосома (hard_penalty=0 и нет окон между уроками), или
      * не истёк лимит времени.
    Лимит относится ИСКЛЮЧИТЕЛЬНО к циклу GA — CSP-инициализация,
    финальный repair и сохранение в БД идут отдельно.
    """

    week_start = forms.DateField(
        label='Начало недели',
        initial=current_monday,
        input_formats=['%Y-%m-%d'],
        widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'})
    )
    classes = forms.ModelMultipleChoiceField(
        label='Классы',
        queryset=Class.objects.order_by('grade', 'parallel'),
        required=False,
        help_text='Оставьте пустым, чтобы сгенерировать расписание для всех классов.'
    )
    time_limit_minutes = forms.FloatField(
        label='Лимит времени работы GA, минут',
        min_value=0.5,
        max_value=120.0,
        initial=5.0,
        help_text=(
            'Сколько минут разрешено перебирать популяции. '
            'Если идеальное расписание найдётся раньше — генерация остановится досрочно. '
            'Если время выйдет — возьмём лучший на тот момент вариант.'
        ),
    )

    save_workload_as_default = forms.BooleanField(
        label='Сохранить нагрузку как базовую',
        required=False,
        help_text='Если включено, введённые часы по предметам станут базовыми и для следующих недель.'
    )

    def clean_week_start(self):
        value = self.cleaned_data['week_start']
        return value - timedelta(days=value.weekday())

    def get_generator_settings(self) -> dict[str, int | float]:
        """Параметры для GeneticScheduleGenerator.

        Поскольку остановка теперь по времени, population_size и generations
        выставлены умышленно с большим запасом — реально ограничителем будет
        ga_time_limit_seconds.
        """
        minutes = float(self.cleaned_data['time_limit_minutes'])
        return {
            'population_size': 120,
            'generations': 100_000,
            'mutation_rate': 0.18,
            'ga_time_limit_seconds': minutes * 60.0,
        }


class ScheduleFilterForm(forms.Form):
    week_start = forms.DateField(
        label='Начало недели',
        initial=current_monday,
        input_formats=['%Y-%m-%d'],
        widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}),
        required=False
    )
    class_obj = forms.ModelChoiceField(
        label='Класс',
        queryset=Class.objects.order_by('grade', 'parallel'),
        required=False
    )
    teacher = forms.ModelChoiceField(
        label='Преподаватель',
        queryset=Teacher.objects.select_related('user').order_by('user__full_name', 'user__username'),
        required=False
    )

    def clean_week_start(self):
        value = self.cleaned_data.get('week_start') or current_monday()
        return value - timedelta(days=value.weekday())


class ScheduleEntryForm(forms.ModelForm):
    lesson_date = forms.DateField(
        input_formats=['%Y-%m-%d'],
        widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'})
    )

    class Meta:
        model = Schedule
        fields = [
            'class_obj',
            'subject',
            'teacher',
            'classroom',
            'time_slot',
            'lesson_date',
            'is_locked',
            'note',
        ]
        widgets = {
            'note': forms.TextInput(attrs={'placeholder': 'Необязательный комментарий или причина ручного изменения'}),
        }


class TeacherWeeklyAvailabilityForm(forms.Form):
    STATUS_CHOICES = [
        (AvailabilityStatus.WORKING, 'Работает'),
        (AvailabilityStatus.DAY_OFF, 'Не работает'),
        (AvailabilityStatus.SICK, 'Болеет'),
    ]

    def __init__(
        self,
        *args,
        weekday_choices: list[tuple[int, str]] | tuple[tuple[int, str], ...],
        initial_statuses: dict[int, str] | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        initial_statuses = initial_statuses or {}
        self.weekday_numbers = [weekday for weekday, _label in weekday_choices]
        for weekday, label in weekday_choices:
            self.fields[f'weekday_{weekday}'] = forms.ChoiceField(
                label=label,
                choices=self.STATUS_CHOICES,
                initial=initial_statuses.get(weekday, AvailabilityStatus.WORKING),
            )

    def cleaned_statuses(self) -> dict[int, str]:
        return {
            weekday: self.cleaned_data[f'weekday_{weekday}']
            for weekday in self.weekday_numbers
            if f'weekday_{weekday}' in self.cleaned_data
        }
