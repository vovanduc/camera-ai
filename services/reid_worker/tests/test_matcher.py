import numpy as np
from reid_worker.matcher import cosine, decide_match

def _n(v):
    v = np.asarray(v, dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)

def test_cosine_identical():
    a = _n([1, 2, 3])
    assert abs(cosine(a, a) - 1.0) < 1e-6

def test_join_best_group_above_threshold():
    new = _n([1, 0, 0])
    groups = [
        {"id": 10, "rep_body_vector": _n([0.9, 0.1, 0])},   # high sim
        {"id": 20, "rep_body_vector": _n([0, 1, 0])},        # low sim
    ]
    r = decide_match(new, groups, body_threshold=0.6)
    assert r["group_id"] == 10
    assert r["similarity"] > 0.6

def test_new_group_when_below_threshold():
    new = _n([1, 0, 0])
    groups = [{"id": 10, "rep_body_vector": _n([0, 1, 0])}]
    r = decide_match(new, groups, body_threshold=0.6)
    assert r["group_id"] is None
    assert isinstance(r["similarity"], float)
    assert r["similarity"] >= 0.0

def test_new_group_when_no_live_groups():
    r = decide_match(_n([1, 0, 0]), [], body_threshold=0.6)
    assert r["group_id"] is None
    assert r["similarity"] == 0.0

def test_picks_highest_among_several_above():
    new = _n([1, 0, 0])
    groups = [
        {"id": 1, "rep_body_vector": _n([0.7, 0.7, 0])},   # ~0.707
        {"id": 2, "rep_body_vector": _n([0.99, 0.01, 0])}, # ~0.9999
    ]
    r = decide_match(new, groups, body_threshold=0.6)
    assert r["group_id"] == 2

def test_picks_highest_when_high_listed_first():
    new = _n([1, 0, 0])
    groups = [
        {"id": 2, "rep_body_vector": _n([0.99, 0.01, 0])},  # high sim, FIRST
        {"id": 1, "rep_body_vector": _n([0.7, 0.7, 0])},    # lower sim (still >0.6), second
    ]
    r = decide_match(new, groups, body_threshold=0.6)
    assert r["group_id"] == 2  # argmax, not first-above-threshold
