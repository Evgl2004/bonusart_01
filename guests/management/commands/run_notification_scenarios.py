import logging
import signal
import time
from typing import Dict, Iterable

from django.core.management.base import BaseCommand

from guests.services.notification_handler_registry import (
    DEFAULT_SCHEDULE_SCENARIO_CODES,
    run_registered_schedule_scenarios,
)
from guests.services.notification_scenarios import ScenarioRunStat

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    Воркер планового запуска автоматических NotificationScenario.

    Команда выполняет скан неактивных гостей и создаёт события в цепочке:
    NotificationScenario -> NotificationEvent -> DispatchTask.
    """

    help = (
        "Плановый запуск автоматизированных сценариев уведомлений "
        "через реестр обработчиков (code -> handler)."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.should_stop = False

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Выполнить один проход и завершить процесс.",
        )
        parser.add_argument(
            "--sleep-seconds",
            type=float,
            default=300.0,
            help="Пауза между проходами в циклическом режиме.",
        )
        parser.add_argument(
            "--limit-per-scenario",
            type=int,
            default=1000,
            help="Максимум гостей на один сценарий за проход.",
        )
        parser.add_argument(
            "--scenario-code",
            dest="scenario_codes",
            action="append",
            default=[],
            help=(
                "Код сценария для запуска. Можно передать параметр несколько раз. "
                f"По умолчанию: {', '.join(DEFAULT_SCHEDULE_SCENARIO_CODES)}."
            ),
        )

    def _setup_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:
        self.should_stop = True
        logger.info(
            "Получен сигнал %s, завершаем воркер notification_scenarios "
            "после текущего прохода.",
            signum,
        )

    def _print_stats(self, stats: Dict[str, ScenarioRunStat]) -> None:
        for scenario_code, scenario_stat in stats.items():
            self.stdout.write(
                f"[{scenario_code}] threshold={scenario_stat.inactive_days_threshold} "
                f"scanned={scenario_stat.scanned_guests} matched={scenario_stat.matched_guests} "
                f"created_tasks={scenario_stat.created_tasks} "
                f"skipped_without_coupon={scenario_stat.skipped_without_coupon} "
                f"skipped_duplicate_or_no_targets={scenario_stat.skipped_duplicate_or_no_targets}"
            )

    @staticmethod
    def _normalize_scenario_codes(raw_codes: Iterable[str]) -> list[str]:
        normalized = [str(code).strip() for code in raw_codes if str(code).strip()]
        if normalized:
            return normalized
        return list(DEFAULT_SCHEDULE_SCENARIO_CODES)

    def _run_single_iteration(self, *, scenario_codes: list[str], limit_per_scenario: int) -> Dict[str, ScenarioRunStat]:
        stats = run_registered_schedule_scenarios(
            scenario_codes=scenario_codes,
            limit_per_scenario=limit_per_scenario,
        )
        self._print_stats(stats)
        return stats

    def handle(self, *args, **options):
        self._setup_signal_handlers()

        once_mode = bool(options["once"])
        sleep_seconds = max(1.0, float(options["sleep_seconds"]))
        limit_per_scenario = max(1, int(options["limit_per_scenario"]))
        scenario_codes = self._normalize_scenario_codes(options["scenario_codes"])

        self.stdout.write(self.style.SUCCESS("Запущен worker автоматических NotificationScenario"))
        self.stdout.write(f"mode={'once' if once_mode else 'loop'}")
        self.stdout.write(f"scenario_codes={', '.join(scenario_codes)}")
        self.stdout.write(f"limit_per_scenario={limit_per_scenario}")
        self.stdout.write(f"sleep_seconds={sleep_seconds}")

        if once_mode:
            self._run_single_iteration(
                scenario_codes=scenario_codes,
                limit_per_scenario=limit_per_scenario,
            )
            return

        while not self.should_stop:
            self._run_single_iteration(
                scenario_codes=scenario_codes,
                limit_per_scenario=limit_per_scenario,
            )
            if self.should_stop:
                break
            time.sleep(sleep_seconds)
