from datetime import date, timedelta

from django import forms

from .models import Class, Schedule, Teacher


def current_monday() -> date:
    today = date.today()
    return today - timedelta(days=today.weekday())


class ScheduleGenerationForm(forms.Form):
    class GenerationMode:
        FAST = 'fast'
        BALANCED = 'balanced'
        QUALITY = 'quality'
        CHOICES = [
            (FAST, 'Быстро'),
            (BALANCED, 'Сбалансированно'),
            (QUALITY, 'Максимальное качество'),
        ]

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
    generation_mode = forms.ChoiceField(
        label='Режим генерации',
        choices=GenerationMode.CHOICES,
        initial=GenerationMode.BALANCED,
        help_text='Выберите, что важнее: скорость расчёта или качество итогового расписания.'
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
        mode = self.cleaned_data['generation_mode']
        if mode == self.GenerationMode.FAST:
            return {
                'population_size': 50,
                'generations': 90,
                'mutation_rate': 0.14,
            }
        if mode == self.GenerationMode.QUALITY:
            return {
                'population_size': 140,
                'generations': 260,
                'mutation_rate': 0.22,
            }
        return {
            'population_size': 90,
            'generations': 180,
            'mutation_rate': 0.18,
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
