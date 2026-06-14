"""Декораторы и помощники для разграничения доступа по ролям.

Роли определены в scheduler.models.UserRole:
- STUDENT     — может только смотреть и фильтровать расписание;
- TEACHER     — может смотреть расписание и менять СВОЮ доступность;
- DISPATCHER  — может всё, кроме администрирования системы;
- ADMIN       — полный доступ, включая административную панель Django.

Суперпользователь (is_superuser=True) имеет полные права независимо от поля role.
"""

from __future__ import annotations

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest
from django.shortcuts import redirect

from .models import UserRole


# ---------------------------------------------------------------------------
# Базовые проверки
# ---------------------------------------------------------------------------

def is_admin(user) -> bool:
    return user.is_authenticated and (
        user.is_superuser or user.role == UserRole.ADMIN
    )


def is_dispatcher(user) -> bool:
    """Диспетчер ИЛИ администратор/суперпользователь (admin может всё)."""
    return user.is_authenticated and (
        user.is_superuser
        or user.role == UserRole.DISPATCHER
        or user.role == UserRole.ADMIN
    )


def is_teacher(user) -> bool:
    return user.is_authenticated and (
        user.is_superuser or user.role == UserRole.TEACHER
    )


def is_student(user) -> bool:
    return user.is_authenticated and (
        user.is_superuser or user.role == UserRole.STUDENT
    )


def can_view_schedule(user) -> bool:
    """Любой авторизованный пользователь может смотреть расписание."""
    return user.is_authenticated


def can_edit_schedule(user) -> bool:
    """Создание/редактирование/удаление занятий — диспетчер и админ."""
    return is_dispatcher(user)


def can_manage_generation(user) -> bool:
    """Запуск автоматической генерации — диспетчер и админ."""
    return is_dispatcher(user)


def can_manage_directories(user) -> bool:
    """Ведение справочников и /admin/ — только админ."""
    return is_admin(user)


def can_edit_teacher_availability(user, teacher) -> bool:
    """Учитель меняет только свою доступность; диспетчер и админ — любую."""
    if not user.is_authenticated:
        return False
    if is_dispatcher(user):
        return True
    if user.role == UserRole.TEACHER and teacher.user_id == user.id:
        return True
    return False


# ---------------------------------------------------------------------------
# Декораторы для views
# ---------------------------------------------------------------------------

def role_required(*roles, allow_superuser: bool = True):
    """Декоратор, ограничивающий доступ перечнем ролей.

    Использование:
        @role_required(UserRole.DISPATCHER, UserRole.ADMIN)
        def some_view(request): ...
    """
    role_set = set(roles)

    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def _wrapped(request: HttpRequest, *args, **kwargs):
            user = request.user
            if allow_superuser and user.is_superuser:
                return view_func(request, *args, **kwargs)
            if getattr(user, "role", None) in role_set:
                return view_func(request, *args, **kwargs)
            raise PermissionDenied(
                "Недостаточно прав для выполнения этого действия."
            )
        return _wrapped
    return decorator


# Удобные псевдонимы под конкретные сценарии
dispatcher_required = role_required(UserRole.DISPATCHER, UserRole.ADMIN)
admin_required      = role_required(UserRole.ADMIN)
teacher_required    = role_required(UserRole.TEACHER, UserRole.DISPATCHER,
                                    UserRole.ADMIN)
