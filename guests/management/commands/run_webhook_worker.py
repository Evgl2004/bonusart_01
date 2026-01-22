import logging
import signal
import sys
from typing import NoReturn
from django.core.management.base import BaseCommand
from django.core.management import color
from guests.services.webhook_worker import WebhookWorker

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    Управляющая команда Django для запуска простого Обработчика Уведомлений.
    Использование: python manage.py run_webhook_worker
    """
    help = "Запускает Обработчик для обработки очереди Уведомлений из Redis."

    # Коды возврата для различных ситуаций
    EXIT_SUCCESS = 0
    EXIT_FAILURE = 1
    EXIT_SIGNAL = 130  # 128 + SIGINT(2) - стандартный код для Ctrl+C

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.worker = None
        self.should_stop = False
        self.style = color.color_style()

    def add_arguments(self, parser):
        """
        Добавляем аргументы командной строки для гибкости.
        """

        parser.add_argument(
            '--health-check',
            action='store_true',
            help='Выполнить проверку здоровья и завершить'
        )

        parser.add_argument(
            '--verbose', '-V',
            action='store_true',
            help='Подробный вывод информации'
        )

    def _setup_signal_handlers(self) -> None:
        """
        Настройка обработчиков сигналов для корректного завершения работы.
        """

        signal.signal(signal.SIGINT, self._signal_handler)  # Ctrl+C
        signal.signal(signal.SIGTERM, self._signal_handler)  # Docker/Kubernetes stop

    def _signal_handler(self, signum, frame):
        """
        Обработчик сигналов для корректного завершения.
        """

        signal_name = self._get_signal_name(signum)
        logger.info(f"Получен сигнал {signal_name}. Завершаем работу команды...")
        self.should_stop = True

        # Если Обработчик уже создан, сообщаем ему о завершении
        if hasattr(self, 'worker') and self.worker:
            self.worker.should_stop = True

    @staticmethod
    def _get_signal_name(signum: int) -> str:
        """
        Получение читаемого имени сигнала.

        Args:
            signum: Номер сигнала

        Returns:
            Строковое представление сигнала
        """

        try:
            return signal.Signals(signum).name
        except (ValueError, AttributeError):
            return f"SIGNAL({signum})"

    def handle(self, *args, **options):
        """
        Основной метод запуска Обработчика.
        """

        self.stdout.write(self.style.SUCCESS("🚀 Запуск команды для Обработки Уведомлений"))

        # Настройка обработчиков сигналов
        self._setup_signal_handlers()

        # Создание экземпляра Обработчика
        self.worker = WebhookWorker()

        # Проверка здоровья, если запрошено
        if options.get('health_check'):
            return self._run_health_check(verbose=options.get('verbose', False))

        # Нормальный режим работы
        self._run_worker(verbose=options.get('verbose', False))

    def _run_health_check(self, verbose: bool = False) -> None:
        """
        Выполнение проверки здоровья Обработки Уведомлений.

        Args:
            verbose: Флаг подробного вывода
        """
        self.stdout.write(self.style.SUCCESS("🏥 Выполнение проверки здоровья..."))

        try:
            health_status = self.worker.health_check()

            # Форматированный вывод
            status = health_status.get('status', 'unknown')

            if status == 'healthy':
                self.stdout.write(self.style.SUCCESS("✅ Обработчик здоров"))
                if verbose:
                    self._print_health_details(health_status)
                sys.exit(self.EXIT_SUCCESS)

            elif status == 'unhealthy':
                self.stdout.write(self.style.ERROR("❌ Обработчик нездоров"))
                self._print_health_details(health_status)
                sys.exit(self.EXIT_FAILURE)

            else:
                self.stdout.write(self.style.WARNING("⚠️ Статус Обработчика неизвестен"))
                self._print_health_details(health_status)
                sys.exit(self.EXIT_FAILURE)

        except Exception as err:
            self.stdout.write(self.style.ERROR(f"💥 Ошибка при проверке здоровья: {err}"))
            logger.exception("Ошибка проверки здоровья")
            sys.exit(self.EXIT_FAILURE)

    def _print_health_details(self, health_status: dict) -> None:
        """
        Вывод детальной информации о состоянии Обработчика.

        Args:
            health_status: Словарь с информацией о здоровье
        """

        self.stdout.write(f"  Статус: {health_status.get('status', 'N/A')}")
        self.stdout.write(f"  Redis подключен: {health_status.get('redis_connected', 'N/A')}")
        self.stdout.write(f"  Сообщений в очереди: {health_status.get('queue_length', 'N/A')}")
        self.stdout.write(f"  Сообщений в DLQ: {health_status.get('dlq_length', 'N/A')}")
        self.stdout.write(f"  Флаг остановки: {health_status.get('should_stop', 'N/A')}")

        # Выводим метрики, если они есть
        if 'metrics' in health_status:
            metrics = health_status['metrics']
            self.stdout.write("  Метрики:")
            self.stdout.write(f"    Обработано сообщений: {metrics.get('messages_processed', 0)}")
            self.stdout.write(f"    Ошибок: {metrics.get('messages_failed', 0)}")
            self.stdout.write(f"    В DLQ: {metrics.get('messages_dlq', 0)}")
            self.stdout.write(f"    Время работы: {metrics.get('uptime_seconds', 0):.1f} сек.")

            # Безопасное получение last_message_seconds_ago
            last_message_ago = metrics.get('last_message_seconds_ago')
            if last_message_ago is not None:
                self.stdout.write(f"    С последнего сообщения: {last_message_ago} сек.")
            else:
                self.stdout.write("    С последнего сообщения: еще не было")

        if 'error' in health_status:
            self.stdout.write(self.style.ERROR(f"  Ошибка: {health_status['error']}"))

        if 'timestamp_human' in health_status:
            self.stdout.write(f"  Время проверки: {health_status['timestamp_human']}")

    def _run_worker(self, verbose: bool = False) -> NoReturn:
        """
        Запуск Обработчика в основном режиме работы.

        Args:
            verbose: Флаг подробного вывода

        Returns:
            NoReturn: Метод не возвращает управление (вызывает sys.exit)
        """

        if verbose:
            self.stdout.write("🔍 Подробный режим включен")

        try:
            # Логируем старт
            logger.info("Запуск основного цикла Обработчика...")

            if verbose:
                self.stdout.write(self.style.SUCCESS("▶️ Обработчик запущен"))
                self.stdout.write("   Для остановки нажмите Ctrl+C или отправьте SIGTERM")
                self.stdout.write("")

            # Запускаем Обработчик
            self.worker.run()

            # Анализируем причину завершения
            if self.should_stop:
                logger.info("Обработчик завершил работу по сигналу")
                self.stdout.write(self.style.WARNING("🛑 Обработчик остановлен по сигналу"))
                sys.exit(self.EXIT_SIGNAL)

            else:
                logger.info("Обработчик завершил работу штатно")
                self.stdout.write(self.style.SUCCESS("✅ Обработчик завершил работу штатно"))
                sys.exit(self.EXIT_SUCCESS)

        except KeyboardInterrupt:
            # Дополнительная обработка Ctrl+C (если не сработал signal_handler)
            logger.info("Обработчик остановлен по KeyboardInterrupt")
            self.stdout.write(self.style.WARNING("\n⏹️ Остановлено пользователем (Ctrl+C)"))
            sys.exit(self.EXIT_SIGNAL)

        except SystemExit as err:
            # Пробрасываем SystemExit наверх
            raise

        except Exception as err:
            # Критическая ошибка
            logger.critical(f"Критическая ошибка Обработчика: {err}", exc_info=True)

            self.stdout.write(self.style.ERROR(f"💥 Критическая ошибка: {err}"))

            if verbose:
                self.stdout.write("")
                self.stdout.write(self.style.WARNING("Последняя информация о состоянии:"))
                try:
                    health = self.worker.health_check()
                    self._print_health_details(health)
                except Exception as health_err:
                    self.stdout.write(self.style.ERROR(f"Не удалось получить статус: {health_err}"))

            sys.exit(self.EXIT_FAILURE)

    def execute(self, *args, **options):
        """
        Переопределенный execute для дополнительного логирования.

        Args:
            args: Аргументы
            options: Опции

        Returns:
            Результат выполнения команды
        """

        logger.info(f"Запуск команды {self.__class__.__name__} с опциями: {options}")
        return super().execute(*args, **options)
