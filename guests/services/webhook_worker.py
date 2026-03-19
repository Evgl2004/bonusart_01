import json
import logging
import signal
import time
from typing import Optional, Dict, Any
from dataclasses import dataclass, asdict

from redis import from_url as redis_from_url
from redis import ConnectionError as redis_ConnectionError
from redis import RedisError as redis_RedisError
from django.conf import settings

from guests.services.webhooks import handle_api_webhook, _get_sagur_access_token_cached, _update_webhook_business_status

logger = logging.getLogger(__name__)


class WebhookWorkerError(Exception):
    """Базовое исключение для обработчика Уведомлений"""
    pass


class FatalMessageError(WebhookWorkerError):
    """Неустранимая ошибка обработки сообщения (повторная обработка невозможна)"""
    pass


class RetryableError(WebhookWorkerError):
    """Временная ошибка, при которой можно повторить попытку позже"""
    pass


@dataclass
class WebhookMessage:
    """
    Структурированное представление Уведомления из очереди Redis
    """

    id: Any  # Идентификатор Уведомления (может быть строкой или числом)
    category: str  # Категория сообщения для маршрутизации
    parsed_body: Dict[str, Any]  # Распарсенное тело Уведомления
    created_at: Optional[str] = None  # Время создания сообщения в источнике
    metadata: Optional[Dict[str, Any]] = None  # Дополнительные метаданные
    retry_count: int = 0  # Количество уже выполненных попыток обработки
    processing_started_at: Optional[float] = None  # Время начала текущей обработки

    @property
    def webhook_id_int(self) -> Optional[int]:
        """
        Преобразует ID Уведомления в целое число для использования в API.
        Возвращает None если преобразование невозможно.
        """
        return self._parse_webhook_id(self.id)

    @staticmethod
    def _parse_webhook_id(webhook_id_raw: Any) -> Optional[int]:
        """
        Внутренний метод для безопасного преобразования ID в int.
        Пытается преобразовать webhook_id к целому числу.
        Возвращает int или None, если преобразование невозможно.
        """

        if webhook_id_raw is None:
            return None

        try:
            # Если уже int, возвращаем как есть
            if isinstance(webhook_id_raw, int):
                return webhook_id_raw
            # Если строка, пытаемся преобразовать
            elif isinstance(webhook_id_raw, str):
                # Удаляем возможные пробелы
                cleaned = webhook_id_raw.strip()
                # Пробуем преобразовать к int
                return int(cleaned)
            # Если число с плавающей точкой, преобразуем к int
            elif isinstance(webhook_id_raw, float):
                return int(webhook_id_raw)
            else:
                logger.warning(f"Неподдерживаемый тип для webhook_id: {type(webhook_id_raw)}")
                return None
        except (ValueError, TypeError) as err:
            logger.error(f"Не удалось преобразовать webhook_id '{webhook_id_raw}' к int: {err}")
            return None


