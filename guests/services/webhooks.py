import re
import logging
import requests
import time
import os

from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from guests.models import Category, DispatchTask, Guest, GuestCategory, GuestCategoryAssignment, Restaurant, VisitHistory

from guests.services.iiko_client import iiko_client
from guests.services.universal_queue.notification_producer import enqueue_guest_notification_tasks

logger = logging.getLogger(__name__)


# =====================================================================
#                     НАСТРОЙКИ SAGUR API
# =====================================================================

SAGUR_BASE_URL = os.getenv("SAGUR_BASE_URL")
SAGUR_USERNAME = os.getenv("SAGUR_USERNAME")
SAGUR_PASSWORD = os.getenv("SAGUR_PASSWORD")

PAGE_SIZE= 490
LIMIT = 490
BALANCE_NOTIFICATION_CATEGORY_EXTERNAL_ID = "BSamfrT83o4Cw5ZG1m4RU7N4CtW6WR2M"

ACCESS_TOKEN = None
TOKEN_EXPIRES_AT = 0  # Время последней проверки токена


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
    global ACCESS_TOKEN, TOKEN_EXPIRES_AT

    current_time = time.time()

    # Если токен есть и еще не истек (по нашему локальному расчету) - возвращаем его
    if ACCESS_TOKEN and TOKEN_EXPIRES_AT and current_time < TOKEN_EXPIRES_AT:
        return ACCESS_TOKEN

    # Сюда попадаем, если токена нет или он просрочен.
    try:
        ACCESS_TOKEN = _get_new_access_token()
        TOKEN_EXPIRES_AT = current_time + 14 * 60
        logger.info("SAGUR: получен новый access-токен, действителен до: %s",
                   time.strftime('%H:%M:%S', time.localtime(TOKEN_EXPIRES_AT)))

        return ACCESS_TOKEN

    except Exception as err:
        logger.error(f"Не удалось получить новый токен: {err}")
        raise


