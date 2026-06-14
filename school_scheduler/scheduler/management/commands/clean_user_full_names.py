"""Удаляет содержимое скобок из User.full_name.

Полезно для очистки исторических данных, где в имя добавлялись пометки
вида "Иванов Иван (преподаватель русского)".

Запуск:
    python manage.py clean_user_full_names                # пересмотр
    python manage.py clean_user_full_names --dry-run      # только показать, ничего не менять
"""

from __future__ import annotations

import re

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction


PARENS_RE = re.compile(r'\s*\([^()]*\)')


def strip_parens(value: str) -> str:
    """Удаляет любые «(...)» из строки и нормализует пробелы.

    Скобки удаляются итеративно — если внутри встречается вложенность,
    после удаления внутренней пары на следующей итерации удаляется внешняя.
    """
    if not value:
        return value
    cleaned = value
    while True:
        new_cleaned = PARENS_RE.sub('', cleaned)
        if new_cleaned == cleaned:
            break
        cleaned = new_cleaned
    return re.sub(r'\s+', ' ', cleaned).strip()


class Command(BaseCommand):
    help = "Удаляет содержимое скобок из User.full_name (например, '(преподаватель)')."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Только показать, что изменится, ничего не сохранять.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        User = get_user_model()
        dry_run = bool(options.get('dry_run'))

        affected = []
        for user in User.objects.all().only('id', 'username', 'full_name').iterator():
            original = user.full_name or ''
            cleaned = strip_parens(original)
            if cleaned != original:
                affected.append((user.id, user.username, original, cleaned))

        if not affected:
            self.stdout.write(self.style.SUCCESS('Нет пользователей со скобками в full_name — всё чисто.'))
            return

        self.stdout.write(self.style.WARNING(f'Найдено {len(affected)} пользователей со скобками:\n'))
        for user_id, username, before, after in affected:
            self.stdout.write(f'  #{user_id:<4} {username:<18} {before!r} -> {after!r}')

        if dry_run:
            self.stdout.write(self.style.NOTICE('\nDry-run: изменения не сохранены.'))
            return

        for user_id, _username, _before, after in affected:
            User.objects.filter(pk=user_id).update(full_name=after)

        self.stdout.write(self.style.SUCCESS(f'\nОбновлено {len(affected)} записей.'))
