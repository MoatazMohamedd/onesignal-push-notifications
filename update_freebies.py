"""
Free games pipeline: fetch → enrich → persist → notify.

Notification delivery is delegated entirely to notifications.py.
Firebase Admin SDK (firebase_admin) has been removed; Firestore is
accessed directly via google-cloud-firestore, which never required it.
"""

import json
import logging
import os
import re
import string
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests
from google.cloud import firestore
from google.oauth2 import service_account

from notifications import notify_expiry_reminder, notify_new_game

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------
GAMERPOWER_API = "https://www.gamerpower.com/api/filter?type=game"
SKIPPED_JSON_FILE = "skipped_games.json"

IGDB_CLIENT_ID = os.getenv("IGDB_CLIENT_ID")
IGDB_ACCESS_TOKEN = os.getenv("IGDB_ACCESS_TOKEN")

FIREBASE_CREDENTIALS_JSON = os.getenv("FIREBASE_CREDENTIALS_JSON")
FIRESTORE_PROJECT_ID = os.getenv("FIRESTORE_PROJECT_ID")

# ---------------------------------------------------------------------------
# Manual games (prepended to the Firestore array)
# ---------------------------------------------------------------------------
MANUAL_GAMES: list[dict] = []

# ---------------------------------------------------------------------------
# Firestore client (no firebase_admin required)
# ---------------------------------------------------------------------------
_firebase_cred_dict: dict = json.loads(FIREBASE_CREDENTIALS_JSON)  # type: ignore[arg-type]
_credentials = service_account.Credentials.from_service_account_info(
    _firebase_cred_dict
)
firestore_client = firestore.Client(
    project=FIRESTORE_PROJECT_ID, credentials=_credentials
)

# ---------------------------------------------------------------------------
# Title normalisation helpers
# ---------------------------------------------------------------------------
_ROMAN_MAP = {
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5",
    "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10",
    "xi": "11", "xii": "12", "xiii": "13", "xiv": "14", "xv": "15",
    "xvi": "16", "xvii": "17", "xviii": "18", "xix": "19", "xx": "20",
}

_EDITION_KEYWORDS = {
    "remastered", "definitive", "goty", "complete", "hd",
    "ultimate", "anniversary", "collection", "trilogy", "bundle",
    "director", "redux", "reloaded", "remake",
}


def normalize_title(title: str) -> str:
    """Normalize game titles for strict equality checks."""
    if not title:
        return ""
    t = unicodedata.normalize("NFKD", title)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.lower().replace("&", " and ")
    t = re.sub(r"[™®©]", "", t)
    t = re.sub(rf"[{re.escape(string.punctuation)}]", " ", t)
    tokens = [_ROMAN_MAP.get(tok, tok) for tok in t.split()]
    return re.sub(r"\s+", " ", " ".join(tokens)).strip()


def is_confusing_match(gp_title: str, igdb_name: str) -> bool:
    """Reject sequels/editions that GamerPower title didn't mention."""
    gp_norm = normalize_title(gp_title)
    igdb_norm = normalize_title(igdb_name)

    if re.findall(r"\d+", igdb_norm) and not re.findall(r"\d+", gp_norm):
        return True

    return any(kw in igdb_norm and kw not in gp_norm for kw in _EDITION_KEYWORDS)


# ---------------------------------------------------------------------------
# Store detection
# ---------------------------------------------------------------------------

def detect_store(offer: dict) -> str:
    title = offer.get("title", "").lower()
    desc = (offer.get("description", "") or "").lower()
    platforms = (offer.get("platforms", "") or "").lower()

    store_signals = [
        ("Steam",           ["steam"]),
        ("Epic Games Store",["epic"]),
        ("GoG",             ["gog"]),
        ("Origin",          ["origin"]),
        ("IndieGala",       ["indiegala"]),
        ("STOVE",           ["stove"]),
        ("Itch.io",         ["itch"]),
        ("DRM-Free",        ["drm-free"]),
    ]
    for store_name, signals in store_signals:
        if any(s in title or s in platforms or s in desc for s in signals):
            return store_name
    return "Unknown"


# ---------------------------------------------------------------------------
# Data merging / skipped-game logging
# ---------------------------------------------------------------------------

def merge_game_data(gp_game: dict, igdb_data: dict) -> dict:
    merged = {**gp_game, **igdb_data}
    merged["open_giveaway_url"] = gp_game.get("open_giveaway_url")
    return merged


