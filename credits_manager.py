# credits_manager.py

import os
from datetime import datetime
from typing import Dict, Any

from dotenv import load_dotenv

load_dotenv()

# Try to import redis. If not available or no REDIS_URL, fall back to in-memory store.
try:
    import redis  # type: ignore
except ImportError:
    redis = None  # type: ignore

# Optional timezone support
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore

REDIS_URL = os.getenv("REDIS_URL")

_redis_client = None
_in_memory_store: Dict[str, Dict[str, Any]] = {}


def _get_redis_client():
    global _redis_client
    if REDIS_URL and redis is not None:
        if _redis_client is None:
            _redis_client = redis.Redis.from_url(
                REDIS_URL,
                decode_responses=True,
            )
        return _redis_client
    return None


def _make_key(user_id: str) -> str:
    # Generic key for a logical "user" – in der alten Welt war das client_id,
    # in der neuen Welt ist es user_id. Die Struktur bleibt kompatibel.
    return f"ew:user:{user_id}"


class NoCreditsError(Exception):
    """Raised when a user has no credits left."""
    pass


def _load_user_record(user_id: str) -> Dict[str, Any]:
    client = _get_redis_client()
    key = _make_key(user_id)

    if client is not None:
        data = client.hgetall(key) or {}
    else:
        data = _in_memory_store.get(key, {})

    # Normalize types
    paid = int(data.get("paid_credits", 0))
    free = int(data.get("free_credits", 0))
    last_refill = data.get("last_free_refill")

    return {
        "paid_credits": paid,
        "free_credits": free,
        "last_free_refill": last_refill,
    }


def _save_user_record(user_id: str, record: Dict[str, Any]) -> None:
    client = _get_redis_client()
    key = _make_key(user_id)

    data = {
        "paid_credits": str(int(record.get("paid_credits", 0))),
        "free_credits": str(int(record.get("free_credits", 0))),
        "last_free_refill": record.get("last_free_refill") or "",
    }

    if client is not None:
        client.hset(key, mapping=data)
    else:
        _in_memory_store[key] = data


def _today_iso_in_target_tz() -> str:
    """
    Try to get today's date in Europe/Zurich.
    If the timezone is not available, fall back to UTC/naive datetime.
    """
    now = datetime.utcnow()

    if "Europe/Zurich" and "ZoneInfo" in globals() and ZoneInfo is not None:
        try:
            tz = ZoneInfo("Europe/Zurich")
            now = datetime.now(tz)
        except Exception:
            # Fallback: keep 'now' as UTC
            pass

    return now.date().isoformat()


def refresh_free_credits(user_id: str) -> Dict[str, Any]:
    """
    Ensure the free tier is correctly reset once per day (Europe/Zurich if possible).
    Free credits do NOT stack. They are set back to 5 if:
      - a new day has started, and
      - the user has no paid credits.
    """
    record = _load_user_record(user_id)

    today = _today_iso_in_target_tz()

    paid = int(record.get("paid_credits", 0))
    last_refill = record.get("last_free_refill")

    if paid == 0 and last_refill != today:
        # New day for this user and no paid credits: reset free credits to 5
        record["free_credits"] = 5
        record["last_free_refill"] = today

    _save_user_record(user_id, record)
    return record


def consume_credit_or_fail(user_id: str) -> Dict[str, Any]:
    """
    Consume exactly one credit. Priority:
      1) paid_credits
      2) free_credits
    If both are zero, raise NoCreditsError.
    """
    record = refresh_free_credits(user_id)

    paid = int(record.get("paid_credits", 0))
    free = int(record.get("free_credits", 0))

    if paid <= 0 and free <= 0:
        raise NoCreditsError("No credits available")

    if paid > 0:
        record["paid_credits"] = paid - 1
    else:
        record["free_credits"] = max(free - 1, 0)

    _save_user_record(user_id, record)
    return record


def add_paid_credits(user_id: str, amount: int) -> Dict[str, Any]:
    """
    Add paid credits (from Stripe purchases or subscriptions).
    Credits are stackable and do not expire.
    """
    if amount <= 0:
        return get_credit_status(user_id)

    record = _load_user_record(user_id)
    paid = int(record.get("paid_credits", 0))
    record["paid_credits"] = paid + amount
    _save_user_record(user_id, record)
    return record


def get_credit_status(user_id: str) -> Dict[str, int]:
    """
    Return the current credit status for the user.
    """
    record = refresh_free_credits(user_id)
    paid = int(record.get("paid_credits", 0))
    free = int(record.get("free_credits", 0))
    total = paid + free
    return {
        "paid_credits": paid,
        "free_credits": free,
        "total_credits": total,
    }
