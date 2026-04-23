from __future__ import annotations

import logging
import sys

from django.apps import AppConfig
from django.conf import settings
from django.db import ProgrammingError
from django.db import OperationalError

logger = logging.getLogger(__name__)
_DJANGO_Q_AUTOSYNC_DONE = False


def _is_qcluster_command(argv: list[str] | None = None) -> bool:
    """
    Возвращает True только для команды `manage.py qcluster`.
    """
    args = argv or list(sys.argv)
    return len(args) >= 2 and args[1] == "qcluster"


class GuestsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "guests"

    def ready(self):
        """
        На старте qcluster синхронизирует settings.Q_CLUSTER.schedule -> django_q_schedule.
        """
        global _DJANGO_Q_AUTOSYNC_DONE

        if _DJANGO_Q_AUTOSYNC_DONE:
            return
        if not _is_qcluster_command():
            return
        if not getattr(settings, "DJANGO_Q_SCHEDULE_AUTOSYNC_ON_QCLUSTER_START", True):
            return

        _DJANGO_Q_AUTOSYNC_DONE = True

        try:
            from guests.services.django_q_schedule_sync import sync_django_q_schedule_from_settings

            stats = sync_django_q_schedule_from_settings(
                dry_run=False,
                prune_stale=getattr(settings, "DJANGO_Q_SCHEDULE_AUTOSYNC_PRUNE_STALE", True),
            )
            logger.info(
                "Django Q schedule autosync on qcluster start: "
                "source=%s created=%s updated=%s unchanged=%s deleted=%s renamed=%s skipped_invalid=%s",
                stats.source_entries,
                stats.created,
                stats.updated,
                stats.unchanged,
                stats.deleted,
                stats.renamed_by_func,
                stats.skipped_invalid,
            )
        except (ProgrammingError, OperationalError):
            # Не валим старт qcluster, если БД/таблицы временно недоступны.
            logger.exception("Django Q schedule autosync skipped: database is not ready yet.")
        except Exception:
            logger.exception("Unexpected error during Django Q schedule autosync.")
