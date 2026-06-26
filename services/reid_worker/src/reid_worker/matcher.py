"""Online group assignment: body cosine vs live group reps. Body = trục chính."""
from __future__ import annotations

import numpy as np


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    return float(np.dot(a, b) / ((np.linalg.norm(a) * np.linalg.norm(b)) + 1e-9))


def decide_match(body_vec: np.ndarray, live_groups: list[dict],
                 body_threshold: float) -> dict:
    """Trả {'group_id': int|None, 'similarity': float}.

    group_id=None ⇒ người mới (tạo group). Ngược lại ⇒ join group cosine cao nhất ≥ threshold.
    """
    best_id, best_sim = None, 0.0
    for g in live_groups:
        sim = cosine(body_vec, g["rep_body_vector"])
        if sim > best_sim:
            best_sim, best_id = sim, g["id"]
    if best_id is not None and best_sim >= body_threshold:
        return {"group_id": int(best_id), "similarity": best_sim}
    return {"group_id": None, "similarity": best_sim}
