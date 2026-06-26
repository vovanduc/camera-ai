import numpy as np
from reid_worker.embed import l2norm, fuse_embeddings, best_quality_idx, body_crop_ok

def test_l2norm_unit_length():
    v = np.array([3.0, 4.0], dtype=np.float32)
    n = l2norm(v)
    assert abs(np.linalg.norm(n) - 1.0) < 1e-6

def test_l2norm_zero_safe():
    n = l2norm(np.zeros(4, dtype=np.float32))
    assert not np.isnan(n).any()

def test_fuse_averages_then_normalizes():
    a = np.array([1.0, 0.0], dtype=np.float32)
    b = np.array([0.0, 1.0], dtype=np.float32)
    f = fuse_embeddings([a, b])
    assert abs(np.linalg.norm(f) - 1.0) < 1e-6
    # mean = [.5,.5] → normalized ~ [.707,.707]
    assert abs(f[0] - f[1]) < 1e-6

def test_fuse_single():
    a = np.array([0.0, 2.0], dtype=np.float32)
    f = fuse_embeddings([a])
    assert np.allclose(f, [0.0, 1.0])

def test_best_quality_idx():
    assert best_quality_idx([0.1, 0.9, 0.5]) == 1


# body_crop_ok: score = discriminator chính (data thật), px = chốt phụ.
def test_body_crop_ok_real_person():
    # người-thật: score cao + crop to → giữ
    assert body_crop_ok(0.82, 326, 546, 0.5, 96, 192) is True

def test_body_crop_ok_glass_door_rejected_by_score():
    # cửa kính: px qua được (121x189) nhưng score 0.0 → loại bằng score
    assert body_crop_ok(0.0, 121, 189, 0.5, 96, 192) is False

def test_body_crop_ok_none_score_rejected():
    # score absent (None) → coi như 0.0 → loại
    assert body_crop_ok(None, 300, 500, 0.5, 96, 192) is False

def test_body_crop_ok_tiny_partial_rejected_by_px():
    # partial nhỏ: dù giả định score cao, px nhỏ → loại bằng px
    assert body_crop_ok(0.7, 51, 40, 0.5, 96, 192) is False

def test_body_crop_ok_boundary_inclusive():
    # đúng ngưỡng → giữ (>=)
    assert body_crop_ok(0.5, 96, 192, 0.5, 96, 192) is True
