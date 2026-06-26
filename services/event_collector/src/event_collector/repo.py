"""Persist parsed events to PostgreSQL (store-only; no occupancy table, no pg_notify)."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import asyncpg


class Repo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def cam_id_for(self, cam_uid: str) -> int | None:
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT id FROM cameras WHERE cam_uid = $1", cam_uid)
            return int(row["id"]) if row else None

    async def ensure_cam(self, cam_uid: str, name: str, rtsp_url: str) -> int:
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                """
                INSERT INTO cameras (cam_uid, name, rtsp_url)
                VALUES ($1, $2, $3)
                ON CONFLICT (cam_uid) DO UPDATE SET name = EXCLUDED.name
                RETURNING id
                """,
                cam_uid, name, rtsp_url,
            )
            return int(row["id"])

    async def insert_counter(
        self, *, cam_id: int, ts: datetime, direction: str,
        scenario: str, data: dict[str, Any], raw: dict[str, Any],
    ) -> int | None:
        """Insert one counter crossing row. Returns event_id, or None if duplicate.

        Counting is done at query time (COUNT of rows). We store the full raw
        envelope (incl. cumulative totalHuman) for audit only.
        """
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                """
                INSERT INTO events
                    (cam_id, ts, type, direction, axis_object_id, payload)
                VALUES ($1, $2, 'counter', $3, $4, $5)
                ON CONFLICT (cam_id, axis_object_id, ts, direction) DO NOTHING
                RETURNING id
                """,
                cam_id, ts, direction, scenario, json.dumps(raw),
            )
            return int(row["id"]) if row else None
