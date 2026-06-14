"""Тест Kempe-swap (единственное реально оставшееся улучшение этой серии).

Multi-restart, расширение hill-climb и поднятие весов были откатены —
на реальных данных они ухудшали результат и замедляли цикл.
"""

from datetime import date, time

from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import (
    Class,
    ClassSubject,
    Classroom,
    EducationLevel,
    LessonTime,
    RoomType,
    Subject,
    Teacher,
    TeacherAvailability,
    TeachingAssignment,
    TimeSlot,
    UserRole,
    Weekday,
)
from .services.schedule_generator import GeneticScheduleGenerator
from .services.schedule_generator.chromosome import Chromosome, Placement
from .services.schedule_generator.data_loader import load_generation_context


class KempeSwapTests(TestCase):
    """Сценарий: два класса делят одного учителя. Из-за конфликта move не работает,
    Kempe-swap должен сработать."""

    def setUp(self):
        self.week_start = date(2026, 4, 6)
        User = get_user_model()

        # Два класса, оба ведёт один учитель — он не может одновременно быть в обоих.
        self.class_a = Class.objects.create(
            name='7A', grade=7, parallel='A', students_count=20,
            education_level=EducationLevel.BASIC,
        )
        self.class_b = Class.objects.create(
            name='7B', grade=7, parallel='Б', students_count=20,
            education_level=EducationLevel.BASIC,
        )
        self.math = Subject.objects.create(
            name='Mathematics', required_room_type=RoomType.ORDINARY, max_lessons_per_day=2,
        )
        ClassSubject.objects.create(class_obj=self.class_a, subject=self.math, weekly_hours=2)
        ClassSubject.objects.create(class_obj=self.class_b, subject=self.math, weekly_hours=2)

        # Два кабинета — чтобы по комнатам не было пересечений.
        self.room_a = Classroom.objects.create(name='201', capacity=30, room_type=RoomType.ORDINARY)
        self.room_b = Classroom.objects.create(name='202', capacity=30, room_type=RoomType.ORDINARY)

        # 4 урока × 5 дней.
        for number, st, en in [
            (1, time(8, 30), time(9, 10)),
            (2, time(9, 20), time(10, 0)),
            (3, time(10, 10), time(10, 50)),
            (4, time(11, 0), time(11, 40)),
        ]:
            lt = LessonTime.objects.create(lesson_number=number, start_time=st, end_time=en, day_type='normal')
            for wd in [Weekday.MONDAY, Weekday.TUESDAY, Weekday.WEDNESDAY, Weekday.THURSDAY, Weekday.FRIDAY]:
                TimeSlot.objects.create(weekday=wd, lesson_time=lt)

        mu = User.objects.create_user(username='math', password='x', role=UserRole.TEACHER, full_name='Math')
        self.math_teacher = Teacher.objects.create(
            user=mu, qualification='Mathematics', workload_hours=24, max_lessons_per_day=6,
        )
        TeachingAssignment.objects.create(teacher=self.math_teacher, subject=self.math, class_obj=self.class_a, hours_per_week=2)
        TeachingAssignment.objects.create(teacher=self.math_teacher, subject=self.math, class_obj=self.class_b, hours_per_week=2)

        for slot in TimeSlot.objects.all():
            TeacherAvailability.objects.create(teacher=self.math_teacher, time_slot=slot, is_available=True)

    def test_kempe_swap_resolves_blocking_lesson(self):
        """Создаём ситуацию: класс A имеет урок на слоте 1, класс B имеет урок на слоте 3
        в понедельник — оба ведёт один учитель. Окна нет, но создадим искусственно:
        двинем урок A на слот 3 (где сейчас B). _try_kempe_swap должен поменять их местами.
        """
        ctx = load_generation_context(self.week_start, class_ids=[self.class_a.id, self.class_b.id])
        # Найдём индексы требований.
        idx_a = next(
            i for i, r in enumerate(ctx.lesson_requirements) if r.class_id == self.class_a.id
        )
        idx_b = next(
            i for i, r in enumerate(ctx.lesson_requirements) if r.class_id == self.class_b.id
        )

        mon_slot_1 = TimeSlot.objects.get(weekday=Weekday.MONDAY, lesson_time__lesson_number=1)
        mon_slot_3 = TimeSlot.objects.get(weekday=Weekday.MONDAY, lesson_time__lesson_number=3)

        placements = [None] * len(ctx.lesson_requirements)
        placements[idx_a] = Placement(time_slot_id=mon_slot_1.id, classroom_id=self.room_a.id)
        placements[idx_b] = Placement(time_slot_id=mon_slot_3.id, classroom_id=self.room_b.id)
        # Остальные требования (если есть) — раскидаем на разные дни/слоты, чтобы не было конфликтов.
        used_slots = {(self.class_a.id, mon_slot_1.id), (self.class_b.id, mon_slot_3.id)}
        used_teacher_slots = {mon_slot_1.id, mon_slot_3.id}
        all_slots = list(TimeSlot.objects.all())
        cursor = 0
        for i, req in enumerate(ctx.lesson_requirements):
            if placements[i] is not None:
                continue
            while cursor < len(all_slots):
                cand = all_slots[cursor]
                cursor += 1
                if cand.id in used_teacher_slots:
                    continue
                if (req.class_id, cand.id) in used_slots:
                    continue
                placements[i] = Placement(time_slot_id=cand.id, classroom_id=self.room_a.id)
                used_teacher_slots.add(cand.id)
                used_slots.add((req.class_id, cand.id))
                break
        chromo = Chromosome(placements=placements)

        from .services.schedule_generator.fitness import evaluate_chromosome
        evaluate_chromosome(chromo, ctx)
        baseline_hard = chromo.hard_penalty

        # Готовим candidate_domains: для каждого требования допустимы все слоты с подходящим кабинетом.
        candidate_domains = {}
        for req in ctx.lesson_requirements:
            entries = []
            for slot in all_slots:
                if (req.teacher_id, slot.id) in ctx.teacher_unavailability:
                    continue
                for room_id in [self.room_a.id, self.room_b.id]:
                    entries.append((slot.id, room_id))
            candidate_domains[req.lesson_id] = entries

        # Запускаем Kempe-swap: хотим переместить A → mon_slot_3 (где сейчас B).
        gen = GeneticScheduleGenerator(seed=42)
        result = gen._try_kempe_swap(
            chromosome=chromo,
            context=ctx,
            candidate_domains=candidate_domains,
            our_index=idx_a,
            target_slot_id=mon_slot_3.id,
        )

        self.assertIsNotNone(
            result, 'Kempe-swap должен сработать — это идеальный сценарий для него',
        )
        # После swap класс A должен оказаться на mon_slot_3, B — на mon_slot_1.
        self.assertEqual(result.placements[idx_a].time_slot_id, mon_slot_3.id)
        self.assertEqual(result.placements[idx_b].time_slot_id, mon_slot_1.id)
        # Hard penalty не должен ухудшиться.
        self.assertLessEqual(result.hard_penalty, baseline_hard)
