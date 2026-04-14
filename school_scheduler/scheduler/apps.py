from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _configure_sqlite(sender, connection, **kwargs):
    if connection.vendor != 'sqlite':
        return
    with connection.cursor() as cursor:
        cursor.execute('PRAGMA journal_mode=WAL;')
        cursor.execute('PRAGMA synchronous=NORMAL;')
        cursor.execute('PRAGMA foreign_keys=ON;')
        cursor.execute('PRAGMA busy_timeout=60000;')


class SchedulerConfig(AppConfig):
    name = 'scheduler'

    def ready(self):
        connection_created.connect(
            _configure_sqlite,
            dispatch_uid='scheduler.sqlite.configure',
            weak=False,
        )
