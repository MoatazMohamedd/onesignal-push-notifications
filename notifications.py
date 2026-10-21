"""
Firebase Cloud Messaging (FCM) topic notifications.

Phones subscribe themselves to a topic, so this module never needs a list of
devices: it sends one message to `free_games_en` and one to `free_games_ar`
and Firebase fans them out to every subscriber (Android directly, iOS via APNs).

Authentication reuses the service-account key you already have
(FIREBASE_CREDENTIALS_JSON) through the FCM HTTP v1 API. No firebase_admin.

Optional environment variable:
  PUSH_TOPIC_PREFIX  e.g. "dev_" to message development builds only
                     (debug builds of the app subscribe to dev_ topics).
  FCM_PROJECT_ID     overrides the project_id found in the service-account key.
"""

import logging
import os
import time

import requests
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2 import service_account

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/firebase.messaging"]
_TOPIC_EN = "free_games_en"
_TOPIC_AR = "free_games_ar"
_MAX_RETRIES = 3
_BACKOFF_BASE = 2  # seconds; doubles each attempt
_MAX_TTL_SECONDS = 2_419_200  # FCM maximum: four weeks

_credentials: service_account.Credentials | None = None
_project_id: str | None = None


def configure(service_account_info: dict) -> None:
    """Load the service-account key (the parsed FIREBASE_CREDENTIALS_JSON)."""
    global _credentials, _project_id
    _credentials = service_account.Credentials.from_service_account_info(
        service_account_info, scopes=_SCOPES
    )
    _project_id = os.getenv("FCM_PROJECT_ID") or service_account_info.get("project_id")
    if not _project_id:
        raise ValueError(
            "FCM project id is missing. Set FCM_PROJECT_ID or include project_id "
            "in FIREBASE_CREDENTIALS_JSON."
        )


def _access_token(force: bool = False) -> str:
    if _credentials is None or not _project_id:
        raise RuntimeError("notifications.configure(service_account_info) was not called.")
    if force or not _credentials.valid:
        _credentials.refresh(Request())
    return _credentials.token  # type: ignore[return-value]


def _explain(resp: requests.Response) -> str:
    try:
        message = resp.json().get("error", {}).get("message", resp.text[:300])
    except ValueError:
        message = resp.text[:300]
    text = f"FCM rejected the message: HTTP {resp.status_code} {message}"
    if resp.status_code == 403:
        text += (
            " — enable 'Firebase Cloud Messaging API (V1)' for the project and give the "
            "service account the role 'Firebase Cloud Messaging API Admin'."
        )
    elif resp.status_code == 404:
        text += " — check the project id (FCM_PROJECT_ID / service-account project_id)."
    return text


def _send_message(message: dict) -> str:
    """POST one message to FCM v1. Returns the message name. Retries transient errors."""
    url = f"https://fcm.googleapis.com/v1/projects/{_project_id}/messages:send"
    last_exc: Exception | None = None
    force_refresh = False

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            token = _access_token(force=force_refresh)
            force_refresh = False
            resp = requests.post(
                url,
                json={"message": message},
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            )
        except (requests.RequestException, GoogleAuthError) as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                wait = _BACKOFF_BASE**attempt
                logger.warning(
                    "⚠️  FCM attempt %d/%d failed (%s). Retrying in %ds...",
                    attempt, _MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)
            continue

        if resp.status_code == 200:
            return resp.json().get("name", "")

        if resp.status_code == 401:  # expired token: refresh once and retry
            force_refresh = True
            last_exc = RuntimeError("HTTP 401")
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            last_exc = RuntimeError(f"HTTP {resp.status_code}")
            if attempt < _MAX_RETRIES:
                wait = _BACKOFF_BASE**attempt
                logger.warning(
                    "⚠️  FCM attempt %d/%d got HTTP %d. Retrying in %ds...",
                    attempt, _MAX_RETRIES, resp.status_code, wait,
                )
                time.sleep(wait)
            continue

        raise RuntimeError(_explain(resp))  # our fault: retrying will not help

    raise RuntimeError(
        f"FCM request failed after {_MAX_RETRIES} attempts: {last_exc}"
    ) from last_exc


