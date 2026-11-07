"""Sync current Steam free-weekend offers from FreeToKeep into Firestore."""

import json
import logging
import os
from datetime import datetime, timezone

import requests
from google.cloud import firestore
from google.oauth2 import service_account


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

API_URL = "https://freetokeep.gg/api/v1/free-weekends"
FIREBASE_CREDENTIALS_JSON = os.getenv("FIREBASE_CREDENTIALS_JSON")
FIRESTORE_PROJECT_ID = os.getenv("FIRESTORE_PROJECT_ID")


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch_current_weekends(now: datetime | None = None) -> tuple[str, list[dict]]:
    response = requests.get(
        API_URL,
        params={"status": "current", "limit": 100},
        timeout=30,
        headers={"User-Agent": "GamesOrb/1.0 (Steam free-weekend feed sync)"},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("FreeToKeep returned an invalid free-weekends response")

    generated_at = payload.get("generatedAt")
    if not isinstance(generated_at, str) or not generated_at.strip():
        raise ValueError("FreeToKeep response is missing generatedAt")

    current_time = now or datetime.now(timezone.utc)
    games: list[dict] = []
    seen_ids: set[str] = set()

    for item in payload["items"]:
        if not isinstance(item, dict):
            logger.warning("Skipping malformed free-weekend item: expected an object")
            continue

        offer_id = item.get("id")
        app_id = item.get("appId")
        title = item.get("title")
        starts_at = item.get("startsAt")
        ends_at = item.get("endsAt")
        end_time = _parse_datetime(ends_at)

        if offer_id is None or not isinstance(app_id, int) or not isinstance(title, str):
            logger.warning("Skipping free-weekend item with missing id, appId, or title")
            continue
        if not end_time:
            logger.warning("Skipping %s: missing or invalid endsAt", title)
            continue
        if end_time <= current_time:
            logger.info("Skipping expired free weekend: %s", title)
            continue

        stable_id = str(offer_id)
        if stable_id in seen_ids:
            logger.warning("Skipping duplicate free-weekend id: %s", stable_id)
            continue
        seen_ids.add(stable_id)

        games.append(
            {
                "id": stable_id,
                "appId": app_id,
                "title": title.strip(),
                "url": item.get("url") if isinstance(item.get("url"), str) else "",
                "imageUrl": item.get("imageUrl")
                if isinstance(item.get("imageUrl"), str)
                else "",
                "startsAt": starts_at if isinstance(starts_at, str) else "",
                "endsAt": ends_at,
            }
        )

    if payload["items"] and not games:
        raise ValueError(
            "FreeToKeep returned offers, but none had valid fields; preserving the existing Firestore feed"
        )

    games.sort(key=lambda game: game["endsAt"])
    return generated_at, games


def main() -> None:
    if not FIREBASE_CREDENTIALS_JSON:
        raise RuntimeError("FIREBASE_CREDENTIALS_JSON is not configured")

    service_account_info = json.loads(FIREBASE_CREDENTIALS_JSON)
    credentials = service_account.Credentials.from_service_account_info(
        service_account_info
    )
    project_id = FIRESTORE_PROJECT_ID or service_account_info.get("project_id")
    if not project_id:
        raise RuntimeError(
            "FIRESTORE_PROJECT_ID is not configured and the service account has no project_id"
        )

    generated_at, games = fetch_current_weekends()
    document = firestore.Client(project=project_id, credentials=credentials)
    document.collection("steam_free_weekends").document("current").set(
        {
            "games": games,
            "count": len(games),
            "generatedAt": generated_at,
            "source": "FreeToKeep.gg",
        }
    )
    logger.info("Saved %d current Steam free weekends to Firestore", len(games))


if __name__ == "__main__":
    main()
