"""
OneSignal notification client.

Replaces firebase_admin.messaging entirely.
Supports bilingual EN/AR notifications with retry logic.
"""

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (injected via environment — never hardcoded)
# ---------------------------------------------------------------------------
ONESIGNAL_APP_ID = os.getenv("ONESIGNAL_APP_ID")
ONESIGNAL_REST_API_KEY = os.getenv("ONESIGNAL_REST_API_KEY")

_API_URL = "https://onesignal.com/api/v1/notifications"
_DEFAULT_SEGMENT = "All"
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2  # seconds; doubles each attempt


# ---------------------------------------------------------------------------
# Core delivery function
# ---------------------------------------------------------------------------

def send_notification(
    *,
    heading_en: str,
    heading_ar: str,
    content_en: str,
    content_ar: str,
    data: dict | None = None,
) -> dict:
    """
    Send a bilingual push notification via OneSignal to all subscribers.

    OneSignal automatically delivers the correct language variant based on
    each device's locale — no client-side logic required.

    Args:
        heading_en: English notification title.
        heading_ar: Arabic notification title (Unicode, RTL-safe).
        content_en: English notification body.
        content_ar: Arabic notification body (Unicode, RTL-safe).
        data:       Optional key/value payload delivered alongside the push.

    Returns:
        The parsed JSON response from the OneSignal API.

    Raises:
        RuntimeError: If all retry attempts are exhausted.
        EnvironmentError: If required env vars are missing.
    """
    if not ONESIGNAL_APP_ID or not ONESIGNAL_REST_API_KEY:
        raise EnvironmentError(
            "ONESIGNAL_APP_ID and ONESIGNAL_REST_API_KEY must be set."
        )

    payload: dict = {
        "app_id": ONESIGNAL_APP_ID,
        "included_segments": [_DEFAULT_SEGMENT],
        # OneSignal i18n: keys are BCP-47 language codes.
        # When a device's locale is 'ar', it receives the Arabic variant.
        # All other locales fall back to 'en'.
        "headings": {"en": heading_en, "ar": heading_ar},
        "contents": {"en": content_en, "ar": content_ar},
        "ttl": 2419200,

    }

    if data:
        # Ensure all values are strings — OneSignal data payloads are
        # string-only; non-string values will be silently dropped on some
        # SDK versions.
        payload["data"] = {k: str(v) for k, v in data.items()}

    headers = {
        "Authorization": f"Basic {ONESIGNAL_REST_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    last_exc: Exception | None = None

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = requests.post(
                _API_URL, json=payload, headers=headers, timeout=10
            )
            response.raise_for_status()

            result: dict = response.json()

            # OneSignal returns HTTP 200 even for logical errors — check explicitly.
            if errors := result.get("errors"):
                raise ValueError(f"OneSignal API error: {errors}")

            recipients = result.get("recipients", 0)
            logger.info(
                "✅ Notification delivered — recipients: %d | id: %s",
                recipients,
                result.get("id"),
            )
            return result

        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                wait = _RETRY_BACKOFF_BASE**attempt
                logger.warning(
                    "⚠️  Notification attempt %d/%d failed (%s). "
                    "Retrying in %ds...",
                    attempt,
                    _MAX_RETRIES,
                    exc,
                    wait,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "❌ All %d notification attempts exhausted: %s",
                    _MAX_RETRIES,
                    exc,
                )

    raise RuntimeError(
        f"Notification failed after {_MAX_RETRIES} attempts."
    ) from last_exc


# ---------------------------------------------------------------------------
# Domain-specific helpers (called from update_freebies.py)
# ---------------------------------------------------------------------------

def notify_new_game(game: dict) -> None:
    """
    Send a 'new free game' push notification in English and Arabic.

    Arabic copy uses natural, idiomatic phrasing and is encoded as
    standard Unicode — no special handling needed beyond ensure_ascii=False
    in any downstream JSON serialisation.
    """
    name = game.get("name") or game.get("title", "Unknown")
    store = game.get("store", "the store")
    worth = game.get("worth", "0.00")

    try:
        send_notification(
            heading_en=f"FREE {name}",
            heading_ar=f"{name} مجانًا",
            content_en=f"Save ${worth}. Thousands are claiming it on {store} — get it now!",
            content_ar=f"وفر ${worth}. آلاف الأشخاص يحصلون عليه على {store} — احصل عليه الآن!",
            data={
                "game_name": name,
                "worth": worth,
                "store": store,
                "expiry_date": game.get("expiry_date", ""),
                "click_action": "OPEN_GAME_PAGE",
            },
        )
        logger.info("🎮 New-game notification sent for: %s", name)
    except (RuntimeError, EnvironmentError) as exc:
        logger.error("❌ New-game notification failed for %s: %s", name, exc)


def notify_expiry_reminder(game: dict) -> None:
    """
    Send a 'last chance' expiry reminder in English and Arabic.

    Only called when reminder_sent is False for this game; the flag is set
    by the caller (update_freebies.py) after this function returns.
    """
    name = game.get("name") or game.get("title", "Unknown")
    store = game.get("store", "the store")
    worth = game.get("worth", "0.00")

    try:
        send_notification(
            heading_en=f"Don't Miss {name}",
            heading_ar=f"لا تفوّت {name}",
            content_en=(
              f"Today is your LAST chance to claim this ${worth} game for FREE on {store}!"
            ),
            content_ar=(
             f"اليوم آخر فرصة للحصول على اللعبة مجانًا من {store}!"
            ),
            data={
                "game_name": name,
                "worth": worth,
                "store": store,
                "expiry_date": game.get("expiry_date", ""),
                "click_action": "OPEN_GAME_PAGE",
            },
        )
        logger.info("⏰ Expiry-reminder notification sent for: %s", name)
    except (RuntimeError, EnvironmentError) as exc:
        logger.error("❌ Expiry-reminder failed for %s: %s", name, exc)