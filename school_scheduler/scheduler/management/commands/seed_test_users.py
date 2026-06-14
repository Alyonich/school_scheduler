"""Создаёт демонстрационных пользователей с разными ролями.

Запуск:
    python manage.py seed_test_users
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from scheduler.models import Teacher, UserRole

USERS_TO_CREATE = [
    # (username, password, full_name, role, is_superuser, is_staff,
    #  attach_to_teacher_id_or_None)
    ("super",      "Super#2026",       "Маслякова Татьяна",
     UserRole.ADMIN,      True,  True,  None),
    ("admin",      "Admin#2026",       "Иванова Анна Петровна",
     UserRole.ADMIN,      False, True,  None),
    ("dispatcher", "Dispatcher#2026",  "Смирнова Мария Ивановна",
     UserRole.DISPATCHER, False, False, None),
    ("teacher1",   "Teacher1#2026",    "Кузнецов Сергей Александрович",
     UserRole.TEACHER,    False, False, "first"),
    ("teacher2",   "Teacher2#2026",    "Соколова Ольга Викторовна",
     UserRole.TEACHER,    False, False, "second"),
    ("student",    "Student#2026",     "Петров Иван",
     UserRole.STUDENT,    False, False, None),
]


class Command(BaseCommand):
    help = "Создаёт демонстрационных пользователей с разными ролями"

    @transaction.atomic
    def handle(self, *args, **options):
        from django.contrib.auth import get_user_model
        User = get_user_model()

        teachers = list(Teacher.objects.select_related("user")
                        .order_by("user__full_name", "user__username"))
        teacher_first = teachers[0] if len(teachers) >= 1 else None
        teacher_second = teachers[1] if len(teachers) >= 2 else None

        results = []
        for (username, password, full_name, role,
             is_superuser, is_staff, teacher_slot) in USERS_TO_CREATE:

            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "full_name": full_name,
                    "role": role,
                    "is_superuser": is_superuser,
                    "is_staff": is_staff,
                },
            )
            # Обновляем поля на случай повторного запуска команды
            user.full_name = full_name
            user.role = role
            user.is_superuser = is_superuser
            user.is_staff = is_staff
            user.is_active = True
            user.set_password(password)
            user.save()

            # Привязываем учителей к существующим записям Teacher,
            # если у этих Teacher нет привязки или привязка к другому user.
            if role == UserRole.TEACHER:
                target_teacher = None
                if teacher_slot == "first":
                    target_teacher = teacher_first
                elif teacher_slot == "second":
                    target_teacher = teacher_second
                if target_teacher is not None and target_teacher.user_id != user.id:
                    # Если у Teacher есть собственный user другой роли, не перезаписываем,
                    # но создаём новую связь, если возможно.
                    if not Teacher.objects.filter(user=user).exists():
                        # Переподписываем существующего учителя на нового user,
                        # только если действующий user — устаревший фикстурный
                        old_user = target_teacher.user
                        if old_user and old_user.role == UserRole.TEACHER and old_user.pk != user.pk:
                            # Сохраняем старого user как обычного пользователя
                            old_user.role = UserRole.STUDENT  # понижение, чтобы не плодить TEACHER без teacher
                            old_user.save()
                        target_teacher.user = user
                        target_teacher.save()

            status = "создан" if created else "обновлён"
            results.append((username, password, role, status))

        # Лог в консоль
        self.stdout.write(self.style.SUCCESS("Демонстрационные пользователи готовы:\n"))
        self.stdout.write(f"{'Логин':<14} {'Пароль':<20} {'Роль':<12} Статус")
        self.stdout.write("-" * 60)
        for username, password, role, status in results:
            self.stdout.write(f"{username:<14} {password:<20} {role:<12} {status}")
