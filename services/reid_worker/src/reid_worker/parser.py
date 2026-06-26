"""Axis object-snapshot MQTT payload → normalized ObjSnap dict (pure)."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any


def _ts_ms(raw: Any) -> int:
    """Axis 'timestamp' (ISO-8601 str) → epoch ms. Best-effort; 0 nếu parse fail."""
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str) and raw:
        try:
            s = raw.replace("Z", "+00:00")
            return int(datetime.fromisoformat(s).timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def parse_objsnap(payload: dict) -> dict | None:
    """Return ObjSnap dict or None if missing jpeg / track_id."""
    b64 = payload.get("data")
    track = payload.get("object_track_id")
    if not b64 or not track:
        return None
    try:
        jpeg = base64.b64decode(b64)
    except (ValueError, TypeError):
        return None
    cls_obj = payload.get("class") or {}
    return {
        "track_id": str(track),
        "cls": cls_obj.get("type") or "obj",
        "score": (float(cls_obj["score"]) if cls_obj.get("score") is not None else None),
        "ts_ms": _ts_ms(payload.get("timestamp")),
        "jpeg": jpeg,
        "crop_box": payload.get("crop_box"),
    }