def _iter_pending_webhooks(access_token: str, page_size: int = PAGE_SIZE):
    """
    Итерируемся по всем вебхукам с business_status=pending,
    проходя по страницам (next).
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
    }

    date_to = timezone.now() - timedelta(minutes=10)
    url = f"{SAGUR_BASE_URL}/api/internal/webhooks/"
    params = {
        "business_status": "pending",
        "page_size": page_size,
        "date_to": date_to.isoformat(),
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


def _update_webhook_business_status(access_token: str, webhook_id: int, status: str, error_description: str = None):
    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-Service-Name": "app_loyalty_bonus_service",
    }
    url = f"{SAGUR_BASE_URL}/api/internal/webhooks/{webhook_id}/update/"
    payload = {
        "business_status": status,
    }

    if error_description:
        payload["error_description"] = error_description[:500]  # Ограничим длину

    try:
        resp = requests.patch(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Webhook %s → %s", webhook_id, status)

    except requests.exceptions.HTTPError as err:
        # Убираем специальную обработку кода ошибки 429 (rate limiting)
        # Обрабатывается как любая другая HTTP ошибка

        if err.response.status_code == 401:
            # Ошибка 401 говорит, что токен истёк
            global ACCESS_TOKEN, TOKEN_EXPIRES_AT
            ACCESS_TOKEN = None
            TOKEN_EXPIRES_AT = 0

            logger.warning("Токен истёк при обновлении статуса %s, кэш очищен", webhook_id)
            raise
        else:
            # Для всех остальных ошибок (включая 429) логируем и пробрасываем
            logger.error(
                "Ошибка обновления статуса %s: статус %s, ответ: %s",
                webhook_id,
                err.response.status_code,
                err.response.text[:200]
            )
            raise  # Пробрасываем исключение дальше

    except Exception as err:
        logger.error("Неожиданная ошибка обновления статуса %s: %s", webhook_id, err)
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

def _is_staff_notification(event: dict) -> bool:
    """
    Определяет, является ли уведомление связанным с сотрудником (идентификатор есть, но нет телефона).
    Наличие идентификатора и отсутствие телефона - многовероятно, что это сотрудник.
    Конечно, есть гости, которые до сих пор используют магнитную карту.
    Таких гостей необходимо позже идентифицировать и добавить в исключения.
    """

    return event.get('phone') is None and event.get('customerId') is not None

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
def apply_category_from_api_webhook(webhook: dict) -> tuple[bool, str]:
    """
    Обработка одного вебхука, полученного из SAGUR API.

    webhook – объект из списка "results" ответа /api/internal/webhooks
    (включая поля category_id_ext, parsed_body, и т.п.).

    Возвращает: (assigned, reason)
        True  - если категория гостю была назначена,
        False - если ничего не сделали (пропущен/ошибка/нет данных), reason содержит причину.
    """
    webhook_id = webhook.get("id")
    event = webhook.get("parsed_body") or {}

    # Проверка на уведомление от сотрудника
    if _is_staff_notification(event):
        customer_id = event.get('customerId')
        reason = (f"Category webhook id={webhook_id} пропущен: "
                  f"гость является сотрудником (customerId={customer_id}, phone отсутствует).")
        logger.info(reason)
        return True, reason

    # 0) Обрабатываем только notificationType == 5
    notif_type = event.get("notificationType")
    if notif_type != 5:
        reason = f"Webhook id={webhook_id}: notificationType={notif_type} (пропуск, ожидаем 5)"
        logger.info(reason)
        return False, reason

    logger.info("Webhook id=%s: notificationType=5, назначаем категорию гостю", webhook_id)

    # 1) Находим/создаём гостя
    guest = find_guest(event)
    if not guest and event.get("phone"):
        guest = get_or_create_guest_from_iiko(event["phone"])

    if not guest:
        reason = (f"Category webhook id={webhook_id}: "
                  f"гость не найден (phone={event.get("phone")}, customerId={event.get("customerId")})")
        logger.warning(reason)
        return False, reason

    # 2) Находим категорию по category_id_ext (external_id в нашей БД)
    category_ext_id = webhook.get("category_id_ext")
    if not category_ext_id:
        reason = f"Category webhook id={webhook_id}: нет category_id_ext, пропускаем (guest_id={guest.id})"
        logger.info(reason)
        return False, reason

    try:
        cat = Category.objects.get(external_id=category_ext_id, is_active=True)
    except Category.DoesNotExist:
        reason = (f"Category webhook id={webhook_id}: категория external_id={category_ext_id} "
                  f"не найдена/неактивна (guest_id={guest.id})")
        logger.warning(reason)
        return False, reason

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

    return True, ""


# =====================================================================
#                  ПРИМЕНЕНИЕ ПОСЕЩЕНИЯ  ГОСТЮ
# =====================================================================
def update_visit_history_from_event(event: dict) -> tuple[bool, str]:
    """
    Обновляет историю посещений гостя (VisitHistory) из вебхука notificationType=1.

    Сопоставление:
      - гость: по phone (и при необходимости по customerId -> Guest.iiko_id)
      - ресторан: по terminalGroupId (или organizationId) -> Restaurant.iiko_id

    Возвращает: (success, reason)
       - success=True - визит обновлен
       - success=False - не обновлен, reason содержит причину
    """

    # Проверка на уведомление от сотрудника
    if _is_staff_notification(event):
        customer_id = event.get('customerId')
        reason = (f"Уведомление от сотрудника пропущено: customerId={customer_id} "
                  f"(отсутствует phone, многовероятно это сотрудник)")
        logger.info(reason)
        return True, reason

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
        reason = f"Гость не найден для notificationType=1: phone={phone}, customerId={customer_id}"
        logger.warning(reason)
        return False, reason

    # 2. Ищем ресторан по iiko_id
    restaurant_iiko_id = terminal_group_id or organization_id
    if not restaurant_iiko_id:
        reason = (f"Не указан идентификатор ресторана (terminalGroupId/organizationId) "
                  f"для гостя id={guest.id}, phone={guest.phone}")
        logger.warning(reason)
        return False, reason

    try:
        restaurant = Restaurant.objects.get(iiko_id=restaurant_iiko_id)
    except Restaurant.DoesNotExist:
        reason = (f"Ресторан с iiko_id={restaurant_iiko_id} не найден в БД "
                  f"для гостя id={guest.id}, phone={guest.phone}")
        logger.warning(reason)
        return False, reason

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

    return True, ""


def _extract_balance_change_value(event: dict) -> str | None:
    """
    Возвращает ключевое значение изменения баланса из webhook-события.

    В проде встречаются разные payload-форматы, поэтому проверяем
    несколько наиболее частых полей.
    """
    for field_name in ("changeSum", "newBalance", "balance", "sum"):
        value = event.get(field_name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _extract_category_external_id(webhook: dict, event: dict) -> str:
    """
    Извлекает внешний идентификатор категории из webhook.

    В разных payload-версиях поле может называться по-разному, поэтому
    проверяем несколько вариантов.
    """
    candidates = (
        webhook.get("category_id_ext"),
        event.get("category_id_ext"),
        event.get("categoryExternalId"),
        event.get("categoryId"),
    )
    for value in candidates:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _is_balance_webhook(webhook: dict, event: dict) -> bool:
    """
    Определяет, что webhook относится к сценарию «Баланс».

    Критерий строгий и явный:
    `category_external_id == BALANCE_NOTIFICATION_CATEGORY_EXTERNAL_ID`.
    """
    category_external_id = _extract_category_external_id(webhook, event)
    return category_external_id == BALANCE_NOTIFICATION_CATEGORY_EXTERNAL_ID


def _build_balance_notification_text(event: dict) -> str:
    """
    Формирует текст уведомления об изменении баланса.

    Приоритет:
    1. Явный текст из webhook;
    2. Автогенерация из числового значения изменения баланса.
    """
    text = str(event.get("text") or "").strip()
    if text:
        return text

    value = _extract_balance_change_value(event)
    if value is not None:
        return f"Изменение баланса: {value}"

    return "Произошло изменение баланса."


def enqueue_balance_notification_from_webhook(
    webhook: dict,
    *,
    is_enabled: bool = True,
    priority: str = DispatchTask.Priority.HIGH,
    primary_only: bool = True,
) -> int:
    """
    Явный бизнес-вызов постановки уведомления о балансе в universal queue.

    Важно:
    1. Параметр `is_enabled=False` отключает только отправку уведомления;
       остальная бизнес-обработка webhook продолжает работать.
    2. Параметры маршрутизации задаются явно из кода (без env-магии):
       priority=high, primary_only=True.
    """
    if not is_enabled:
        return 0

    event = webhook.get("parsed_body") or {}
    if not isinstance(event, dict):
        return 0

    if not _is_balance_webhook(webhook, event):
        return 0

    guest = find_guest(event)
    if not guest and event.get("phone"):
        guest = get_or_create_guest_from_iiko(event["phone"])

    if guest is None:
        logger.info("Balance webhook enqueue: гость не найден, задача не создана.")
        return 0

    message_text = _build_balance_notification_text(event)
    if not message_text:
        return 0

    webhook_id = webhook.get("id")

    return enqueue_guest_notification_tasks(
        guest=guest,
        message_text=message_text,
        source_type=DispatchTask.SourceType.WEBHOOK,
        source_key=f"balance:{webhook_id or ''}",
        priority=priority,
        primary_only=primary_only,
        payload={
            "webhook_id": webhook_id,
            "notification_type": event.get("notificationType"),
            "kind": "balance_changed",
            "event": event,
        },
    )


def handle_api_webhook(
    webhook: dict,
    *,
    send_balance_notification: bool = True,
) -> tuple[bool, str]:
    """
    Центральный обработчик webhook из SAGUR API.

    Правила маршрутизации:
    1. Если `category_id_ext` совпадает с категорией «Баланс»,
       ставим high-priority задачу уведомления в universal queue.
    2. `notificationType=1` -> обновляем историю посещений (VisitHistory).
    3. `notificationType=5` -> назначаем категорию гостю.
    4. Остальные типы пока не обрабатываем.

    Возвращает:
        True  - webhook обработан успешно, можно ставить `business_status=complete`.
        False - webhook не обработан (ошибка/недостаток данных), ставим `business_status=failed`.

    Параметры:
        send_balance_notification: включать ли постановку balance-уведомления
        в universal queue для данного вызова.
    """
    event = webhook.get("parsed_body") or {}
    notif_type = event.get("notificationType")
    webhook_id = webhook.get("id")
    is_balance_webhook = _is_balance_webhook(webhook, event if isinstance(event, dict) else {})

    # --- Явный balance-сценарий по фиксированному category external id ---
    if is_balance_webhook:
        try:
            enqueued_tasks = enqueue_balance_notification_from_webhook(
                webhook,
                is_enabled=send_balance_notification,
                priority=DispatchTask.Priority.HIGH,
                primary_only=True,
            )
            logger.info(
                "Webhook id=%s: balance-событие обработано (category_ext_id=%s), "
                "поставлено задач: %s",
                webhook_id,
                BALANCE_NOTIFICATION_CATEGORY_EXTERNAL_ID,
                enqueued_tasks,
            )
            return True, f"balance webhook processed, enqueued={enqueued_tasks}"
        except Exception:
            logger.exception(
                "Webhook id=%s: ошибка постановки balance-задач в universal queue.",
                webhook_id,
            )
            return False, "balance enqueue error"

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
    return False, f"Неизвестный notificationType={notif_type}"

# =====================================================================
#        ОБРАБОТКА ВЕБ-ХУКОВ ЧЕРЕЗ SAGUR API (business_status=pending)
# =====================================================================

def process_recent_webhooks(period_minutes=10, using="webhooks", max_retries=3, retry_delay=5):
    """
    Обработка вебхуков через SAGUR API.

    Сейчас:
      - получаем список вебхуков из SAGUR API /api/internal/webhooks
        с business_status=pending
      - обрабатываем каждый:
          * balance-событие по category_id_ext -> enqueue уведомления в universal queue
          * notificationType=1 -> обновление VisitHistory
          * notificationType=5 -> назначение категории гостю
      - помечаем его в SAGUR как business_status='complete' при успешной обработке
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
            assigned, reason = handle_api_webhook(
                webhook,
                send_balance_notification=True,
            )

            if assigned:
                processed_count += 1
                final_status = "complete"
                log_details = reason or 'Успех'
                reason = reason if reason else None
                logger.info(f"Уведомление id={webhook_id} успешно обработано! Детали: {log_details}")
            else:
                final_status = "failed"
                logger.warning(f"Уведомление id={webhook_id} не обработан: {reason}. "
                               f"Устанавливаем business_status='failed'")

            # Единый вызов для обновления статуса в БД
            _update_webhook_business_status(access_token, webhook_id, final_status, reason)

        except Exception as e:
            logger.exception(
                "Ошибка обработки вебхука id=%s: %s. Помечаем как 'failed'",
                webhook_id,
                e,
            )
            try:
                reason = f"Ошибка обработки: {str(e)[:200]}"
                _update_webhook_business_status(access_token, webhook_id, "failed", reason)
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
