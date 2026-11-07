"""Curate FreeToGame's current Windows free-to-play catalog into Firestore."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from google.cloud import firestore
from google.oauth2 import service_account


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

API_URL = "https://www.freetogame.com/api/games"
SOURCE_URL = "https://www.freetogame.com/"
COLLECTION = "free_to_play_games"
DOCUMENT = "current"
ALGORITHM_VERSION = 1

POPULAR_LIMIT = 12
POPULAR_GENRE_LIMIT = 3
NEW_RELEASE_LIMIT = 8
NEW_RELEASE_GENRE_LIMIT = 2
NEW_RELEASE_WINDOW_DAYS = 730
NEW_RELEASE_POPULARITY_CEILING = 180
MIN_POPULAR_PICKS = 6

FIREBASE_CREDENTIALS_JSON = os.getenv("FIREBASE_CREDENTIALS_JSON")
FIRESTORE_PROJECT_ID = os.getenv("FIRESTORE_PROJECT_ID")


def _valid_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in {"https", "http"} and bool(parsed.netloc)


def _release_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _fetch_games(sort_by: str) -> list[dict[str, Any]]:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.get(
                API_URL,
                params={"platform": "windows", "sort-by": sort_by},
                timeout=(10, 30),
                headers={"User-Agent": "GamesOrb/1.0 (FreeToGame catalog sync)"},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("FreeToGame response was not a JSON list")
            return payload
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < 2:
                delay = 2**attempt
                logger.warning(
                    "FreeToGame %s request failed; retrying in %ds: %s",
                    sort_by,
                    delay,
                    exc,
                )
                time.sleep(delay)
    raise RuntimeError(f"FreeToGame {sort_by} request failed: {last_error}")


def _normalize_game(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    game_id = raw.get("id")
    title = raw.get("title")
    genre = raw.get("genre")
    platform = raw.get("platform")
    release_date = _release_date(raw.get("release_date"))
    thumbnail = raw.get("thumbnail")
    game_url = raw.get("game_url")
    profile_url = raw.get("freetogame_profile_url")

    if isinstance(game_id, bool) or not isinstance(game_id, int):
        return None
    if not all(isinstance(value, str) and value.strip() for value in (title, genre, platform)):
        return None
    if "windows" not in platform.casefold():
        return None
    if release_date is None or release_date > date.today():
        return None
    if not _valid_url(thumbnail) or not _valid_url(game_url) or not _valid_url(profile_url):
        return None

    return {
        "id": game_id,
        "title": title.strip(),
        "thumbnail": thumbnail.strip(),
        "shortDescription": str(raw.get("short_description") or "").strip()[:500],
        "gameUrl": game_url.strip(),
        "genre": genre.strip(),
        "platform": platform.strip(),
        "publisher": str(raw.get("publisher") or "").strip(),
        "developer": str(raw.get("developer") or "").strip(),
        "releaseDate": release_date.isoformat(),
        "profileUrl": profile_url.strip(),
    }


def _dedupe_ranked(
    raw_games: list[dict[str, Any]], rank_field: str
) -> tuple[dict[int, dict[str, Any]], list[int]]:
    games: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    for index, raw in enumerate(raw_games, start=1):
        game = _normalize_game(raw)
        if game is None:
            continue
        game_id = game["id"]
        if game_id not in games:
            games[game_id] = game
            order.append(game_id)
        games[game_id][rank_field] = index
    return games, order


def _genre_key(game: dict[str, Any]) -> str:
    return game["genre"].strip().casefold()


def _choose_diverse(
    candidates: list[dict[str, Any]], limit: int, per_genre: int
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    genre_counts: dict[str, int] = {}
    selected_ids: set[int] = set()

    for game in candidates:
        genre = _genre_key(game)
        if genre_counts.get(genre, 0) >= per_genre:
            continue
        selected.append(game)
        selected_ids.add(game["id"])
        genre_counts[genre] = genre_counts.get(genre, 0) + 1
        if len(selected) >= limit:
            break

    # Preserve useful coverage when a narrow feed has too few distinct genres.
    if len(selected) < limit:
        for game in candidates:
            if game["id"] in selected_ids:
                continue
            selected.append(game)
            if len(selected) >= limit:
                break
    return selected


def curate_games(
    popularity_payload: list[dict[str, Any]],
    release_payload: list[dict[str, Any]],
    today: date | None = None,
) -> list[dict[str, Any]]:
    current_day = today or datetime.now(timezone.utc).date()
    popular_games, popular_order = _dedupe_ranked(
        popularity_payload, "popularityRank"
    )
    release_games, release_order = _dedupe_ranked(release_payload, "releaseRank")

    for game_id, release_game in release_games.items():
        if game_id in popular_games:
            popular_games[game_id]["releaseRank"] = release_game["releaseRank"]
        else:
            popular_games[game_id] = release_game

    if len(popular_games) < MIN_POPULAR_PICKS:
        raise ValueError(
            f"Only {len(popular_games)} valid games were returned; preserving existing Firestore data"
        )

    popularity_candidates = [popular_games[game_id] for game_id in popular_order if game_id in popular_games]
    popular_picks = _choose_diverse(
        popularity_candidates, POPULAR_LIMIT, POPULAR_GENRE_LIMIT
    )
    if len(popular_picks) < MIN_POPULAR_PICKS:
        raise ValueError("Not enough valid popular picks; preserving existing Firestore data")

    release_candidates: list[dict[str, Any]] = []
    for game_id in release_order:
        game = popular_games.get(game_id)
        if not game:
            continue
        released = _release_date(game.get("releaseDate"))
        popularity_rank = game.get("popularityRank")
        if released is None or not isinstance(popularity_rank, int):
            continue
        age_days = (current_day - released).days
        if age_days < 0 or age_days > NEW_RELEASE_WINDOW_DAYS:
            continue
        if popularity_rank > NEW_RELEASE_POPULARITY_CEILING:
            continue

        release_candidates.append(game)

    # The API provides the fresh-to-old order; only keep releases with a popularity
    # signal, then cap repeat genres so one category cannot fill the whole shelf.
    release_candidates.sort(
        key=lambda game: (
            game.get("releaseRank", 10_000),
            game.get("popularityRank", 10_000),
        )
    )
    new_release_picks = _choose_diverse(
        release_candidates, NEW_RELEASE_LIMIT, NEW_RELEASE_GENRE_LIMIT
    )

    labels: dict[int, set[str]] = {}
    for game in popular_picks:
        labels.setdefault(game["id"], set()).add("popular")
    for game in new_release_picks:
        labels.setdefault(game["id"], set()).add("new_release")

    selected_ids = [game["id"] for game in popular_picks]
    selected_ids.extend(
        game["id"] for game in new_release_picks if game["id"] not in labels or "popular" not in labels[game["id"]]
    )

    curated: list[dict[str, Any]] = []
    for game_id in selected_ids:
        game = popular_games[game_id]
        curated.append(
            {
                key: value
                for key, value in {
                    **game,
                    "editorialLabels": sorted(labels[game_id]),
                }.items()
                if value is not None
            }
        )

    return curated


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

    popularity_payload = _fetch_games("popularity")
    release_payload = _fetch_games("release-date")
    games = curate_games(popularity_payload, release_payload)
    popular_count = sum("popular" in game["editorialLabels"] for game in games)
    new_count = sum("new_release" in game["editorialLabels"] for game in games)

    document = firestore.Client(project=project_id, credentials=credentials)
    document.collection(COLLECTION).document(DOCUMENT).set(
        {
            "games": games,
            "count": len(games),
            "popularCount": popular_count,
            "newReleaseCount": new_count,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "source": "FreeToGame",
            "sourceUrl": SOURCE_URL,
            "algorithmVersion": ALGORITHM_VERSION,
        }
    )
    logger.info(
        "Saved %d curated Windows F2P games (%d popular, %d recent) to %s/%s",
        len(games),
        popular_count,
        new_count,
        COLLECTION,
        DOCUMENT,
    )


if __name__ == "__main__":
    main()
