import re
import logging
import requests
import time
import os

from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from guests.models import Category, DispatchTask, Guest, GuestCategory, GuestCategoryAssignment, Restaurant, VisitHistory

from guests.services.balance_notifications import BALANCE_NOTIFICATION_CATEGORY_EXTERNAL_ID, is_balance_webhook
from guests.services.guest_resolution import resolve_or_create_guest
from guests.services.notification_handler_registry import run_webhook_scenario_by_code
from guests.services.notification_registry import SCENARIO_CODE_BALANCE_CHANGED
from guests.services.olap_webhook_bridge import enqueue_olap_sync_from_webhook

logger = logging.getLogger(__name__)


# =====================================================================
#                     РќРђРЎРўР РћР™РљР SAGUR API
# =====================================================================

SAGUR_BASE_URL = os.getenv("SAGUR_BASE_URL")
SAGUR_USERNAME = os.getenv("SAGUR_USERNAME")
SAGUR_PASSWORD = os.getenv("SAGUR_PASSWORD")

PAGE_SIZE= 490
LIMIT = 490

ACCESS_TOKEN = None
TOKEN_EXPIRES_AT = 0  # Р’СЂРµРјСЏ РїРѕСЃР»РµРґРЅРµР№ РїСЂРѕРІРµСЂРєРё С‚РѕРєРµРЅР°


