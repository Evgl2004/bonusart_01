import re
import logging
import requests
import time
import os

from requests import HTTPError
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from ..models import Guest, Category, GuestCategory, Restaurant, VisitHistory, GuestCategoryAssignment

from .iiko_client import iiko_client

logger = logging.getLogger(__name__)


# =====================================================================
#                     НАСТРОЙКИ SAGUR API
# =====================================================================

SAGUR_BASE_URL = os.getenv("SAGUR_BASE_URL")
SAGUR_USERNAME = os.getenv("SAGUR_USERNAME")
SAGUR_PASSWORD = os.getenv("SAGUR_PASSWORD")

PAGE_SIZE=490
LIMIT =490

ACCESS_TOKEN=None

# =====================================================================
#                     ПОЛУЧЕНИЕ ТОКЕНА
# =====================================================================
def _verify_token(token: str) -> bool:
    token_preview = f"{token[:6]}...{token[-6:]} (len={len(token)})"

    try:
        logger.info("SAGUR: проверяю токен %s", token_preview)

        resp = requests.post(
            f"{SAGUR_BASE_URL}/api/token/verify/",
            json={"token": token},
            timeout=5
        )

        if resp.status_code != 200:
            logger.warning(
                "SAGUR: verify токен %s → статус %s, ответ: %s",
                token_preview,
                resp.status_code,
                resp.text[:200],
            )

        return resp.status_code == 200

    except Exception as e:
        logger.warning(
            "SAGUR: ошибка при verify токена %s: %s",
            token_preview,
            e
        )
        return False


def _get_new_access_token() -> str:
    resp = requests.post(
        f"{SAGUR_BASE_URL}/api/token/",
        json={"username": SAGUR_USERNAME, "password": SAGUR_PASSWORD},
        timeout=10
    )
    resp.raise_for_status()
    data = resp.json()
    return data["access"]


def _get_sagur_access_token_cached() -> str:
    """
    Возвращает действующий токен.
    Запрашивает новый, только если старый умер.
    """
    global ACCESS_TOKEN

    # Есть токен? Проверяем его
    if ACCESS_TOKEN:
        if _verify_token(ACCESS_TOKEN):
            return ACCESS_TOKEN
        else:
            logger.info("SAGUR: токен не активен, запрашиваю новый...")

    # Иначе — берём новый
    ACCESS_TOKEN = _get_new_access_token()
    logger.info("SAGUR: получен новый access-токен")
    return ACCESS_TOKEN