def append_skipped(game: dict, reason: str) -> None:
    entry = {
        **game,
        "reason": reason,
        "skipped_at": datetime.now(timezone.utc).isoformat(),
    }
    skipped: list = []
    if os.path.exists(SKIPPED_JSON_FILE):
        with open(SKIPPED_JSON_FILE, "r", encoding="utf-8") as f:
            try:
                skipped = json.load(f)
            except json.JSONDecodeError:
                skipped = []
    skipped.append(entry)
    with open(SKIPPED_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(skipped, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# GamerPower
# ---------------------------------------------------------------------------

def fetch_gamerpower_games() -> list[dict]:
    """Fetch current free games from the GamerPower API."""
    try:
        resp = requests.get(GAMERPOWER_API, timeout=10)
        resp.raise_for_status()
        games: list[dict] = []

        for offer in resp.json():
            if "Key Giveaway" in offer.get("title", ""):
                continue

            end_date = offer.get("end_date")
            expiry_date = (
                end_date
                if end_date and end_date != "N/A"
                else (datetime.utcnow() + timedelta(days=30)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            )

            clean_title = re.sub(r"\s*\(.*?\)", "", offer["title"])
            clean_title = re.sub(r"\s*Giveaway", "", clean_title).strip()
            worth = offer.get("worth", "$0.00").replace("$", "").strip() or "0.00"

            games.append({
                "gamerpower_id": offer["id"],
                "title": clean_title,
                "worth": worth,
                "store": detect_store(offer),
                "expiry_date": expiry_date,
                "open_giveaway_url": (
                    offer.get("open_giveaway_url") or offer.get("open_giveaway")
                ),
            })
        return games

    except Exception as exc:
        logger.error("❌ Error fetching GamerPower data: %s", exc)
        return []


# ---------------------------------------------------------------------------
# IGDB
# ---------------------------------------------------------------------------

def fetch_igdb_data(
    title: str, normalized_target: str, gp_game: dict
) -> dict:
    """Fetch and transform game metadata from IGDB (4 req/s rate limit)."""
    time.sleep(0.25)

    headers = {
        "Client-ID": IGDB_CLIENT_ID,
        "Authorization": f"Bearer {IGDB_ACCESS_TOKEN}",
    }
    body = f"""
    search "{title}";
    fields id, name, cover.url, total_rating, storyline, first_release_date,
           summary, genres.name, player_perspectives.name, game_engines.name,
           game_modes.name, screenshots.url, websites.url, platforms;
    limit 25;
    """
    try:
        resp = requests.post(
            "https://api.igdb.com/v4/games",
            headers=headers,
            data=body.strip(),
            timeout=10,
        )
        resp.raise_for_status()

        for result in resp.json() or []:
            candidate = result.get("name", "") or ""
            if normalize_title(candidate) != normalized_target:
                continue
            if is_confusing_match(title, candidate):
                append_skipped(gp_game, f"Confusing match with '{candidate}'")
                continue
            platforms = [str(p) for p in result.get("platforms", [])]
            if not any(pid in platforms for pid in ("6", "14", "92")):
                append_skipped(gp_game, f"Non-PC platform match: {candidate}")
                continue
            return _transform_igdb(result)

        append_skipped(gp_game, "No strict safe IGDB match")
        return {}

    except Exception as exc:
        append_skipped(gp_game, f"IGDB fetch error: {exc}")
        return {}


def _transform_igdb(raw: dict) -> dict:
    """Transform IGDB raw response into our internal schema."""
    def _cover(url: str) -> str:
        return "https:" + url.replace("t_thumb", "t_cover_big")

    def _screenshot(url: str) -> str:
        return "https:" + url.replace("t_thumb", "t_screenshot_med")

    transformed: dict = {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "summary": raw.get("summary"),
        "storyline": raw.get("storyline"),
        "total_rating": raw.get("total_rating"),
        "first_release_date": raw.get("first_release_date"),
    }

    if cover := raw.get("cover"):
        if url := cover.get("url"):
            transformed["cover_url"] = _cover(url)

    if screenshots := raw.get("screenshots"):
        transformed["screenshots"] = [
            _screenshot(s["url"]) for s in screenshots if s.get("url")
        ]

    if websites := raw.get("websites"):
        transformed["websites"] = [w["url"] for w in websites if w.get("url")]

    for field in ("player_perspectives", "game_engines", "game_modes", "genres"):
        if items := raw.get(field):
            transformed[field] = [i["name"] for i in items if i.get("name")]

    return transformed


# ---------------------------------------------------------------------------
# Expiry check
# ---------------------------------------------------------------------------

def is_expiring_today(expiry_date: str) -> bool:
    """Return True if expiry_date falls on today's UTC date."""
    try:
        exp = datetime.fromisoformat(expiry_date.replace("Z", "+00:00"))
    except ValueError:
        return False
    return exp.date() == datetime.now(timezone.utc).date()


# ---------------------------------------------------------------------------
# Firestore persistence
# ---------------------------------------------------------------------------

def get_firestore_games() -> list[dict]:
    """Read the current game list from Firestore."""
    try:
        doc = firestore_client.collection("test").document("games").get()
        if doc.exists:
            return (doc.to_dict() or {}).get("games", [])
        return []
    except Exception as exc:
        logger.error("❌ Firestore read failed: %s", exc)
        return []


def update_firestore_games(games: list[dict]) -> None:
    """Persist the updated game list to Firestore."""
    try:
        firestore_client.collection("test").document("games").set(
            {"games": games}
        )
        logger.info("✅ Saved %d games to Firestore", len(games))
    except Exception as exc:
        logger.error("❌ Firestore write failed: %s", exc)


# ---------------------------------------------------------------------------
# Expiry reminder dispatch
# ---------------------------------------------------------------------------

def send_expiry_reminders(
    games: list[dict], firestore_games: list[dict]
) -> None:
    """
    Send expiry reminders for games that expire today and haven't been
    reminded yet. Updates reminder_sent flag in-place on the game dict.
    """
    firestore_map = {g["gamerpower_id"]: g for g in firestore_games}

    for game in games:
        if not is_expiring_today(game.get("expiry_date", "")):
            continue

        existing = firestore_map.get(game["gamerpower_id"], {})
        if existing.get("reminder_sent"):
            continue

        notify_expiry_reminder(game)
        game["reminder_sent"] = True


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("🎮 Fetching GamerPower freebies...")
    gp_games = fetch_gamerpower_games()

    if not gp_games:
        logger.warning("⚠️  No games fetched from GamerPower. Exiting.")
        return

    logger.info("📥 Found %d games from GamerPower", len(gp_games))

    logger.info("🔍 Fetching games from Firestore...")
    firestore_games = get_firestore_games()

    firestore_ids = {g["gamerpower_id"] for g in firestore_games}
    gp_ids = {g["gamerpower_id"] for g in gp_games}

    added_ids = gp_ids - firestore_ids
    removed_ids = firestore_ids - gp_ids

    if not added_ids and not removed_ids:
        logger.info("✨ No changes detected. Everything is up to date!")
        return

    logger.info(
        "📊 Changes — added: %d, removed: %d",
        len(added_ids),
        len(removed_ids),
    )

    firestore_map = {g["gamerpower_id"]: g for g in firestore_games}
    enriched_games: list[dict] = []

    test_games = ["Grand Theft Auto V","Death Stranding: Director's Cut", "Marvel's Guardians of the Galaxy","Control: Ultimate Edition"]
    for gp_game in test_games:
        gp_id = gp_game["gamerpower_id"]
        is_new = gp_id in added_ids

        # Enrich when new, or when existing entry is missing IGDB data.
        should_enrich = is_new or not (
            firestore_map.get(gp_id, {}).get("id")
            and firestore_map.get(gp_id, {}).get("name")
        )

        if True:
            logger.info("🔎 Enriching: %s", gp_game["title"])
            igdb_data = fetch_igdb_data(
                gp_game["title"], normalize_title(gp_game["title"]), gp_game
            )

            if not igdb_data:
                logger.warning("⚠️  Skipped %s (no IGDB match)", gp_game["title"])
                continue

            merged = merge_game_data(gp_game, igdb_data)

            if gp_id in firestore_map:
                existing = firestore_map[gp_id]
                final: dict = {}
                for key, value in merged.items():
                    if key in ("expiry_date", "worth", "store", "open_giveaway_url"):
                        final[key] = value
                    elif existing.get(key) not in (None, "", [], {}):
                        final[key] = existing[key]
                    else:
                        final[key] = value
                enriched_games.append(final)
            else:
                enriched_games.append(merged)

            if is_new:
                notify_new_game(merged)

        else:
            # Existing game with IGDB data — refresh mutable API fields only.
            existing = firestore_map[gp_id]
            refreshed = {
                **existing,
                **{
                    k: gp_game[k]
                    for k in ("expiry_date", "worth", "store", "open_giveaway_url")
                    if k in gp_game
                },
            }
            enriched_games.append(refreshed)

    logger.info("⏰ Checking for games expiring today...")
    send_expiry_reminders(enriched_games, firestore_games)

    final_games = MANUAL_GAMES + enriched_games
    if MANUAL_GAMES:
        logger.info("📌 Prepended %d manual games", len(MANUAL_GAMES))

    update_firestore_games(final_games)

    if removed_ids:
        logger.info("🗑️  Removed %d expired games", len(removed_ids))

    logger.info(
        "✅ Done! %d total games (%d manual + %d from API)",
        len(final_games),
        len(MANUAL_GAMES),
        len(enriched_games),
    )


if __name__ == "__main__":
    main()