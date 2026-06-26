"""Parse Axis MQTT events into typed dicts the repo can persist.

Topic structure on broker:

    axis/<SERIAL>/event/<NAMESPACE_PREFIX>/<EVENT_PATH>
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _cam_uid_from_topic(topic: str) -> str:
    parts = topic.split("/")
    return parts[1] if len(parts) >= 2 else "unknown"


def _ms_to_dt(ms: int | float) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)


def parse_event(topic: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return a normalized event dict, or None for unsupported topics."""
    cam_uid = _cam_uid_from_topic(topic)
    try:
        ts = _ms_to_dt(payload["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    data = (payload.get("message") or {}).get("data") or {}

    # Counter — Object Analytics line crossing.
    if "ObjectAnalytics/Device1Scenario1" in topic and "Interval" not in topic \
            and "Passthrough" not in topic:
        return {
            "type": "counter", "cam_uid": cam_uid, "ts": ts,
            "direction": "in", "scenario": "IN", "data": data, "raw": payload,
        }
    if "ObjectAnalytics/Device1Scenario2" in topic and "Interval" not in topic \
            and "Passthrough" not in topic:
        return {
            "type": "counter", "cam_uid": cam_uid, "ts": ts,
            "direction": "out", "scenario": "OUT", "data": data, "raw": payload,
        }

    # Motion (VMD or ONVIF MotionRegionDetector / MotionAlarm).
    if "/VMD/" in topic or "Motion" in topic:
        active_raw = data.get("active") or data.get("State") or data.get("state")
        return {
            "type": "motion", "cam_uid": cam_uid, "ts": ts,
            "direction": None, "scenario": None,
            "data": {"active": str(active_raw) in {"1", "true", "True"}, **data},
            "raw": payload,
        }

    # Image Health (tamper, blur, defocus, low light, etc.).
    if "ImageHealth" in topic:
        return {
            "type": "health", "cam_uid": cam_uid, "ts": ts,
            "direction": None, "scenario": None, "data": data, "raw": payload,
        }

    return None