def _iter_pending_webhooks(access_token: str, page_size: int = PAGE_SIZE):
    """
    Итерируемся по всем вебхукам с business_status=pending,
    проходя по страницам (next).
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
    }

    url = f"{SAGUR_BASE_URL}/api/internal/webhooks"
    params = {
        "business_status": "pending",
        "page_size": page_size,
    }

    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        # Вариант 1: DRF-пагинация с полем "results"
        if isinstance(data, dict) and "results" in data:
            for item in data["results"]:
                yield item
            url = data.get("next")
            params = None  # дальше всё уже в next
        # Вариант 2: просто список
        elif isinstance(data, list):
            for item in data:
                yield item
            url = None
        else:
            logger.warning("SAGUR: неожиданный формат ответа при запросе вебхуков: %s", data)
            url = None


def _update_webhook_business_status(access_token: str, webhook_id: int, status: str):
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{SAGUR_BASE_URL}/api/internal/webhooks/{webhook_id}/update/"
    payload = {"business_status": status}

    while True:  # пытаемся пока не удастся или не упадём по др. ошибке
        try:
            resp = requests.patch(url, headers=headers, json=payload, timeout=10)
            resp.raise_for_status()
            logger.info("Webhook %s → %s", webhook_id, status)
            return

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                retry_after = e.response.headers.get("Retry-After")
                if retry_after:
                    wait_time = int(retry_after)
                else:
                    wait_time = 3600  # подождать 1 час, если сервер не дал подсказку

                logger.warning(
                    "API вернул 429 (лимит). Ждём %s секунд перед повтором PATCH id=%s",
                    wait_time,
                    webhook_id
                )
                time.sleep(wait_time)
                continue

            # для других ошибок выбрасываем исключение
            raise

# =====================================================================
#                         ПОИСК ГОСТЯ
# =====================================================================
# --------- для телефона из SimplePush ---------
PHONE_RE = re.compile(r"Имя \(Guest\.Name\):\s*(\+\d+)")

def find_guest(event: dict):
    phone = event.get("phone")

    if not phone and "text" in event:
        m = PHONE_RE.search(event["text"] or "")
        if m:
            phone = m.group(1)

    guest = None

    if phone:
        guest = Guest.objects.filter(phone=phone).first()

    if not guest:
        cid = event.get("customerId")
        if cid:
            guest = Guest.objects.filter(iiko_id=cid).first()

    return guest

# =====================================================================
#                         ПОИСК ГОСТЯ в iikocard
# =====================================================================

def get_or_create_guest_from_iiko(phone: str) -> Guest | None:
    """
    Получить данные клиента из iikoCard по телефону
    и создать гостя в нашей БД, если его раньше не было.
    """

    if not phone:
        return None

    # 1. Запрашиваем iiko API
    try:
        data = iiko_client.get_customer_by_phone(phone)
    except Exception as e:
        logger.error("Ошибка запроса к iiko API: %s", e)
        return None

    if not data:
        logger.info("iiko: клиент по телефону %s не найден", phone)
        return None

    # 2. Формат может быть разным — выбираем правильный
    customer = data.get("customer") or data

    iiko_id = customer.get("id")
    if not iiko_id:
        logger.warning("iiko: у ответа нет поля id, данные: %s", customer)
        return None

    # 3. Создаём гостя или получаем существующего
    guest, created = Guest.objects.get_or_create(
        iiko_id=iiko_id,
        defaults={
            "phone": customer.get("phone"),
            "first_name": customer.get("name") or "",
            "last_name": customer.get("surname") or "",
            "email": customer.get("email") or "",
            "gender": customer.get("sex") or None,
            "birthdate": customer.get("birthdate") or None,
            "created_at": timezone.now(),
            "updated_at": timezone.now(),
        },
    )

    if created:
        logger.info("Создан новый гость из iiko: %s — %s %s",
                    phone,
                    guest.first_name,
                    guest.last_name)
    else:
        # 4. Обновляем недостающие поля, если они пустые
        updated = False

        def upd(field, value):
            nonlocal updated
            if value and not getattr(guest, field):
                setattr(guest, field, value)
                updated = True

        upd("phone", customer.get("phone"))
        upd("first_name", customer.get("name"))
        upd("last_name", customer.get("surname"))
        upd("email", customer.get("email"))
        upd("gender", customer.get("sex"))
        upd("birthdate", customer.get("birthdate"))

        if updated:
            guest.updated_at = timezone.now()
            guest.save()
            logger.info("Обновлен существующий гость из iiko: %s", phone)

    return guest



# =====================================================================
#                  ПРИМЕНЕНИЕ КАТЕГОРИЙ К ГОСТЮ
# =====================================================================
def apply_category_from_api_webhook(webhook: dict) -> bool:
    """
    Обработка одного вебхука, полученного из SAGUR API.

    webhook – объект из списка "results" ответа /api/internal/webhooks
    (включая поля category_id_ext, parsed_body, и т.п.).

    Возвращает:
        True  - если категория гостю была назначена,
        False - если ничего не сделали (пропущен/ошибка/нет данных).
    """
    webhook_id = webhook.get("id")
    event = webhook.get("parsed_body") or {}

    # 0) Обрабатываем только notificationType == 5
    notif_type = event.get("notificationType")
    if notif_type != 5:
        logger.info(
            "Webhook id=%s: notificationType=%s (пропуск, ожидаем 5)",
            webhook_id,
            notif_type,
        )
        return False

    logger.info("Webhook id=%s: notificationType=5, назначаем категорию гостю", webhook_id)

    # 1) Находим/создаём гостя
    guest = find_guest(event)
    if not guest and event.get("phone"):
        guest = get_or_create_guest_from_iiko(event["phone"])

    if not guest:
        logger.warning(
            "Category webhook id=%s: гость не найден (phone=%s, customerId=%s)",
            webhook_id,
            event.get("phone"),
            event.get("customerId"),
        )
        return False

    # 2) Находим категорию по category_id_ext (external_id в нашей БД)
    category_ext_id = webhook.get("category_id_ext")
    if not category_ext_id:
        logger.info(
            "Category webhook id=%s: нет category_id_ext, пропускаем (guest_id=%s)",
            webhook_id,
            guest.id,
        )
        return False

    try:
        cat = Category.objects.get(external_id=category_ext_id, is_active=True)
    except Category.DoesNotExist:
        logger.warning(
            "Category webhook id=%s: категория external_id=%s не найдена/неактивна (guest_id=%s)",
            webhook_id,
            category_ext_id,
            guest.id,
        )
        return False

    # 3) Определяем ресторан
    terminal_group_id = event.get("terminalGroupId")
    organization_id = event.get("organizationId")
    restaurant_iiko_id = terminal_group_id or organization_id

    restaurant = None
    if restaurant_iiko_id:
        restaurant = Restaurant.objects.filter(iiko_id=restaurant_iiko_id).first()
        if restaurant:
            logger.info(
                "Category webhook id=%s: найден ресторан (restaurant_id=%s, iiko_id=%s, name=%s)",
                webhook_id,
                restaurant.id,
                restaurant.iiko_id,
                restaurant.name,
            )
        else:
            logger.warning(
                "Category webhook id=%s: ресторан iiko_id=%s не найден, запишем событие без ресторана (guest_id=%s)",
                webhook_id,
                restaurant_iiko_id,
                guest.id,
            )
    else:
        logger.info(
            "Category webhook id=%s: terminalGroupId/organizationId отсутствуют, ресторан не определён (guest_id=%s)",
            webhook_id,
            guest.id,
        )

    # 4) Время события (если пришло changedOn — берём его, иначе now)
    changed_on = event.get("changedOn")
    if changed_on:
        try:
            assigned_at = datetime.fromisoformat(changed_on)
        except ValueError:
            logger.warning(
                "Category webhook id=%s: не удалось распарсить changedOn=%s, используем now()",
                webhook_id,
                changed_on,
            )
            assigned_at = timezone.now()
    else:
        assigned_at = timezone.now()

    if timezone.is_naive(assigned_at):
        assigned_at = timezone.make_aware(assigned_at, timezone.get_current_timezone())

    # Главный человекочитаемый лог (как “раньше”, но с рестораном)
    logger.info(
        "Category webhook id=%s: назначаем категорию '%s' (category_id=%s, external_id=%s) "
        "гостю id=%s, phone=%s из ресторана '%s' (restaurant_id=%s, iiko_id=%s) at=%s",
        webhook_id,
        cat.name,
        cat.id,
        getattr(cat, "external_id", None),
        guest.id,
        guest.phone,
        getattr(restaurant, "name", None),
        getattr(restaurant, "id", None),
        getattr(restaurant, "iiko_id", None),
        assigned_at.isoformat(),
    )

    # 5) Пишем событие в лог + обновляем агрегат GuestCategory
    with transaction.atomic():
        # 5.1) ЛОГ (второй вариант): сохраняем откуда (restaurant) и когда назначили
        logger.info(
            "Category webhook id=%s: создаём GuestCategoryAssignment "
            "(guest_id=%s, phone=%s, category='%s'[%s], restaurant='%s'[%s], assigned_at=%s)",
            webhook_id,
            guest.id,
            guest.phone,
            cat.name,
            cat.id,
            getattr(restaurant, "name", None),
            getattr(restaurant, "id", None),
            assigned_at.isoformat(),
        )

        GuestCategoryAssignment.objects.create(
            guest=guest,
            category=cat,
            restaurant=restaurant,  # может быть None
            assigned_at=assigned_at,
        )

        # 5.2) Агрегат (как было): обновляем last_assigned_at и assign_count
        gc, created = GuestCategory.objects.get_or_create(
            guest=guest,
            category=cat,
            defaults={
                "last_assigned_at": assigned_at,
                "assign_count": 1,
                # Если у вас добавлено поле last_restaurant/last_restaurant_id — можно раскомментировать:
                # "last_restaurant": restaurant,
            },
        )

        if created:
            logger.info(
                "Category webhook id=%s: создан агрегат GuestCategory "
                "(guest_id=%s, phone=%s, category='%s'[%s], assign_count=1, last_assigned_at=%s, last_restaurant_id=%s)",
                webhook_id,
                guest.id,
                guest.phone,
                cat.name,
                cat.id,
                assigned_at.isoformat(),
                getattr(restaurant, "id", None),
            )
        else:
            old_count = gc.assign_count or 0
            gc.assign_count = old_count + 1
            gc.last_assigned_at = assigned_at

            # Если в модели GuestCategory есть поле last_restaurant (FK) — обновляйте его тоже:
            if hasattr(gc, "last_restaurant_id"):
                gc.last_restaurant = restaurant

            update_fields = ["assign_count", "last_assigned_at"]
            if hasattr(gc, "last_restaurant_id"):
                update_fields.append("last_restaurant")

            gc.save(update_fields=update_fields)

            logger.info(
                "Category webhook id=%s: обновлён агрегат GuestCategory "
                "(guest_id=%s, category='%s' (id=%s), assign_count %s->%s, "
                "last_assigned_at=%s, last_restaurant_id=%s)",
                webhook_id,
                guest.id,
                cat.name,
                cat.id,
                old_count,
                gc.assign_count,
                assigned_at.isoformat(),
                getattr(restaurant, "id", None),
            )

    return True


# =====================================================================
#                  ПРИМЕНЕНИЕ ПОСЕЩЕНИЯ  ГОСТЮ
# =====================================================================
def update_visit_history_from_event(event: dict) -> bool:
    """
    Обновляет историю посещений гостя (VisitHistory) из вебхука notificationType=1.

    Сопоставление:
      - гость: по phone (и при необходимости по customerId -> Guest.iiko_id)
      - ресторан: по terminalGroupId (или organizationId) -> Restaurant.iiko_id
    """
    phone = event.get("phone")
    customer_id = event.get("customerId")
    terminal_group_id = event.get("terminalGroupId")  # предполагаем, что это iiko_id ресторана
    organization_id = event.get("organizationId")     # запасной вариант

    # 1. Ищем гостя
    guest = None

    if phone:
        # сначала пробуем найти в своей БД
        guest = Guest.objects.filter(phone=phone).first()

        # если не нашли — пытаемся подтянуть/создать гостя из iikocard
        if not guest:
            guest = get_or_create_guest_from_iiko(phone)

    if not guest and customer_id:
        guest = Guest.objects.filter(iiko_id=customer_id).first()

    if not guest:
        logger.warning(
            "Гость не найден для notificationType=1: phone=%s, customerId=%s",
            phone,
            customer_id,
        )
        return False

    # 2. Ищем ресторан по iiko_id
    restaurant_iiko_id = terminal_group_id or organization_id
    if not restaurant_iiko_id:
        logger.warning(
            "Не указан идентификатор ресторана (terminalGroupId/organizationId) "
            "для гостя id=%s, phone=%s",
            guest.id,
            guest.phone,
        )
        return False

    try:
        restaurant = Restaurant.objects.get(iiko_id=restaurant_iiko_id)
    except Restaurant.DoesNotExist:
        logger.warning(
            "Ресторан с iiko_id=%s не найден в БД для гостя id=%s, phone=%s",
            restaurant_iiko_id,
            guest.id,
            guest.phone,
        )
        return False

    # 3. Парсим дату визита
    changed_on = event.get("changedOn")
    if changed_on:
        try:
            visit_dt = datetime.fromisoformat(changed_on)
        except ValueError:
            logger.warning(
                "Не удалось распарсить changedOn=%s, используем текущее время",
                changed_on,
            )
            visit_dt = timezone.now()
    else:
        visit_dt = timezone.now()

    # делаем дату aware, если она naive
    if timezone.is_naive(visit_dt):
        visit_dt = timezone.make_aware(visit_dt, timezone.get_current_timezone())

    # 4. Обновляем/создаём VisitHistory
    vh, created = VisitHistory.objects.get_or_create(
        guest=guest,
        restaurant=restaurant,
        defaults={
            "visit_date": visit_dt,
            "visit_count": 1,  # первый визит
        },
    )

    if not created:
        old_dt = vh.visit_date

        # увеличиваем счётчик посещений всегда при новом событии
        vh.visit_count = (vh.visit_count or 0) + 1

        # дата последнего визита — только если пришёл более поздний визит
        if visit_dt > old_dt:
            vh.visit_date = visit_dt
            vh.save(update_fields=["visit_date", "visit_count"])
            logger.info(
                "Обновлена дата последнего визита гостя id=%s в ресторан %s: %s → %s "
                "(visit_count=%s)",
                guest.id,
                restaurant.name,
                old_dt,
                visit_dt,
                vh.visit_count,
            )
        else:
            vh.save(update_fields=["visit_count"])
            logger.info(
                "Получен визит %s (не новее текущего %s), увеличен visit_count=%s. "
                "guest_id=%s, restaurant=%s",
                visit_dt,
                old_dt,
                vh.visit_count,
                guest.id,
                restaurant.name,
            )
    else:
        logger.info(
            "Создана запись визита: guest_id=%s, restaurant=%s, visit_date=%s, visit_count=1",
            guest.id,
            restaurant.name,
            visit_dt,
        )


    return True


def handle_api_webhook(webhook: dict) -> bool:
    """
    Центральный обработчик вебхука из SAGUR API.

    Внутри по notificationType решаем, что делать:
      - 1  -> обновляем историю посещений (VisitHistory)
      - 5  -> назначаем категорию гостю
      - иначе -> пока ничего не делаем

    Возвращает:
        True  - вебхук считается успешно обработанным, можно ставить business_status='complete'
        False - вебхук оставляем pending (например, гость/ресторан не найден, ошибка и т.п.)
    """
    event = webhook.get("parsed_body") or {}
    notif_type = event.get("notificationType")
    webhook_id = webhook.get("id")

    # --- notificationType = 1: обновляем историю посещений ---
    if notif_type == 1:
        logger.info(
            "Webhook id=%s: notificationType=1, обновляем историю посещений",
            webhook_id,
        )
        return update_visit_history_from_event(event)

    # --- notificationType = 5: назначаем категорию ---
    if notif_type == 5:
        logger.info(
            "Webhook id=%s: notificationType=5, назначаем категорию гостю",
            webhook_id,
        )
        return apply_category_from_api_webhook(webhook)

    # --- остальные типы пока не обрабатываем специально ---
    logger.info(
        "Webhook id=%s: неизвестный или неиспользуемый notificationType=%s, "
        "ничего не делаем, оставляем pending",
        webhook_id,
        notif_type,
    )
    return False



# =====================================================================
#        ОБРАБОТКА ВЕБ-ХУКОВ ЧЕРЕЗ SAGUR API (business_status=pending)
# =====================================================================

def process_recent_webhooks(period_minutes=10, using="webhooks", max_retries=3, retry_delay=5):
    """
    Обработка вебхуков через SAGUR API.

    Сейчас:
      - получаем список вебхуков из SAGUR API /api/internal/webhooks
        с business_status=pending
      - обрабатываем каждый (назначаем категорию гостю ТОЛЬКО для notificationType=5)
      - помечаем его в SAGUR как business_status='complete' (если категория назначена)
        или 'failed' при ошибке.

    ВАЖНО:
      - за один запуск обрабатываем не более LIMIT вебхуков, чтобы
        воркеры могли делить работу порциями.

    Параметры period_minutes/using/max_retries оставлены для
    совместимости со старым кодом, но сейчас не используются.
    """

    try:
        access_token = _get_sagur_access_token_cached()
    except Exception as e:
        logger.error("SAGUR: не удалось получить access-токен: %s", e)
        return 0

    processed_count = 0  # сколько реально назначено категорий (complete)
    seen_count = 0       # сколько всего вебхуков мы посмотрели в этом запуске

    for webhook in _iter_pending_webhooks(access_token, page_size=PAGE_SIZE):
        if seen_count >= LIMIT:
            break

        seen_count += 1
        webhook_id = webhook.get("id")

        try:
            assigned = handle_api_webhook(webhook)

            if assigned:
                _update_webhook_business_status(access_token, webhook_id, "complete")
                processed_count += 1
            else:
                # Логическая ошибка / ничего не сделали -> считаем FAILED
                logger.warning(
                    "Webhook id=%s не обработан (assigned=False), "
                    "устанавливаем business_status='failed'",
                    webhook_id,
                )
                _update_webhook_business_status(access_token, webhook_id, "failed")

        except Exception as e:
            logger.exception(
                "Ошибка обработки вебхука id=%s: %s. Помечаем как 'failed'",
                webhook_id,
                e,
            )
            try:
                _update_webhook_business_status(access_token, webhook_id, "failed")
            except Exception as e2:
                logger.error(
                    "Доп. ошибка при обновлении статуса вебхука id=%s на 'failed': %s",
                    webhook_id,
                    e2,
                )

    logger.info(
        "process_recent_webhooks (через SAGUR API): "
        "просмотрено %s вебхуков (pending), успешно обработано (complete): %s",
        seen_count,
        processed_count,
    )
    return processed_count

