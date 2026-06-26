"""Gom object-snapshot theo track_id thành 1 Appearance (1 lượt qua cửa)."""
from __future__ import annotations

_FACE_CLASSES = {"HumanFace", "Head", "Face"}


class Assembler:
    def __init__(self, track_timeout_ms: int = 3000) -> None:
        self.track_timeout_ms = track_timeout_ms
        self._buf: dict[str, list[dict]] = {}
        self._last_arrival: dict[str, int] = {}   # wall-clock, cho timeout

    def add(self, obj: dict, arrival_ms: int) -> None:
        t = obj["track_id"]
        self._buf.setdefault(t, []).append(obj)
        self._last_arrival[t] = max(self._last_arrival.get(t, 0), arrival_ms)

    def _build(self, track_id: str) -> dict:
        objs = self._buf[track_id]
        body = [o for o in objs if o["cls"] not in _FACE_CLASSES]
        face = [o for o in objs if o["cls"] in _FACE_CLASSES]
        return {
            "track_id": track_id,
            "ts_ms": objs[-1]["ts_ms"],   # camera-clock crop cuối → field ts lưu DB
            "body_objs": body,
            "face_objs": face,
        }

    def _pop(self, track_id: str) -> dict:
        ap = self._build(track_id)
        del self._buf[track_id]
        del self._last_arrival[track_id]
        return ap

    def flush_expired(self, now_ms: int) -> list[dict]:
        expired = [t for t, last in self._last_arrival.items()
                   if now_ms - last > self.track_timeout_ms]
        return [self._pop(t) for t in expired]

    def flush_all(self) -> list[dict]:
        return [self._pop(t) for t in list(self._buf.keys())]
