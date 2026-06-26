"""Body Re-ID (OSNet) + face vote (InsightFace) + multi-frame fuse.

Body = trục chính. Face = vote phụ, chỉ khi crop mặt đủ chất lượng.
OSNet osnet_x1_0 → 512-d. InsightFace w600k_mbf → 512-d. Khớp vector(512).
"""
from __future__ import annotations

import io
from typing import Any

import numpy as np


# ---------- pure helpers (TDD) ----------
def l2norm(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def fuse_embeddings(embs: list[np.ndarray]) -> np.ndarray:
    """Average đa frame rồi L2-normalize → 1 chữ ký bền."""
    mean = np.mean(np.vstack([np.asarray(e, dtype=np.float32) for e in embs]), axis=0)
    return l2norm(mean)


def best_quality_idx(qualities: list[float]) -> int:
    return int(np.argmax(qualities)) if qualities else 0


def body_crop_ok(score: float | None, w: int, h: int,
                 score_min: float, min_w: int, min_h: int) -> bool:
    """Lọc crop body trước khi embed.

    `score` = class confidence object_snapshot. Data thật (2026-06-25): người-thật
    crop sạch có score 0.6–0.92; junk/non-person (cửa kính, partial, sàn) score ~0.0.
    → score là discriminator chính. Px là chốt chặn phụ (partial nhỏ).
    """
    s = 0.0 if score is None else float(score)
    return s >= score_min and w >= min_w and h >= min_h


def _jpeg_to_bgr(jpeg: bytes) -> np.ndarray:
    import cv2
    arr = np.frombuffer(jpeg, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


# ---------- live models ----------
class BodyEmbedder:
    """OSNet body Re-ID embedding (torchreid FeatureExtractor, CPU)."""

    def __init__(self, model_name: str = "osnet_x1_0", device: str = "cpu") -> None:
        # ⚠️ Import path đổi giữa các release torchreid: thử `torchreid.utils` trước,
        # fallback `torchreid.reid.utils` (sẽ lộ ngay khi load ở Task 8).
        try:
            from torchreid.utils import FeatureExtractor
        except ImportError:
            from torchreid.reid.utils import FeatureExtractor
        self.extractor = FeatureExtractor(model_name=model_name, device=device)

    def extract(self, jpeg: bytes) -> np.ndarray:
        import cv2
        bgr = _jpeg_to_bgr(jpeg)
        # OSNet train trên RGB (chuẩn ImageNet) → convert; FeatureExtractor tự resize 256×128.
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        feat = self.extractor(rgb)               # torch tensor (1, 512)
        v = feat.cpu().numpy().reshape(-1).astype(np.float32)
        return l2norm(v)


class FaceEmbedder:
    """InsightFace buffalo_s detect + ArcFace embed. Trả (emb, quality) hoặc None."""

    def __init__(self, det_size: tuple[int, int] = (640, 640),
                 quality_min: float = 0.2) -> None:
        from insightface.app import FaceAnalysis
        self.quality_min = quality_min
        self.app = FaceAnalysis(name="buffalo_s",
                                providers=["CPUExecutionProvider"],
                                allowed_modules=["detection", "recognition"])
        self.app.prepare(ctx_id=-1, det_size=det_size)

    def extract(self, jpeg: bytes) -> tuple[np.ndarray, float] | None:
        import cv2
        img = _jpeg_to_bgr(jpeg)
        faces = self.app.get(img)
        if not faces:
            return None
        f = max(faces, key=lambda x: (x.bbox[2]-x.bbox[0])*(x.bbox[3]-x.bbox[1])*x.det_score)
        emb = getattr(f, "normed_embedding", None)
        if emb is None:
            return None
        h = int(f.bbox[3] - f.bbox[1])
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        sharp = min(1.0, cv2.Laplacian(gray, cv2.CV_64F).var() / 500.0)
        quality = float(0.4 * sharp + 0.4 * min(1.0, h / 120.0) + 0.2 * float(f.det_score))
        if quality < self.quality_min:
            return None
        return l2norm(np.asarray(emb, dtype=np.float32)), quality


def embed_appearance(ap: dict, body: BodyEmbedder,
                     face: "FaceEmbedder | None", *,
                     body_score_min: float = 0.5,
                     body_min_w: int = 96, body_min_h: int = 192) -> dict | None:
    """Appearance → fused body vector (+ face vote nếu có). None nếu không có body crop.

    KHÔNG fallback face→body: OSNet-of-face-crop làm rep body = match trục chính bằng rác.
    Thiếu body crop → skip appearance.

    Lọc crop body bằng score class + px (xem `body_crop_ok`): diệt non-person/junk
    (cửa kính, partial, sàn) trước khi embed → chống group rác + false-merge junk-to-junk.
    Toàn bộ crop fail filter → return None (KHÔNG tạo group từ rác).
    """
    if not ap["body_objs"]:
        return None
    body_embs, crops = [], []
    for i, o in enumerate(ap["body_objs"]):
        try:
            bgr = _jpeg_to_bgr(o["jpeg"])
            if bgr is None:
                continue
            h, w = bgr.shape[:2]
            if not body_crop_ok(o.get("score"), w, h, body_score_min, body_min_w, body_min_h):
                continue
            body_embs.append(body.extract(o["jpeg"]))
            crops.append(("body", o["jpeg"], i, o.get("score") or 0.0))
        except Exception:
            continue
    if not body_embs:
        return None
    body_vec = fuse_embeddings(body_embs)

    face_vec = None
    if face is not None and ap["face_objs"]:
        face_embs, face_q = [], []
        for j, o in enumerate(ap["face_objs"]):
            r = face.extract(o["jpeg"])
            if r is not None:
                face_embs.append(r[0])
                face_q.append(r[1])
                crops.append(("face", o["jpeg"], j, r[1]))
        if face_embs:
            face_vec = fuse_embeddings(face_embs)

    return {"body_vector": body_vec, "face_vector": face_vec, "crops": crops}