class WebhookWorker:
    """
    Обработчик Уведомлений, работающий с очередью Redis.
    Основные функции:
    1. Получение сообщений из очереди через BLPOP (блокирующий pop)
    2. Обработка сообщений с механизмом повторных попыток
    3. Отправка неустранимых ошибок в Dead Letter Queue
    4. Обновление статусов через внешний API
    """

    def __init__(self):
        """
        Инициализация обработчика: настройки, Redis клиент, обработка сигналов
        """
        # Настройки подключения к Redis из Django settings
        self.redis_url = getattr(settings, 'REDIS_QUEUE_URL', 'redis://localhost:6379/1')
        self.queue_name = getattr(settings, 'REDIS_QUEUE_NAME', 'webhook_queue')
        self.dlq_name = getattr(settings, 'REDIS_DLQ_NAME', f'{self.queue_name}_dlq')

        # Настройки повторных попыток
        self.max_retries = getattr(settings, 'MAX_RETRIES', 3)

        # Флаг для корректной остановки цикла по сигналу
        self.should_stop = False

        # Настройки повторного подключения к Redis
        self.redis_retry_delay = 1
        self.max_retry_delay = 60

        # Логирование активности (чтобы видеть, что обработчик жив)
        self.last_activity_log = time.time()
        self.activity_log_interval = getattr(settings, 'ACTIVITY_LOG_INTERVAL', 300)

        # Таймаут для BLPOP (секунды ожидания нового сообщения)
        self.blpop_timeout = getattr(settings, 'BLPOP_TIMEOUT', 2)

        # Метод для проверки зависимостей при старте
        self.dependencies_checked = False

        # Инициализация Redis клиента с параметрами
        self.redis_client = redis_from_url(
            self.redis_url,
            decode_responses=False,  # Получаем байты, работаем с байтами для сохранения кодировки
            socket_connect_timeout=10,  # Таймаут подключения
            socket_timeout=30,  # Таймаут операций
            health_check_interval=30,   # Периодическая проверка здоровья соединения
        )

        # Метрики для мониторинга
        self.metrics: Dict[str, Any] = {}
        self._init_metrics()

        logger.info(
            f"Обработчик инициализирован. "
            f"Очередь: {self.queue_name}, "
            f"DLQ: {self.dlq_name}, "
            f"Redis: {self.redis_url}"
        )
        self._setup_signal_handlers()

    def _init_metrics(self) -> None:
        """Инициализация метрик с правильными типами."""

        self.metrics = {
            'messages_processed': 0,  # int
            'messages_failed': 0,  # int
            'messages_dlq': 0,  # int
            'start_time': time.time(),  # float
            'last_message_time': None,  # Optional[float]
        }

    def _setup_signal_handlers(self):
        """
        Настройка обработчиков сигналов SIGINT (Ctrl+C) и SIGTERM (завершение процесса)
        для корректного завершения работы без потери сообщений
        """

        signal.signal(signal.SIGINT, self._signal_handler)      # Ctrl+C
        signal.signal(signal.SIGTERM, self._signal_handler)     # Docker stop

    def _signal_handler(self, signum, frame):
        """
        Обработчик сигналов. Устанавливает флаг should_stop для корректного завершения работы.
        """

        logger.info(f"Получен сигнал {signum}. Инициируем корректное завершение работы...")
        self.should_stop = True

    def _check_dependencies(self) -> bool:
        """
        Проверка доступности всех зависимостей перед началом работы.
        Возвращает True если все зависимости доступны.
        """

        try:
            # 1. Проверка Redis
            if not self.redis_client.ping():
                logger.error("Redis недоступен")
                return False

            # 2. Проверка наличия очередей
            logger.info(f"Проверка очередей: основная='{self.queue_name}', DLQ='{self.dlq_name}'")

            # 3. Проверка SAGUR токена (опционально, но полезно)
            try:
                token = _get_sagur_access_token_cached()
                if not token:
                    logger.warning("Не удалось получить токен SAGUR, но продолжаем работу")
                else:
                    logger.info("SAGUR токен доступен")
            except Exception as err:
                logger.warning(f"Проверка SAGUR токена пропущена: {err}")

            logger.info("Все зависимости проверены успешно")
            self.dependencies_checked = True
            return True

        except Exception as err:
            logger.error(f"Ошибка проверки зависимостей: {err}")
            return False

    def run(self):
        """
        Главный цикл обработки сообщений.
        Алгоритм:
        1. Проверка подключения к Redis
        2. Бесконечный цикл до получения сигнала остановки
        3. Периодическое логирование активности
        4. Ожидание сообщений из очереди
        5. Обработка с восстановлением после ошибок Redis
        """
        logger.info(f"Запуск основного цикла Обработчика. Ожидание сообщений в '{self.queue_name}'...")

        # Проверка зависимостей перед началом работы
        if not self._check_dependencies():
            logger.critical("Проверка зависимостей не пройдена. Обработчик остановлен.")
            return

        # Основной цикл обработки
        while not self.should_stop:
            try:
                # Периодическое логирование активности
                current_time = time.time()
                if current_time - self.last_activity_log > self.activity_log_interval:
                    self._log_queue_stats()
                    self.last_activity_log = current_time

                # Блокирующее получение сообщения из очереди (BLPOP)
                # BLPOP возвращает None при таймауте, что позволяет проверить should_stop
                result = self.redis_client.blpop(self.queue_name, timeout=self.blpop_timeout)

                if result is None:
                    # Таймаут — в очереди нет сообщений. Просто продолжаем цикл.
                    continue

                # Проверка структуры ответа от BLPOP.
                # BLPOP возвращает кортеж (имя_очереди, сообщение)
                if not isinstance(result, tuple) or len(result) != 2:
                    logger.error(f"Некорректный ответ от Redis BLPOP: {result}")
                    continue

                # result = (имя_очереди:bytes, сообщение:bytes)
                _, message_bytes = result
                self._process_with_retry(message_bytes)

                # Обновляем метрики
                self.metrics['messages_processed'] += 1
                self.metrics['last_message_time'] = time.time()

                # Сброс задержки переподключения при успешной обработке
                self.redis_retry_delay = 1

            except redis_ConnectionError as err:
                # Временная потеря связи с Redis. Ждем и пробуем снова.
                logger.error(f"Потеряно соединение с Redis: {err}. Повтор через {self.redis_retry_delay} сек.")
                # Экспоненциальная задержка
                self._sleep_with_stop(self.redis_retry_delay)
                self.redis_retry_delay = min(self.redis_retry_delay * 2, self.max_retry_delay)
            except KeyboardInterrupt:
                logger.info("Получен KeyboardInterrupt. Завершаем работу...")
                self.should_stop = True
            except Exception as err:
                # Ловим все остальные исключения, чтобы Обработчик не упал.
                logger.exception(f"Непредвиденная ошибка в главном цикле: {err}")
                # Пауза с проверкой флага остановки, чтобы быстрее завершаться по SIGTERM.
                self._sleep_with_stop(1)

        logger.info("Корректное завершение работы: основной цикл Обработчика остановлен.")
        self._log_final_metrics()
        self._cleanup_resources()

    def _sleep_with_stop(self, total_seconds: float) -> None:
        """
        Пауза с периодической проверкой `should_stop`.

        Позволяет воркеру быстрее завершаться по внешнему сигналу
        (например `docker compose stop`), не дожидаясь длинного sleep.
        """
        remaining = max(0.0, float(total_seconds))
        while remaining > 0 and not self.should_stop:
            step = min(0.5, remaining)
            time.sleep(step)
            remaining -= step

    def _log_final_metrics(self):
        """
        Логирование финальных метрик при завершении работы.
        """

        uptime = time.time() - self.metrics['start_time']
        logger.info(
            f"Финальная статистика: "
            f"обработано {self.metrics['messages_processed']} сообщений, "
            f"ошибок {self.metrics['messages_failed']}, "
            f"в DLQ {self.metrics['messages_dlq']}, "
            f"uptime {uptime:.1f} сек."
        )

    def _log_queue_stats(self):
        """Логирование статистики очередей для мониторинга"""
        try:
            queue_length = self.redis_client.llen(self.queue_name)
            dlq_length = self.redis_client.llen(self.dlq_name)
            logger.info(
                f"Обработчик активен. "
                f"Сообщений в очереди: {queue_length}, "
                f"в DLQ: {dlq_length}"
            )
        except Exception as err:
            logger.warning(f"Не удалось проверить длину очереди: {err}")

    def _cleanup_resources(self):
        """Корректное освобождение ресурсов при завершении работы"""
        try:
            self.redis_client.close()
            logger.info("Соединение с Redis закрыто")
        except Exception as err:
            logger.warning(f"Ошибка при закрытии соединения с Redis: {err}")

    def _process_with_retry(self, message_bytes: bytes):
        """
        Основной метод обработки сообщения с механизмом повторных попыток.
        Включает парсинг, проверку лимита попыток и обработку ошибок.
        """

        try:
            message = self._parse_message(message_bytes)
        except FatalMessageError as err:
            self.metrics['messages_failed'] += 1
            logger.error(f"Неустранимая ошибка парсинга: {err}")
            self._send_to_dlq(message_bytes, reason=str(err))
            return

        # Проверка превышения лимита повторных попыток
        if message.retry_count >= self.max_retries:
            self.metrics['messages_failed'] += 1
            logger.error(
                f"Уведомление id={message.id} превысило лимит попыток "
                f"({message.retry_count}/{self.max_retries}). Отправка в DLQ."
            )
            self._send_to_dlq(message_bytes, reason="Превышен лимит попыток")
            return

        try:
            self._process_single_message(message)
        except RetryableError as err:
            self.metrics['messages_failed'] += 1
            # Временная ошибка - отправляем сообщение на повторную обработку
            logger.warning(
                f"Временная ошибка обработки Уведомления id={message.id}: {err}. "
                f"Попытка {message.retry_count + 1}/{self.max_retries}"
            )
            self._retry_message(message, message_bytes, str(err))
        except FatalMessageError as err:
            self.metrics['messages_failed'] += 1
            # Неустранимая ошибка - отправляем в DLQ
            logger.error(f"Неустранимая ошибка обработки Уведомления id={message.id}: {err}")
            self._send_to_dlq(message_bytes, reason=str(err))
        except Exception as err:
            self.metrics['messages_failed'] += 1
            # Непредвиденная ошибка - пытаемся повторить
            logger.exception(f"Непредвиденная ошибка обработки Уведомления id={message.id}")
            self._retry_message(message, message_bytes, f"Непредвиденная ошибка: {err}")

    @staticmethod
    def _decode_message_bytes(message_bytes: bytes) -> str:
        """
        Безопасно декодирует байты входящего webhook-сообщения.

        Порядок попыток:
        1. `utf-8` (основной стандарт проекта);
        2. `utf-8-sig` (если есть BOM);
        3. `cp1251` (fallback для исторических сообщений).
        """
        decode_attempts: list[tuple[str, str]] = []

        for encoding in ("utf-8", "utf-8-sig", "cp1251"):
            try:
                decoded = message_bytes.decode(encoding)
                if encoding != "utf-8":
                    logger.warning(
                        "Webhook message decoded using fallback encoding=%s (historical compatibility mode).",
                        encoding,
                    )
                return decoded
            except UnicodeDecodeError as err:
                decode_attempts.append((encoding, str(err)))

        details = "; ".join(f"{encoding}: {error}" for encoding, error in decode_attempts)
        raise FatalMessageError(f"Не удалось декодировать сообщение: {details}")

    @staticmethod
    def _parse_message(message_bytes: bytes) -> WebhookMessage:
        """
        Парсинг сырых байтов из Redis в структурированный WebhookMessage.
        Включает декодирование, валидацию JSON и проверку обязательных полей.
        """

        try:
            # Декодирование и защита от проблем исторической кодировки.
            message_str = WebhookWorker._decode_message_bytes(message_bytes)

            # Парсинг JSON
            message_dict = json.loads(message_str)

            # Валидация обязательных полей
            required_fields = ['id', 'category', 'parsed_body']
            for field in required_fields:
                if field not in message_dict:
                    raise FatalMessageError(f"Отсутствует обязательное поле: {field}")

            # Валидация типа parsed_body
            parsed_body = message_dict.get('parsed_body')
            if not isinstance(parsed_body, dict):
                raise FatalMessageError(f"parsed_body должен быть словарем, получен {type(parsed_body)}")

            # Создание структурированного объекта сообщения
            return WebhookMessage(
                id=message_dict.get('id'),
                category=message_dict.get('category'),
                parsed_body=parsed_body,
                created_at=message_dict.get('created_at'),
                metadata=message_dict.get('metadata', {}),
                retry_count=message_dict.get('retry_count', 0),
                processing_started_at=time.time()  # Засекаем время начала обработки
            )

        except json.JSONDecodeError as err:
            raise FatalMessageError(f"Некорректный JSON: {err}")
        except KeyError as err:
            raise FatalMessageError(f"Отсутствует обязательное поле: {err}")

    def _process_single_message(self, message: WebhookMessage):
        """
        Обработка одного Уведомления:
        1. Преобразование ID в int
        2. Вызов основного обработчика
        3. Обновление статуса через API
        """

        # Преобразование ID В INT - происходит через свойство webhook_id_int
        # Свойство вызывает _parse_webhook_id, который пытается преобразовать
        # ID в int (обрабатывает строки, числа, None и т.д
        webhook_id_int = message.webhook_id_int
        if webhook_id_int is None:
            raise FatalMessageError(f"Некорректный формат webhook_id: {message.id}")

        logger.info(f"Обработка Уведомления id={webhook_id_int} (исходный: {message.id})")

        # Подготовка данных для обработчика Уведомлений
        webhook_data = {
            "id": webhook_id_int,
            "category_id_ext": message.category,
            "parsed_body": message.parsed_body,
            "created_at": message.created_at,
            "metadata": message.metadata,
        }

        # Основная обработка Уведомлений
        try:
            processed_successfully, result_message = handle_api_webhook(
                webhook_data,
                send_balance_notification=True,
            )
            if result_message:
                logger.info(f"Уведомление с id={webhook_id_int} обработано: успешно! Результат: {result_message }")
            else:
                logger.info(f"Уведомление с id={webhook_id_int} обработано: успешно!")
        except Exception as err:
            logger.error(f"Ошибка для Уведомления с id={webhook_id_int}: {err}")
            # Определяем тип ошибки
            if self._is_retryable_error(err):
                raise RetryableError(f"Ошибка в handle_api_webhook: {err}")
            else:
                raise FatalMessageError(f"Неустранимая ошибка в handle_api_webhook: {err}")

        # Обновление статуса Уведомления через внешний API
        try:
            token = _get_sagur_access_token_cached()
            status = "complete" if processed_successfully else "failed"
            _update_webhook_business_status(token, webhook_id_int, status)
            logger.info(f"Статус Уведомления id={webhook_id_int} обновлён на '{status}'.")
        except Exception as api_error:
            raise RetryableError(f"Ошибка обновления статуса: {api_error}")

    @staticmethod
    def _is_retryable_error(error: Exception) -> bool:
        """
        Эвристический метод определения временных ошибок.
        Анализирует текст исключения и его тип.
        """

        error_str = str(error).lower()

        # 429 - временная ошибка (rate limiting)
        if "429" in error_str or "too many requests" in error_str:
            return True

        # 401 - токен истёк, тоже временная (обновим и повторим)
        if "401" in error_str or "token expired" in error_str:
            return True

        error_str = str(error).lower()
        retryable_keywords = [
            'timeout', 'connection', 'network', 'temporary',
            'unavailable', 'busy', 'retry', 'gateway', 'service'
        ]
        for keyword in retryable_keywords:
            if keyword in error_str:
                return True
        # Проверка типов исключений, связанных с сетью/таймаутами
        retryable_exceptions = (ConnectionError, TimeoutError)
        return isinstance(error, retryable_exceptions)

    def _retry_message(self, message: WebhookMessage, original_bytes: bytes, reason: str):
        """
        Отправка сообщения на повторную обработку.
        Увеличивает счетчик попыток и добавляет информацию о повторной обработке.
        """

        try:
            message.retry_count += 1
            message_dict = asdict(message)
            # Удаляем временные поля, которые не нужно сохранять в очереди
            message_dict.pop('processing_started_at', None)
            message_dict.pop('webhook_id_int', None)  # property, не является полем

            # Сохраняем существующие метаданные
            if 'metadata' not in message_dict or message_dict['metadata'] is None:
                message_dict['metadata'] = {}

            # Добавляем информацию о повторной попытке
            message_dict['metadata']['last_retry_reason'] = reason
            message_dict['metadata']['last_retry_time'] = time.time()
            message_dict['metadata']['retry_count'] = message.retry_count

            # Сериализация и отправка обратно в очередь
            retry_message = json.dumps(message_dict, ensure_ascii=False).encode('utf-8')
            self.redis_client.rpush(self.queue_name, retry_message)

            logger.info(
                f"Уведомления id={message.id} отправлено на повторную обработку. "
                f"Попытка {message.retry_count}/{self.max_retries}"
            )

        except Exception as err:
            logger.error(f"Не удалось отправить Уведомления на повторную обработку: {err}")
            self._send_to_dlq(original_bytes, reason=f"Ошибка при повторной отправке: {err}")

    def _send_to_dlq(self, message_bytes: bytes, reason: str):
        """
        Отправка сообщения в Dead Letter Queue с дополнительной информацией.
        DLQ используется для анализа неустранимых ошибок.
        """

        try:
            # Формируем обогащенное сообщение для DLQ
            dlq_message = {
                'original_message': message_bytes.decode('utf-8', errors='replace'),
                'reason': reason,
                'timestamp': time.time(),
                'timestamp_human': time.strftime('%Y-%m-%d %H:%M:%S'),
                'queue_source': self.queue_name
            }
            dlq_bytes = json.dumps(dlq_message, ensure_ascii=False).encode('utf-8')
            self.redis_client.rpush(self.dlq_name, dlq_bytes)
            self.metrics['messages_dlq'] += 1
            logger.warning(f"Сообщение отправлено в DLQ. Причина: {reason}")
        except Exception as err:
            logger.error(f"Не удалось отправить сообщение в DLQ: {err}")
            # Логируем часть сообщения для отладки (без переполнения логов)
            logger.error(f"Потерянное сообщение (не удалось отправить в DLQ): {message_bytes[:500]}")

    def health_check(self) -> Dict[str, Any]:
        """
        Проверка состояния обработчика для мониторинга.
        Возвращает статус, длины очередей и другую диагностическую информацию.
        """

        try:
            # Кешируем значения для атомарности проверки
            queue_len = self.redis_client.llen(self.queue_name)
            dlq_len = self.redis_client.llen(self.dlq_name)

            try:
                is_connected = self.redis_client.ping()
                status = 'healthy' if is_connected else 'unhealthy'
            except (redis_ConnectionError, redis_RedisError, ConnectionError, TimeoutError) as err:
                logger.warning(f"Ошибка подключения к Редис: {type(err).__name__}: {err}")
                is_connected = False
                status = 'unhealthy'

            uptime = time.time() - self.metrics['start_time']

            # Безопасное вычисление времени с последнего сообщения
            last_message_time = self.metrics.get('last_message_time')
            last_message_ago: Optional[float] = None

            if last_message_time is not None and isinstance(last_message_time, (int, float)):
                last_message_ago = round(time.time() - last_message_time, 1)

            return {
                'status': status,
                'redis_connected': is_connected,
                'queue_length': queue_len,
                'dlq_length': dlq_len,
                'should_stop': self.should_stop,
                'last_activity': self.last_activity_log,
                'metrics': {
                    'messages_processed': self.metrics['messages_processed'],
                    'messages_failed': self.metrics['messages_failed'],
                    'messages_dlq': self.metrics['messages_dlq'],
                    'uptime_seconds': round(uptime, 1),
                    'last_message_seconds_ago': last_message_ago,
                },
                'timestamp': time.time(),
                'timestamp_human': time.strftime('%Y-%m-%d %H:%M:%S'),
            }
        except Exception as err:
            return {
                'status': 'error',
                'error': str(err),
                'timestamp': time.time()
            }
