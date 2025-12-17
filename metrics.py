# metrics.py
import os
import json
from datetime import datetime
from typing import Any, Dict, Optional

from dotenv import load_dotenv

load_dotenv()

try:
    import redis  # type: ignore
except ImportError:
    redis = None  # type: ignore

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore


REDIS_URL = os.getenv("REDIS_URL")

_redis_client = None


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


def _today_iso_in_target_tz() -> str:
    """
    Today's date in Europe/Zurich if possible, otherwise UTC date.
    (Muss identisch zur Credits-Logik sein: day-based, not hourly.)
    """
    now = datetime.utcnow()
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo("Europe/Zurich")
            now = datetime.now(tz)
        except Exception:
            pass
    return now.date().isoformat()


TOTAL_IMAGES_KEY = "metrics:total:images"
TOTAL_CREDITS_SPENT_KEY = "metrics:total:credits_spent"

PUBLIC_SNAPSHOT_KEY = "metrics:snapshot:public"
PUBLIC_SNAPSHOT_DATE_KEY = "metrics:snapshot:public:date"


def incr_credits_spent(amount: int) -> None:
    if amount <= 0:
        return
    client = _get_redis_client()
    if client is None:
        return
    client.incrby(TOTAL_CREDITS_SPENT_KEY, int(amount))


def incr_images_created(amount: int = 1) -> None:
    if amount <= 0:
        return
    client = _get_redis_client()
    if client is None:
        return
    client.incrby(TOTAL_IMAGES_KEY, int(amount))


def _read_totals() -> Dict[str, int]:
    client = _get_redis_client()
    if client is None:
        return {"images_total": 0, "credits_spent_total": 0}

    images_raw = client.get(TOTAL_IMAGES_KEY)
    credits_raw = client.get(TOTAL_CREDITS_SPENT_KEY)

    images_total = int(images_raw) if images_raw else 0
    credits_spent_total = int(credits_raw) if credits_raw else 0

    return {
        "images_total": images_total,
        "credits_spent_total": credits_spent_total,
    }


def get_public_metrics_snapshot() -> Dict[str, Any]:
    """
    Returns a snapshot that refreshes once per Zurich day (lazy refresh).
    """
    today = _today_iso_in_target_tz()
    client = _get_redis_client()

    # If Redis is not available, just return computed values (non-persistent)
    if client is None:
        totals = _read_totals()
        return {
            **totals,
            "updated_date": today,
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "refresh_policy": "daily",
        }

    snap_date = client.get(PUBLIC_SNAPSHOT_DATE_KEY)
    snap_json = client.get(PUBLIC_SNAPSHOT_KEY)

    if snap_date == today and snap_json:
        try:
            return json.loads(snap_json)
        except Exception:
            # fallback: regenerate
            pass

    # Regenerate snapshot (1x per day)
    totals = _read_totals()
    snapshot = {
        **totals,
        "updated_date": today,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "refresh_policy": "daily",
    }

    client.set(PUBLIC_SNAPSHOT_KEY, json.dumps(snapshot))
    client.set(PUBLIC_SNAPSHOT_DATE_KEY, today)

    return snapshot
