"""Write the latest USD exchange-rate catalogue to Firestore."""

import json
import os
import sys
from pathlib import Path

from google.cloud import firestore
from google.oauth2 import service_account


def load_payload(path: str) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    currencies = payload.get("currencies")
    if payload.get("baseCurrency") != "USD" or not isinstance(currencies, list):
        raise ValueError("invalid exchange-rate payload")
    if not currencies or not all(
        isinstance(item.get("code"), str)
        and isinstance(item.get("rate"), (int, float))
        and item["rate"] > 0
        for item in currencies
    ):
        raise ValueError("exchange-rate payload has invalid currencies")
    return payload


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: update_exchange_rates.py <payload.json>")

    credentials_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
    project_id = os.getenv("FIRESTORE_PROJECT_ID")
    if not credentials_json or not project_id:
        raise RuntimeError("Firestore credentials are not configured")

    credentials = service_account.Credentials.from_service_account_info(
        json.loads(credentials_json)
    )
    client = firestore.Client(project=project_id, credentials=credentials)
    payload = load_payload(sys.argv[1])
    client.collection("exchange_rates").document("latest").set(
        {
            **payload,
            "updatedAt": firestore.SERVER_TIMESTAMP,
            "source": "Frankfurter",
        }
    )


if __name__ == "__main__":
    main()