# =====================================================================
#                     РџРћР›РЈР§Р•РќРР• РўРћРљР•РќРђ
# =====================================================================
def _verify_token(token: str) -> bool:
    token_preview = f"{token[:6]}...{token[-6:]} (len={len(token)})"

    try:
        logger.info("SAGUR: РїСЂРѕРІРµСЂСЏСЋ С‚РѕРєРµРЅ %s", token_preview)

        resp = requests.post(
            f"{SAGUR_BASE_URL}/api/token/verify/",
            json={"token": token},
            timeout=5
        )

        if resp.status_code != 200:
            logger.warning(
                "SAGUR: verify С‚РѕРєРµРЅ %s в†’ СЃС‚Р°С‚СѓСЃ %s, РѕС‚РІРµС‚: %s",
                token_preview,
                resp.status_code,
                resp.text[:200],
            )

        return resp.status_code == 200

    except Exception as e:
        logger.warning(
            "SAGUR: РѕС€РёР±РєР° РїСЂРё verify С‚РѕРєРµРЅР° %s: %s",
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
    Р’РѕР·РІСЂР°С‰Р°РµС‚ РґРµР№СЃС‚РІСѓСЋС‰РёР№ С‚РѕРєРµРЅ.
    Р—Р°РїСЂР°С€РёРІР°РµС‚ РЅРѕРІС‹Р№, С‚РѕР»СЊРєРѕ РµСЃР»Рё СЃС‚Р°СЂС‹Р№ СѓРјРµСЂ.
    """
    global ACCESS_TOKEN, TOKEN_EXPIRES_AT

    current_time = time.time()

    # Р•СЃР»Рё С‚РѕРєРµРЅ РµСЃС‚СЊ Рё РµС‰Рµ РЅРµ РёСЃС‚РµРє (РїРѕ РЅР°С€РµРјСѓ Р»РѕРєР°Р»СЊРЅРѕРјСѓ СЂР°СЃС‡РµС‚Сѓ) - РІРѕР·РІСЂР°С‰Р°РµРј РµРіРѕ
    if ACCESS_TOKEN and TOKEN_EXPIRES_AT and current_time < TOKEN_EXPIRES_AT:
        return ACCESS_TOKEN

    # РЎСЋРґР° РїРѕРїР°РґР°РµРј, РµСЃР»Рё С‚РѕРєРµРЅР° РЅРµС‚ РёР»Рё РѕРЅ РїСЂРѕСЃСЂРѕС‡РµРЅ.
    try:
        ACCESS_TOKEN = _get_new_access_token()
        TOKEN_EXPIRES_AT = current_time + 14 * 60
        logger.info("SAGUR: РїРѕР»СѓС‡РµРЅ РЅРѕРІС‹Р№ access-С‚РѕРєРµРЅ, РґРµР№СЃС‚РІРёС‚РµР»РµРЅ РґРѕ: %s",
                   time.strftime('%H:%M:%S', time.localtime(TOKEN_EXPIRES_AT)))

        return ACCESS_TOKEN

    except Exception as err:
        logger.error(f"РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕР»СѓС‡РёС‚СЊ РЅРѕРІС‹Р№ С‚РѕРєРµРЅ: {err}")
        raise


def _iter_pending_webhooks(access_token: str, page_size: int = PAGE_SIZE):
    """
    РС‚РµСЂРёСЂСѓРµРјСЃСЏ РїРѕ РІСЃРµРј РІРµР±С…СѓРєР°Рј СЃ business_status=pending,
    РїСЂРѕС…РѕРґСЏ РїРѕ СЃС‚СЂР°РЅРёС†Р°Рј (next).
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

        # Р’Р°СЂРёР°РЅС‚ 1: DRF-РїР°РіРёРЅР°С†РёСЏ СЃ РїРѕР»РµРј "results"
        if isinstance(data, dict) and "results" in data:
            for item in data["results"]:
                yield item
            url = data.get("next")
            params = None  # РґР°Р»СЊС€Рµ РІСЃС‘ СѓР¶Рµ РІ next
        # Р’Р°СЂРёР°РЅС‚ 2: РїСЂРѕСЃС‚Рѕ СЃРїРёСЃРѕРє
        elif isinstance(data, list):
            for item in data:
                yield item
            url = None
        else:
            logger.warning("SAGUR: РЅРµРѕР¶РёРґР°РЅРЅС‹Р№ С„РѕСЂРјР°С‚ РѕС‚РІРµС‚Р° РїСЂРё Р·Р°РїСЂРѕСЃРµ РІРµР±С…СѓРєРѕРІ: %s", data)
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
        payload["error_description"] = error_description[:500]  # РћРіСЂР°РЅРёС‡РёРј РґР»РёРЅСѓ

    try:
        resp = requests.patch(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Webhook %s в†’ %s", webhook_id, status)

    except requests.exceptions.HTTPError as err:
        # РЈР±РёСЂР°РµРј СЃРїРµС†РёР°Р»СЊРЅСѓСЋ РѕР±СЂР°Р±РѕС‚РєСѓ РєРѕРґР° РѕС€РёР±РєРё 429 (rate limiting)
        # РћР±СЂР°Р±Р°С‚С‹РІР°РµС‚СЃСЏ РєР°Рє Р»СЋР±Р°СЏ РґСЂСѓРіР°СЏ HTTP РѕС€РёР±РєР°

        if err.response.status_code == 401:
            # РћС€РёР±РєР° 401 РіРѕРІРѕСЂРёС‚, С‡С‚Рѕ С‚РѕРєРµРЅ РёСЃС‚С‘Рє
            global ACCESS_TOKEN, TOKEN_EXPIRES_AT
            ACCESS_TOKEN = None
            TOKEN_EXPIRES_AT = 0

            logger.warning("РўРѕРєРµРЅ РёСЃС‚С‘Рє РїСЂРё РѕР±РЅРѕРІР»РµРЅРёРё СЃС‚Р°С‚СѓСЃР° %s, РєСЌС€ РѕС‡РёС‰РµРЅ", webhook_id)
            raise
        else:
            # Р”Р»СЏ РІСЃРµС… РѕСЃС‚Р°Р»СЊРЅС‹С… РѕС€РёР±РѕРє (РІРєР»СЋС‡Р°СЏ 429) Р»РѕРіРёСЂСѓРµРј Рё РїСЂРѕР±СЂР°СЃС‹РІР°РµРј
            logger.error(
                "РћС€РёР±РєР° РѕР±РЅРѕРІР»РµРЅРёСЏ СЃС‚Р°С‚СѓСЃР° %s: СЃС‚Р°С‚СѓСЃ %s, РѕС‚РІРµС‚: %s",
                webhook_id,
                err.response.status_code,
                err.response.text[:200]
            )
            raise  # РџСЂРѕР±СЂР°СЃС‹РІР°РµРј РёСЃРєР»СЋС‡РµРЅРёРµ РґР°Р»СЊС€Рµ

    except Exception as err:
        logger.error("РќРµРѕР¶РёРґР°РЅРЅР°СЏ РѕС€РёР±РєР° РѕР±РЅРѕРІР»РµРЅРёСЏ СЃС‚Р°С‚СѓСЃР° %s: %s", webhook_id, err)
        raise


# =====================================================================
#                         РџРћРРЎРљ Р“РћРЎРўРЇ
# =====================================================================
# --------- РґР»СЏ С‚РµР»РµС„РѕРЅР° РёР· SimplePush ---------
PHONE_RE = re.compile(r"РРјСЏ \(Guest\.Name\):\s*(\+\d+)")

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
    РћРїСЂРµРґРµР»СЏРµС‚, СЏРІР»СЏРµС‚СЃСЏ Р»Рё СѓРІРµРґРѕРјР»РµРЅРёРµ СЃРІСЏР·Р°РЅРЅС‹Рј СЃ СЃРѕС‚СЂСѓРґРЅРёРєРѕРј (РёРґРµРЅС‚РёС„РёРєР°С‚РѕСЂ РµСЃС‚СЊ, РЅРѕ РЅРµС‚ С‚РµР»РµС„РѕРЅР°).
    РќР°Р»РёС‡РёРµ РёРґРµРЅС‚РёС„РёРєР°С‚РѕСЂР° Рё РѕС‚СЃСѓС‚СЃС‚РІРёРµ С‚РµР»РµС„РѕРЅР° - РјРЅРѕРіРѕРІРµСЂРѕСЏС‚РЅРѕ, С‡С‚Рѕ СЌС‚Рѕ СЃРѕС‚СЂСѓРґРЅРёРє.
    РљРѕРЅРµС‡РЅРѕ, РµСЃС‚СЊ РіРѕСЃС‚Рё, РєРѕС‚РѕСЂС‹Рµ РґРѕ СЃРёС… РїРѕСЂ РёСЃРїРѕР»СЊР·СѓСЋС‚ РјР°РіРЅРёС‚РЅСѓСЋ РєР°СЂС‚Сѓ.
    РўР°РєРёС… РіРѕСЃС‚РµР№ РЅРµРѕР±С…РѕРґРёРјРѕ РїРѕР·Р¶Рµ РёРґРµРЅС‚РёС„РёС†РёСЂРѕРІР°С‚СЊ Рё РґРѕР±Р°РІРёС‚СЊ РІ РёСЃРєР»СЋС‡РµРЅРёСЏ.
    """

    return event.get('phone') is None and event.get('customerId') is not None

# =====================================================================
#                         РџРћРРЎРљ Р“РћРЎРўРЇ РІ iikocard
# =====================================================================

def get_or_create_guest_from_iiko(phone: str) -> Guest | None:
    """
    РџРѕР»СѓС‡РёС‚СЊ РґР°РЅРЅС‹Рµ РєР»РёРµРЅС‚Р° РёР· iikoCard РїРѕ С‚РµР»РµС„РѕРЅСѓ
    Рё СЃРѕР·РґР°С‚СЊ РіРѕСЃС‚СЏ РІ РЅР°С€РµР№ Р‘Р”, РµСЃР»Рё РµРіРѕ СЂР°РЅСЊС€Рµ РЅРµ Р±С‹Р»Рѕ.
    """

    if not phone:
        return None

    try:
        from guests.services.iiko_client import iiko_client
    except Exception as e:
        logger.error("iiko-РєР»РёРµРЅС‚ РЅРµРґРѕСЃС‚СѓРїРµРЅ: %s", e)
        return None

    # 1. Р—Р°РїСЂР°С€РёРІР°РµРј iiko API
    try:
        data = iiko_client.get_customer_by_phone(phone)
    except Exception as e:
        logger.error("РћС€РёР±РєР° Р·Р°РїСЂРѕСЃР° Рє iiko API: %s", e)
        return None

    if not data:
        logger.info("iiko: РєР»РёРµРЅС‚ РїРѕ С‚РµР»РµС„РѕРЅСѓ %s РЅРµ РЅР°Р№РґРµРЅ", phone)
        return None

    # 2. Р¤РѕСЂРјР°С‚ РјРѕР¶РµС‚ Р±С‹С‚СЊ СЂР°Р·РЅС‹Рј вЂ” РІС‹Р±РёСЂР°РµРј РїСЂР°РІРёР»СЊРЅС‹Р№
    customer = data.get("customer") or data

    iiko_id = customer.get("id")
    if not iiko_id:
        logger.warning("iiko: Сѓ РѕС‚РІРµС‚Р° РЅРµС‚ РїРѕР»СЏ id, РґР°РЅРЅС‹Рµ: %s", customer)
        return None

    # 3. РЎРѕР·РґР°С‘Рј РіРѕСЃС‚СЏ РёР»Рё РїРѕР»СѓС‡Р°РµРј СЃСѓС‰РµСЃС‚РІСѓСЋС‰РµРіРѕ
    resolved = resolve_or_create_guest(
        phone=customer.get("phone") or phone,
        iiko_id=iiko_id,
        first_name=customer.get("name") or "",
        last_name=customer.get("surname") or "",
        email=customer.get("email") or "",
        gender=customer.get("sex") or None,
        birthdate=customer.get("birthdate") or None,
        allow_create=True,
        source="webhooks.iiko",
    )
    guest = resolved.guest
    if guest is None:
        return None

    if resolved.created:
        logger.info("iiko guest created: phone=%s guest_id=%s", phone, guest.id)
    elif resolved.duplicate_candidates > 0:
        logger.warning(
            "iiko guest duplicates: phone=%s duplicates=%s chosen_guest_id=%s",
            phone,
            resolved.duplicate_candidates,
            guest.id,
        )

    return guest



# =====================================================================
#                  РџР РРњР•РќР•РќРР• РљРђРўР•Р“РћР РР™ Рљ Р“РћРЎРўР®
# =====================================================================
def apply_category_from_api_webhook(webhook: dict) -> tuple[bool, str]:
    """
    РћР±СЂР°Р±РѕС‚РєР° РѕРґРЅРѕРіРѕ РІРµР±С…СѓРєР°, РїРѕР»СѓС‡РµРЅРЅРѕРіРѕ РёР· SAGUR API.

    webhook вЂ“ РѕР±СЉРµРєС‚ РёР· СЃРїРёСЃРєР° "results" РѕС‚РІРµС‚Р° /api/internal/webhooks
    (РІРєР»СЋС‡Р°СЏ РїРѕР»СЏ category_id_ext, parsed_body, Рё С‚.Рї.).

    Р’РѕР·РІСЂР°С‰Р°РµС‚: (assigned, reason)
        True  - РµСЃР»Рё РєР°С‚РµРіРѕСЂРёСЏ РіРѕСЃС‚СЋ Р±С‹Р»Р° РЅР°Р·РЅР°С‡РµРЅР°,
        False - РµСЃР»Рё РЅРёС‡РµРіРѕ РЅРµ СЃРґРµР»Р°Р»Рё (РїСЂРѕРїСѓС‰РµРЅ/РѕС€РёР±РєР°/РЅРµС‚ РґР°РЅРЅС‹С…), reason СЃРѕРґРµСЂР¶РёС‚ РїСЂРёС‡РёРЅСѓ.
    """
    webhook_id = webhook.get("id")
    event = webhook.get("parsed_body") or {}

    # РџСЂРѕРІРµСЂРєР° РЅР° СѓРІРµРґРѕРјР»РµРЅРёРµ РѕС‚ СЃРѕС‚СЂСѓРґРЅРёРєР°
    if _is_staff_notification(event):
        customer_id = event.get('customerId')
        reason = (f"Category webhook id={webhook_id} РїСЂРѕРїСѓС‰РµРЅ: "
                  f"РіРѕСЃС‚СЊ СЏРІР»СЏРµС‚СЃСЏ СЃРѕС‚СЂСѓРґРЅРёРєРѕРј (customerId={customer_id}, phone РѕС‚СЃСѓС‚СЃС‚РІСѓРµС‚).")
        logger.info(reason)
        return True, reason

    # 0) РћР±СЂР°Р±Р°С‚С‹РІР°РµРј С‚РѕР»СЊРєРѕ notificationType == 5
    notif_type = event.get("notificationType")
    if notif_type != 5:
        reason = f"Webhook id={webhook_id}: notificationType={notif_type} (РїСЂРѕРїСѓСЃРє, РѕР¶РёРґР°РµРј 5)"
        logger.info(reason)
        return False, reason

    logger.info("Webhook id=%s: notificationType=5, РЅР°Р·РЅР°С‡Р°РµРј РєР°С‚РµРіРѕСЂРёСЋ РіРѕСЃС‚СЋ", webhook_id)

    # 1) РќР°С…РѕРґРёРј/СЃРѕР·РґР°С‘Рј РіРѕСЃС‚СЏ
    guest = find_guest(event)
    if not guest and event.get("phone"):
        guest = get_or_create_guest_from_iiko(event["phone"])

    if not guest:
        reason = (f"Category webhook id={webhook_id}: "
                  f"РіРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅ (phone={event.get("phone")}, customerId={event.get("customerId")})")
        logger.warning(reason)
        return False, reason

    # 2) РќР°С…РѕРґРёРј РєР°С‚РµРіРѕСЂРёСЋ РїРѕ category_id_ext (external_id РІ РЅР°С€РµР№ Р‘Р”)
    category_ext_id = webhook.get("category_id_ext")
    if not category_ext_id:
        reason = f"Category webhook id={webhook_id}: РЅРµС‚ category_id_ext, РїСЂРѕРїСѓСЃРєР°РµРј (guest_id={guest.id})"
        logger.info(reason)
        return False, reason

    try:
        cat = Category.objects.get(external_id=category_ext_id, is_active=True)
    except Category.DoesNotExist:
        reason = (f"Category webhook id={webhook_id}: РєР°С‚РµРіРѕСЂРёСЏ external_id={category_ext_id} "
                  f"РЅРµ РЅР°Р№РґРµРЅР°/РЅРµР°РєС‚РёРІРЅР° (guest_id={guest.id})")
        logger.warning(reason)
        return False, reason

    # 3) РћРїСЂРµРґРµР»СЏРµРј СЂРµСЃС‚РѕСЂР°РЅ
    terminal_group_id = event.get("terminalGroupId")
    organization_id = event.get("organizationId")
    restaurant_iiko_id = terminal_group_id or organization_id

    restaurant = None
    if restaurant_iiko_id:
        restaurant = Restaurant.objects.filter(iiko_id=restaurant_iiko_id).first()
        if restaurant:
            logger.info(
                "Category webhook id=%s: РЅР°Р№РґРµРЅ СЂРµСЃС‚РѕСЂР°РЅ (restaurant_id=%s, iiko_id=%s, name=%s)",
                webhook_id,
                restaurant.id,
                restaurant.iiko_id,
                restaurant.name,
            )
        else:
            logger.warning(
                "Category webhook id=%s: СЂРµСЃС‚РѕСЂР°РЅ iiko_id=%s РЅРµ РЅР°Р№РґРµРЅ, Р·Р°РїРёС€РµРј СЃРѕР±С‹С‚РёРµ Р±РµР· СЂРµСЃС‚РѕСЂР°РЅР° (guest_id=%s)",
                webhook_id,
                restaurant_iiko_id,
                guest.id,
            )
    else:
        logger.info(
            "Category webhook id=%s: terminalGroupId/organizationId РѕС‚СЃСѓС‚СЃС‚РІСѓСЋС‚, СЂРµСЃС‚РѕСЂР°РЅ РЅРµ РѕРїСЂРµРґРµР»С‘РЅ (guest_id=%s)",
            webhook_id,
            guest.id,
        )

    # 4) Р’СЂРµРјСЏ СЃРѕР±С‹С‚РёСЏ (РµСЃР»Рё РїСЂРёС€Р»Рѕ changedOn вЂ” Р±РµСЂС‘Рј РµРіРѕ, РёРЅР°С‡Рµ now)
    changed_on = event.get("changedOn")
    if changed_on:
        try:
            assigned_at = datetime.fromisoformat(changed_on)
        except ValueError:
            logger.warning(
                "Category webhook id=%s: РЅРµ СѓРґР°Р»РѕСЃСЊ СЂР°СЃРїР°СЂСЃРёС‚СЊ changedOn=%s, РёСЃРїРѕР»СЊР·СѓРµРј now()",
                webhook_id,
                changed_on,
            )
            assigned_at = timezone.now()
    else:
        assigned_at = timezone.now()

    if timezone.is_naive(assigned_at):
        assigned_at = timezone.make_aware(assigned_at, timezone.get_current_timezone())

    # Р“Р»Р°РІРЅС‹Р№ С‡РµР»РѕРІРµРєРѕС‡РёС‚Р°РµРјС‹Р№ Р»РѕРі (РєР°Рє вЂњСЂР°РЅСЊС€РµвЂќ, РЅРѕ СЃ СЂРµСЃС‚РѕСЂР°РЅРѕРј)
    logger.info(
        "Category webhook id=%s: РЅР°Р·РЅР°С‡Р°РµРј РєР°С‚РµРіРѕСЂРёСЋ '%s' (category_id=%s, external_id=%s) "
        "РіРѕСЃС‚СЋ id=%s, phone=%s РёР· СЂРµСЃС‚РѕСЂР°РЅР° '%s' (restaurant_id=%s, iiko_id=%s) at=%s",
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

    # 5) РџРёС€РµРј СЃРѕР±С‹С‚РёРµ РІ Р»РѕРі + РѕР±РЅРѕРІР»СЏРµРј Р°РіСЂРµРіР°С‚ GuestCategory
    with transaction.atomic():
        # 5.1) Р›РћР“ (РІС‚РѕСЂРѕР№ РІР°СЂРёР°РЅС‚): СЃРѕС…СЂР°РЅСЏРµРј РѕС‚РєСѓРґР° (restaurant) Рё РєРѕРіРґР° РЅР°Р·РЅР°С‡РёР»Рё
        logger.info(
            "Category webhook id=%s: СЃРѕР·РґР°С‘Рј GuestCategoryAssignment "
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
            restaurant=restaurant,  # РјРѕР¶РµС‚ Р±С‹С‚СЊ None
            assigned_at=assigned_at,
        )

        # 5.2) РђРіСЂРµРіР°С‚ (РєР°Рє Р±С‹Р»Рѕ): РѕР±РЅРѕРІР»СЏРµРј last_assigned_at Рё assign_count
        gc, created = GuestCategory.objects.get_or_create(
            guest=guest,
            category=cat,
            defaults={
                "last_assigned_at": assigned_at,
                "assign_count": 1,
                # Р•СЃР»Рё Сѓ РІР°СЃ РґРѕР±Р°РІР»РµРЅРѕ РїРѕР»Рµ last_restaurant/last_restaurant_id вЂ” РјРѕР¶РЅРѕ СЂР°СЃРєРѕРјРјРµРЅС‚РёСЂРѕРІР°С‚СЊ:
                # "last_restaurant": restaurant,
            },
        )

        if created:
            logger.info(
                "Category webhook id=%s: СЃРѕР·РґР°РЅ Р°РіСЂРµРіР°С‚ GuestCategory "
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

            # Р•СЃР»Рё РІ РјРѕРґРµР»Рё GuestCategory РµСЃС‚СЊ РїРѕР»Рµ last_restaurant (FK) вЂ” РѕР±РЅРѕРІР»СЏР№С‚Рµ РµРіРѕ С‚РѕР¶Рµ:
            if hasattr(gc, "last_restaurant_id"):
                gc.last_restaurant = restaurant

            update_fields = ["assign_count", "last_assigned_at"]
            if hasattr(gc, "last_restaurant_id"):
                update_fields.append("last_restaurant")

            gc.save(update_fields=update_fields)

            logger.info(
                "Category webhook id=%s: РѕР±РЅРѕРІР»С‘РЅ Р°РіСЂРµРіР°С‚ GuestCategory "
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
#                  РџР РРњР•РќР•РќРР• РџРћРЎР•Р©Р•РќРРЇ  Р“РћРЎРўР®
# =====================================================================
def update_visit_history_from_event(event: dict) -> tuple[bool, str]:
    """
    РћР±РЅРѕРІР»СЏРµС‚ РёСЃС‚РѕСЂРёСЋ РїРѕСЃРµС‰РµРЅРёР№ РіРѕСЃС‚СЏ (VisitHistory) РёР· РІРµР±С…СѓРєР° notificationType=1.

    РЎРѕРїРѕСЃС‚Р°РІР»РµРЅРёРµ:
      - РіРѕСЃС‚СЊ: РїРѕ phone (Рё РїСЂРё РЅРµРѕР±С…РѕРґРёРјРѕСЃС‚Рё РїРѕ customerId -> Guest.iiko_id)
      - СЂРµСЃС‚РѕСЂР°РЅ: РїРѕ terminalGroupId (РёР»Рё organizationId) -> Restaurant.iiko_id

    Р’РѕР·РІСЂР°С‰Р°РµС‚: (success, reason)
       - success=True - РІРёР·РёС‚ РѕР±РЅРѕРІР»РµРЅ
       - success=False - РЅРµ РѕР±РЅРѕРІР»РµРЅ, reason СЃРѕРґРµСЂР¶РёС‚ РїСЂРёС‡РёРЅСѓ
    """

    # РџСЂРѕРІРµСЂРєР° РЅР° СѓРІРµРґРѕРјР»РµРЅРёРµ РѕС‚ СЃРѕС‚СЂСѓРґРЅРёРєР°
    if _is_staff_notification(event):
        customer_id = event.get('customerId')
        reason = (f"РЈРІРµРґРѕРјР»РµРЅРёРµ РѕС‚ СЃРѕС‚СЂСѓРґРЅРёРєР° РїСЂРѕРїСѓС‰РµРЅРѕ: customerId={customer_id} "
                  f"(РѕС‚СЃСѓС‚СЃС‚РІСѓРµС‚ phone, РјРЅРѕРіРѕРІРµСЂРѕСЏС‚РЅРѕ СЌС‚Рѕ СЃРѕС‚СЂСѓРґРЅРёРє)")
        logger.info(reason)
        return True, reason

    phone = event.get("phone")
    customer_id = event.get("customerId")
    terminal_group_id = event.get("terminalGroupId")  # РїСЂРµРґРїРѕР»Р°РіР°РµРј, С‡С‚Рѕ СЌС‚Рѕ iiko_id СЂРµСЃС‚РѕСЂР°РЅР°
    organization_id = event.get("organizationId")     # Р·Р°РїР°СЃРЅРѕР№ РІР°СЂРёР°РЅС‚

    # 1. РС‰РµРј РіРѕСЃС‚СЏ
    guest = None

    if phone:
        # СЃРЅР°С‡Р°Р»Р° РїСЂРѕР±СѓРµРј РЅР°Р№С‚Рё РІ СЃРІРѕРµР№ Р‘Р”
        guest = Guest.objects.filter(phone=phone).first()

        # РµСЃР»Рё РЅРµ РЅР°С€Р»Рё вЂ” РїС‹С‚Р°РµРјСЃСЏ РїРѕРґС‚СЏРЅСѓС‚СЊ/СЃРѕР·РґР°С‚СЊ РіРѕСЃС‚СЏ РёР· iikocard
        if not guest:
            guest = get_or_create_guest_from_iiko(phone)

    if not guest and customer_id:
        guest = Guest.objects.filter(iiko_id=customer_id).first()

    if not guest:
        reason = f"Р“РѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅ РґР»СЏ notificationType=1: phone={phone}, customerId={customer_id}"
        logger.warning(reason)
        return False, reason

    # 2. РС‰РµРј СЂРµСЃС‚РѕСЂР°РЅ РїРѕ iiko_id
    restaurant_iiko_id = terminal_group_id or organization_id
    if not restaurant_iiko_id:
        reason = (f"РќРµ СѓРєР°Р·Р°РЅ РёРґРµРЅС‚РёС„РёРєР°С‚РѕСЂ СЂРµСЃС‚РѕСЂР°РЅР° (terminalGroupId/organizationId) "
                  f"РґР»СЏ РіРѕСЃС‚СЏ id={guest.id}, phone={guest.phone}")
        logger.warning(reason)
        return False, reason

    try:
        restaurant = Restaurant.objects.get(iiko_id=restaurant_iiko_id)
    except Restaurant.DoesNotExist:
        reason = (f"Р РµСЃС‚РѕСЂР°РЅ СЃ iiko_id={restaurant_iiko_id} РЅРµ РЅР°Р№РґРµРЅ РІ Р‘Р” "
                  f"РґР»СЏ РіРѕСЃС‚СЏ id={guest.id}, phone={guest.phone}")
        logger.warning(reason)
        return False, reason

    # 3. РџР°СЂСЃРёРј РґР°С‚Сѓ РІРёР·РёС‚Р°
    changed_on = event.get("changedOn")
    if changed_on:
        try:
            visit_dt = datetime.fromisoformat(changed_on)
        except ValueError:
            logger.warning(
                "РќРµ СѓРґР°Р»РѕСЃСЊ СЂР°СЃРїР°СЂСЃРёС‚СЊ changedOn=%s, РёСЃРїРѕР»СЊР·СѓРµРј С‚РµРєСѓС‰РµРµ РІСЂРµРјСЏ",
                changed_on,
            )
            visit_dt = timezone.now()
    else:
        visit_dt = timezone.now()

    # РґРµР»Р°РµРј РґР°С‚Сѓ aware, РµСЃР»Рё РѕРЅР° naive
    if timezone.is_naive(visit_dt):
        visit_dt = timezone.make_aware(visit_dt, timezone.get_current_timezone())

    # 4. РћР±РЅРѕРІР»СЏРµРј/СЃРѕР·РґР°С‘Рј VisitHistory
    vh, created = VisitHistory.objects.get_or_create(
        guest=guest,
        restaurant=restaurant,
        defaults={
            "visit_date": visit_dt,
            "visit_count": 1,  # РїРµСЂРІС‹Р№ РІРёР·РёС‚
        },
    )

    if not created:
        old_dt = vh.visit_date

        # СѓРІРµР»РёС‡РёРІР°РµРј СЃС‡С‘С‚С‡РёРє РїРѕСЃРµС‰РµРЅРёР№ РІСЃРµРіРґР° РїСЂРё РЅРѕРІРѕРј СЃРѕР±С‹С‚РёРё
        vh.visit_count = (vh.visit_count or 0) + 1

        # РґР°С‚Р° РїРѕСЃР»РµРґРЅРµРіРѕ РІРёР·РёС‚Р° вЂ” С‚РѕР»СЊРєРѕ РµСЃР»Рё РїСЂРёС€С‘Р» Р±РѕР»РµРµ РїРѕР·РґРЅРёР№ РІРёР·РёС‚
        if visit_dt > old_dt:
            vh.visit_date = visit_dt
            vh.save(update_fields=["visit_date", "visit_count"])
            logger.info(
                "РћР±РЅРѕРІР»РµРЅР° РґР°С‚Р° РїРѕСЃР»РµРґРЅРµРіРѕ РІРёР·РёС‚Р° РіРѕСЃС‚СЏ id=%s РІ СЂРµСЃС‚РѕСЂР°РЅ %s: %s в†’ %s "
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
                "РџРѕР»СѓС‡РµРЅ РІРёР·РёС‚ %s (РЅРµ РЅРѕРІРµРµ С‚РµРєСѓС‰РµРіРѕ %s), СѓРІРµР»РёС‡РµРЅ visit_count=%s. "
                "guest_id=%s, restaurant=%s",
                visit_dt,
                old_dt,
                vh.visit_count,
                guest.id,
                restaurant.name,
            )
    else:
        logger.info(
            "РЎРѕР·РґР°РЅР° Р·Р°РїРёСЃСЊ РІРёР·РёС‚Р°: guest_id=%s, restaurant=%s, visit_date=%s, visit_count=1",
            guest.id,
            restaurant.name,
            visit_dt,
        )

    return True, ""


def enqueue_balance_notification_from_webhook(
    webhook: dict,
    *,
    is_enabled: bool = True,
    priority: str = DispatchTask.Priority.HIGH,
    primary_only: bool = True,
) -> int:
    """
    РЇРІРЅС‹Р№ Р±РёР·РЅРµСЃ-РІС‹Р·РѕРІ РїРѕСЃС‚Р°РЅРѕРІРєРё СѓРІРµРґРѕРјР»РµРЅРёСЏ Рѕ Р±Р°Р»Р°РЅСЃРµ РІ universal queue.

    Р’Р°Р¶РЅРѕ:
    1. РџР°СЂР°РјРµС‚СЂ `is_enabled=False` РѕС‚РєР»СЋС‡Р°РµС‚ С‚РѕР»СЊРєРѕ РѕС‚РїСЂР°РІРєСѓ СѓРІРµРґРѕРјР»РµРЅРёСЏ;
       РѕСЃС‚Р°Р»СЊРЅР°СЏ Р±РёР·РЅРµСЃ-РѕР±СЂР°Р±РѕС‚РєР° webhook РїСЂРѕРґРѕР»Р¶Р°РµС‚ СЂР°Р±РѕС‚Р°С‚СЊ.
    2. РџР°СЂР°РјРµС‚СЂС‹ РјР°СЂС€СЂСѓС‚РёР·Р°С†РёРё Р·Р°РґР°СЋС‚СЃСЏ СЏРІРЅРѕ РёР· РєРѕРґР° (Р±РµР· env-РјР°РіРёРё):
       priority=high, primary_only=True.
    """
    from guests.services.balance_notifications import (
        enqueue_balance_notification_from_webhook as _enqueue_balance_notification,
    )

    return _enqueue_balance_notification(
        webhook=webhook,
        is_enabled=is_enabled,
        priority=priority,
        primary_only=primary_only,
    )


def _is_live_olap_bridge_enabled_for_notification(notification_type: int | None) -> bool:
    """
    РџСЂРѕРІРµСЂСЏРµС‚, РІРєР»СЋС‡С‘РЅ Р»Рё live-РјРѕСЃС‚ webhook -> OlapCheckSyncJournal РґР»СЏ РґР°РЅРЅРѕРіРѕ С‚РёРїР° СѓРІРµРґРѕРјР»РµРЅРёСЏ.

    Р›РѕРіРёРєР°:
    1. РѕР±С‰РёР№ С„Р»Р°Рі `OLAP_BRIDGE_ENABLE_LIVE_WEBHOOK_ENQUEUE` РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РІРєР»СЋС‡С‘РЅ;
    2. `notification_type` РґРѕР»Р¶РµРЅ РІС…РѕРґРёС‚СЊ РІ СЂР°Р·СЂРµС€С‘РЅРЅС‹Р№ СЃРїРёСЃРѕРє
       `OLAP_BRIDGE_ALLOWED_NOTIFICATION_TYPES`.
    """
    if not bool(getattr(settings, "OLAP_BRIDGE_ENABLE_LIVE_WEBHOOK_ENQUEUE", False)):
        return False

    allowed_types = getattr(settings, "OLAP_BRIDGE_ALLOWED_NOTIFICATION_TYPES", {1}) or {1}
    if not isinstance(allowed_types, (set, list, tuple)):
        return False

    try:
        normalized_types = {int(item) for item in allowed_types}
    except (TypeError, ValueError):
        normalized_types = {1}

    return notification_type in normalized_types


def handle_api_webhook(
    webhook: dict,
    *,
    send_balance_notification: bool = True,
) -> tuple[bool, str]:
    """
    Р¦РµРЅС‚СЂР°Р»СЊРЅС‹Р№ РѕР±СЂР°Р±РѕС‚С‡РёРє webhook РёР· SAGUR API.

    РџСЂР°РІРёР»Р° РјР°СЂС€СЂСѓС‚РёР·Р°С†РёРё:
    1. Р•СЃР»Рё `category_id_ext` СЃРѕРІРїР°РґР°РµС‚ СЃ РєР°С‚РµРіРѕСЂРёРµР№ В«Р‘Р°Р»Р°РЅСЃВ»,
       СЃС‚Р°РІРёРј high-priority Р·Р°РґР°С‡Сѓ СѓРІРµРґРѕРјР»РµРЅРёСЏ РІ universal queue.
    2. `notificationType=1` -> РѕР±РЅРѕРІР»СЏРµРј РёСЃС‚РѕСЂРёСЋ РїРѕСЃРµС‰РµРЅРёР№ (VisitHistory).
    3. `notificationType=5` -> РЅР°Р·РЅР°С‡Р°РµРј РєР°С‚РµРіРѕСЂРёСЋ РіРѕСЃС‚СЋ.
    4. РћСЃС‚Р°Р»СЊРЅС‹Рµ С‚РёРїС‹ РїРѕРєР° РЅРµ РѕР±СЂР°Р±Р°С‚С‹РІР°РµРј.

    Р’РѕР·РІСЂР°С‰Р°РµС‚:
        True  - webhook РѕР±СЂР°Р±РѕС‚Р°РЅ СѓСЃРїРµС€РЅРѕ, РјРѕР¶РЅРѕ СЃС‚Р°РІРёС‚СЊ `business_status=complete`.
        False - webhook РЅРµ РѕР±СЂР°Р±РѕС‚Р°РЅ (РѕС€РёР±РєР°/РЅРµРґРѕСЃС‚Р°С‚РѕРє РґР°РЅРЅС‹С…), СЃС‚Р°РІРёРј `business_status=failed`.

    РџР°СЂР°РјРµС‚СЂС‹:
        send_balance_notification: РІРєР»СЋС‡Р°С‚СЊ Р»Рё РїРѕСЃС‚Р°РЅРѕРІРєСѓ balance-СѓРІРµРґРѕРјР»РµРЅРёСЏ
        РІ universal queue РґР»СЏ РґР°РЅРЅРѕРіРѕ РІС‹Р·РѕРІР°.
    """
    event = webhook.get("parsed_body") or {}
    notif_type = event.get("notificationType")
    webhook_id = webhook.get("id")
    is_balance_event = is_balance_webhook(webhook, event if isinstance(event, dict) else {})

    # --- РЇРІРЅС‹Р№ balance-СЃС†РµРЅР°СЂРёР№ РїРѕ С„РёРєСЃРёСЂРѕРІР°РЅРЅРѕРјСѓ category external id ---
    if is_balance_event:
        try:
            enqueued_tasks = run_webhook_scenario_by_code(
                scenario_code=SCENARIO_CODE_BALANCE_CHANGED,
                webhook=webhook,
                is_enabled=send_balance_notification,
                priority=DispatchTask.Priority.HIGH,
                primary_only=True,
            )
            logger.info(
                "Webhook id=%s: balance-СЃРѕР±С‹С‚РёРµ РѕР±СЂР°Р±РѕС‚Р°РЅРѕ (category_ext_id=%s), "
                "РїРѕСЃС‚Р°РІР»РµРЅРѕ Р·Р°РґР°С‡: %s",
                webhook_id,
                BALANCE_NOTIFICATION_CATEGORY_EXTERNAL_ID,
                enqueued_tasks,
            )
            return True, f"balance webhook processed, enqueued={enqueued_tasks}"
        except Exception:
            logger.exception(
                "Webhook id=%s: РѕС€РёР±РєР° РїРѕСЃС‚Р°РЅРѕРІРєРё balance-Р·Р°РґР°С‡ РІ universal queue.",
                webhook_id,
            )
            return False, "balance enqueue error"

    # --- notificationType = 1: РѕР±РЅРѕРІР»СЏРµРј РёСЃС‚РѕСЂРёСЋ РїРѕСЃРµС‰РµРЅРёР№ (+ live-РјРѕСЃС‚ РІ OLAP РїСЂРё РІРєР»СЋС‡С‘РЅРЅРѕРј С„Р»Р°РіРµ) ---
    if notif_type == 1:
        logger.info(
            "Webhook id=%s: notificationType=1, РѕР±РЅРѕРІР»СЏРµРј РёСЃС‚РѕСЂРёСЋ РїРѕСЃРµС‰РµРЅРёР№",
            webhook_id,
        )
        success, reason = update_visit_history_from_event(event)
        if not success:
            return success, reason
        if reason:
            logger.info(
                "Webhook id=%s: VisitHistory РѕР±СЂР°Р±РѕС‚Р°РЅ Р±РµР· РїРѕСЃС‚Р°РЅРѕРІРєРё OLAP-Р·Р°РґР°С‡Рё (%s)",
                webhook_id,
                reason,
            )
            return success, reason

        if _is_live_olap_bridge_enabled_for_notification(notif_type):
            bridge_result = enqueue_olap_sync_from_webhook(
                webhook=webhook,
                guest=find_guest(event),
            )
            logger.info(
                "Webhook id=%s: live-РјРѕСЃС‚ OLAP РІС‹РїРѕР»РЅРµРЅ (created=%s, row_id=%s, reason=%s)",
                webhook_id,
                bridge_result.created,
                bridge_result.row_id,
                bridge_result.reason,
            )

        return success, reason

    # --- notificationType = 5: РЅР°Р·РЅР°С‡Р°РµРј РєР°С‚РµРіРѕСЂРёСЋ ---
    if notif_type == 5:
        logger.info(
            "Webhook id=%s: notificationType=5, РЅР°Р·РЅР°С‡Р°РµРј РєР°С‚РµРіРѕСЂРёСЋ РіРѕСЃС‚СЋ",
            webhook_id,
        )
        return apply_category_from_api_webhook(webhook)

    # --- РѕСЃС‚Р°Р»СЊРЅС‹Рµ С‚РёРїС‹ РїРѕРєР° РЅРµ РѕР±СЂР°Р±Р°С‚С‹РІР°РµРј СЃРїРµС†РёР°Р»СЊРЅРѕ ---
    logger.info(
        "Webhook id=%s: РЅРµРёР·РІРµСЃС‚РЅС‹Р№ РёР»Рё РЅРµРёСЃРїРѕР»СЊР·СѓРµРјС‹Р№ notificationType=%s, "
        "РЅРёС‡РµРіРѕ РЅРµ РґРµР»Р°РµРј, РѕСЃС‚Р°РІР»СЏРµРј pending",
        webhook_id,
        notif_type,
    )
    return False, f"РќРµРёР·РІРµСЃС‚РЅС‹Р№ notificationType={notif_type}"

# =====================================================================
#        РћР‘Р РђР‘РћРўРљРђ Р’Р•Р‘-РҐРЈРљРћР’ Р§Р•Р Р•Р— SAGUR API (business_status=pending)
# =====================================================================

def process_recent_webhooks(period_minutes=10, using="webhooks", max_retries=3, retry_delay=5):
    """
    РћР±СЂР°Р±РѕС‚РєР° РІРµР±С…СѓРєРѕРІ С‡РµСЂРµР· SAGUR API.

    РЎРµР№С‡Р°СЃ:
      - РїРѕР»СѓС‡Р°РµРј СЃРїРёСЃРѕРє РІРµР±С…СѓРєРѕРІ РёР· SAGUR API /api/internal/webhooks
        СЃ business_status=pending
      - РѕР±СЂР°Р±Р°С‚С‹РІР°РµРј РєР°Р¶РґС‹Р№:
          * balance-СЃРѕР±С‹С‚РёРµ РїРѕ category_id_ext -> enqueue СѓРІРµРґРѕРјР»РµРЅРёСЏ РІ universal queue
          * notificationType=1 -> РѕР±РЅРѕРІР»РµРЅРёРµ VisitHistory
          * notificationType=5 -> РЅР°Р·РЅР°С‡РµРЅРёРµ РєР°С‚РµРіРѕСЂРёРё РіРѕСЃС‚СЋ
      - РїРѕРјРµС‡Р°РµРј РµРіРѕ РІ SAGUR РєР°Рє business_status='complete' РїСЂРё СѓСЃРїРµС€РЅРѕР№ РѕР±СЂР°Р±РѕС‚РєРµ
        РёР»Рё 'failed' РїСЂРё РѕС€РёР±РєРµ.

    Р’РђР–РќРћ:
      - Р·Р° РѕРґРёРЅ Р·Р°РїСѓСЃРє РѕР±СЂР°Р±Р°С‚С‹РІР°РµРј РЅРµ Р±РѕР»РµРµ LIMIT РІРµР±С…СѓРєРѕРІ, С‡С‚РѕР±С‹
        РІРѕСЂРєРµСЂС‹ РјРѕРіР»Рё РґРµР»РёС‚СЊ СЂР°Р±РѕС‚Сѓ РїРѕСЂС†РёСЏРјРё.

    РџР°СЂР°РјРµС‚СЂС‹ period_minutes/using/max_retries РѕСЃС‚Р°РІР»РµРЅС‹ РґР»СЏ
    СЃРѕРІРјРµСЃС‚РёРјРѕСЃС‚Рё СЃРѕ СЃС‚Р°СЂС‹Рј РєРѕРґРѕРј, РЅРѕ СЃРµР№С‡Р°СЃ РЅРµ РёСЃРїРѕР»СЊР·СѓСЋС‚СЃСЏ.
    """

    try:
        access_token = _get_sagur_access_token_cached()
    except Exception as e:
        logger.error("SAGUR: РЅРµ СѓРґР°Р»РѕСЃСЊ РїРѕР»СѓС‡РёС‚СЊ access-С‚РѕРєРµРЅ: %s", e)
        return 0

    processed_count = 0  # СЃРєРѕР»СЊРєРѕ СЂРµР°Р»СЊРЅРѕ РЅР°Р·РЅР°С‡РµРЅРѕ РєР°С‚РµРіРѕСЂРёР№ (complete)
    seen_count = 0       # СЃРєРѕР»СЊРєРѕ РІСЃРµРіРѕ РІРµР±С…СѓРєРѕРІ РјС‹ РїРѕСЃРјРѕС‚СЂРµР»Рё РІ СЌС‚РѕРј Р·Р°РїСѓСЃРєРµ
    notify_balance = bool(getattr(settings, "BALANCE_WEBHOOK_NOTIFY_ENABLED", True))

    for webhook in _iter_pending_webhooks(access_token, page_size=PAGE_SIZE):
        if seen_count >= LIMIT:
            break

        seen_count += 1
        webhook_id = webhook.get("id")

        try:
            assigned, reason = handle_api_webhook(
                webhook,
                send_balance_notification=notify_balance,
            )

            if assigned:
                processed_count += 1
                final_status = "complete"
                log_details = reason or 'РЈСЃРїРµС…'
                reason = reason if reason else None
                logger.info(f"РЈРІРµРґРѕРјР»РµРЅРёРµ id={webhook_id} СѓСЃРїРµС€РЅРѕ РѕР±СЂР°Р±РѕС‚Р°РЅРѕ! Р”РµС‚Р°Р»Рё: {log_details}")
            else:
                final_status = "failed"
                logger.warning(f"РЈРІРµРґРѕРјР»РµРЅРёРµ id={webhook_id} РЅРµ РѕР±СЂР°Р±РѕС‚Р°РЅ: {reason}. "
                               f"РЈСЃС‚Р°РЅР°РІР»РёРІР°РµРј business_status='failed'")

            # Р•РґРёРЅС‹Р№ РІС‹Р·РѕРІ РґР»СЏ РѕР±РЅРѕРІР»РµРЅРёСЏ СЃС‚Р°С‚СѓСЃР° РІ Р‘Р”
            _update_webhook_business_status(access_token, webhook_id, final_status, reason)

        except Exception as e:
            logger.exception(
                "РћС€РёР±РєР° РѕР±СЂР°Р±РѕС‚РєРё РІРµР±С…СѓРєР° id=%s: %s. РџРѕРјРµС‡Р°РµРј РєР°Рє 'failed'",
                webhook_id,
                e,
            )
            try:
                reason = f"РћС€РёР±РєР° РѕР±СЂР°Р±РѕС‚РєРё: {str(e)[:200]}"
                _update_webhook_business_status(access_token, webhook_id, "failed", reason)
            except Exception as e2:
                logger.error(
                    "Р”РѕРї. РѕС€РёР±РєР° РїСЂРё РѕР±РЅРѕРІР»РµРЅРёРё СЃС‚Р°С‚СѓСЃР° РІРµР±С…СѓРєР° id=%s РЅР° 'failed': %s",
                    webhook_id,
                    e2,
                )

    logger.info(
        "process_recent_webhooks (С‡РµСЂРµР· SAGUR API): "
        "РїСЂРѕСЃРјРѕС‚СЂРµРЅРѕ %s РІРµР±С…СѓРєРѕРІ (pending), СѓСЃРїРµС€РЅРѕ РѕР±СЂР°Р±РѕС‚Р°РЅРѕ (complete): %s",
        seen_count,
        processed_count,
    )
    return processed_count