def send_to_topic(
    topic: str,
    title: str,
    body: str,
    data: dict | None = None,
    ttl: int = 86400,
) -> str:
    """Send one notification to everyone subscribed to `topic`."""
    if not topic or any(char in topic for char in "/ "):
        raise ValueError(f"Invalid FCM topic: {topic!r}")
    if not 0 <= ttl <= _MAX_TTL_SECONDS:
        raise ValueError(
            f"ttl must be between 0 and {_MAX_TTL_SECONDS} seconds, got {ttl}."
        )

    prefix = os.getenv("PUSH_TOPIC_PREFIX", "")
    message: dict = {
        "topic": f"{prefix}{topic}",
        "notification": {"title": title, "body": body},
        "android": {"priority": "HIGH", "ttl": f"{ttl}s"},
        "apns": {
            "headers": {
                "apns-priority": "10",
                "apns-expiration": str(int(time.time()) + ttl),
            },
            "payload": {"aps": {"sound": "default"}},
        },
    }
    if data:
        # FCM data payloads must be string -> string.
        message["data"] = {k: str(v) for k, v in data.items()}
    return _send_message(message)


def send_notification(
    *,
    heading_en: str,
    heading_ar: str,
    content_en: str,
    content_ar: str,
    data: dict | None = None,
    ttl: int = 86400,
) -> dict:
    """
    Send the English text to the English topic and the Arabic text to the Arabic
    topic. Raises RuntimeError if either send failed (the other is still attempted).

    Note: FCM topic sends do not report how many devices received the message.
    A successful result means Firebase accepted it for delivery.
    """
    results: dict = {}
    errors: list[str] = []

    for topic, title, body in (
        (_TOPIC_EN, heading_en, content_en),
        (_TOPIC_AR, heading_ar, content_ar),
    ):
        try:
            results[topic] = send_to_topic(topic, title, body, data, ttl)
            logger.info("📨 Push accepted for topic %s: %s", topic, results[topic])
        except RuntimeError as exc:
            errors.append(f"{topic}: {exc}")
            logger.error("❌ Push to topic %s failed: %s", topic, exc)

    if errors:
        raise RuntimeError("; ".join(errors))
    return results


# ---------------------------------------------------------------------------
# Domain-specific helpers (called from update_freebies.py)
# ---------------------------------------------------------------------------

def notify_new_game(game: dict) -> None:
    """Send a 'new free game' push in English and Arabic."""
    name = game.get("name") or game.get("title", "Unknown")
    store = game.get("store", "the store")
    worth = game.get("worth", "0.00")

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
        ttl=86400,  # 24h
    )
    logger.info("🎮 New-game notification accepted by FCM for: %s", name)


def notify_expiry_reminder(game: dict) -> None:
    """
    Send a 'last chance' expiry reminder in English and Arabic.

    Only called when reminder_sent is False for this game; the flag is set
    by the caller (update_freebies.py) after this function returns.
    """
    name = game.get("name") or game.get("title", "Unknown")
    store = game.get("store", "the store")
    worth = game.get("worth", "0.00")

    send_notification(
        heading_en=f"Don't Miss {name}",
        heading_ar=f"لا تفوّت {name}",
        content_en=f"Today is your LAST chance to claim this ${worth} game for FREE on {store}!",
        content_ar=f"اليوم آخر فرصة للحصول على اللعبة مجانًا من {store}!",
        data={
            "game_name": name,
            "worth": worth,
            "store": store,
            "expiry_date": game.get("expiry_date", ""),
            "click_action": "OPEN_GAME_PAGE",
        },
        ttl=43200,  # 12h: a stale "last chance" is worse than none
    )
    logger.info("⏰ Expiry reminder accepted by FCM for: %s", name)
