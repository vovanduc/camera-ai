# services/reid_worker/src/reid_worker/repo.py
"""pgvector writes/queries cho re-entry grouping. Body cosine = trục chính."""
from __future__ import annotations

from datetime import datetime

import asyncpg
import numpy as np


def _vec_literal(v: np.ndarray) -> str:
    return "[" + ",".join(f"{float(x):.6f}" for x in v) + "]"


def _parse_vec(s: str) -> np.ndarray:
    return np.fromstring(s.strip("[]"), sep=",", dtype=np.float32)


class ReidRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def cam_id_for(self, cam_uid: str) -> int | None:
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT id FROM cameras WHERE cam_uid = $1", cam_uid)
            return int(row["id"]) if row else None

    async def live_groups(self, cam_id: int, ttl_hours: float) -> list[dict]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                """
                SELECT id, rep_body_vector::text AS rep
                FROM person_group
                WHERE cam_id = $1 AND last_seen >= now() - ($2 || ' hours')::interval
                """,
                cam_id, str(ttl_hours),
            )
        return [{"id": int(r["id"]), "rep_body_vector": _parse_vec(r["rep"])} for r in rows]

    async def create_group(self, *, cam_id: int, ts: datetime, body_vec: np.ndarray,
                           face_vec: np.ndarray | None, track_id: str | None,
                           rep_crop_path: str | None) -> int:
        async with self.pool.acquire() as c:
            async with c.transaction():
                row = await c.fetchrow(
                    """
                    INSERT INTO person_group
                        (cam_id, first_seen, last_seen, visit_count,
                         rep_body_vector, rep_face_vector, rep_crop_path)
                    VALUES ($1, $2, $2, 1, $3::vector, $4::vector, $5)
                    RETURNING id
                    """,
                    cam_id, ts, _vec_literal(body_vec),
                    None if face_vec is None else _vec_literal(face_vec),
                    rep_crop_path,
                )
                gid = int(row["id"])
                await c.execute(
                    """
                    INSERT INTO appearance (group_id, cam_id, ts, body_vector, face_vector, track_id)
                    VALUES ($1, $2, $3, $4::vector, $5::vector, $6)
                    """,
                    gid, cam_id, ts, _vec_literal(body_vec),
                    None if face_vec is None else _vec_literal(face_vec), track_id,
                )
                return gid

    async def add_appearance_to_group(self, *, group_id: int, cam_id: int,
                                      ts: datetime, body_vec: np.ndarray,
                                      face_vec: np.ndarray | None,
                                      track_id: str | None) -> int:
        async with self.pool.acquire() as c:
            async with c.transaction():
                row = await c.fetchrow(
                    """
                    INSERT INTO appearance
                        (group_id, cam_id, ts, body_vector, face_vector, track_id)
                    VALUES ($1, $2, $3, $4::vector, $5::vector, $6)
                    RETURNING id
                    """,
                    group_id, cam_id, ts, _vec_literal(body_vec),
                    None if face_vec is None else _vec_literal(face_vec), track_id,
                )
                await c.execute(
                    """
                    UPDATE person_group
                    SET last_seen = $1, visit_count = visit_count + 1
                    WHERE id = $2
                    """,
                    ts, group_id,
                )
                return int(row["id"])

    async def latest_appearance_id_for_group(self, group_id: int) -> int:
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT id FROM appearance WHERE group_id = $1 ORDER BY id DESC LIMIT 1",
                group_id,
            )
            return int(row["id"])

    async def insert_crop(self, *, appearance_id: int, kind: str, path: str,
                          frame_idx: int, quality: float) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                """
                INSERT INTO appearance_crop (appearance_id, kind, path, frame_idx, quality)
                VALUES ($1, $2, $3, $4, $5)
                """,
                appearance_id, kind, path, frame_idx, float(quality),
            )

    async def purge_expired(self, ttl_hours: float) -> list[int]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "DELETE FROM person_group WHERE last_seen < now() - ($1 || ' hours')::interval "
                "RETURNING id",
                str(ttl_hours),
            )
        return [int(r["id"]) for r in rows]
