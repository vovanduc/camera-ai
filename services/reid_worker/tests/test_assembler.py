from reid_worker.assembler import Assembler

def _obj(track, cls, ts_ms, jpeg=b"\xff\xd8x"):
    return {"track_id": track, "cls": cls, "score": 0.9, "ts_ms": ts_ms,
            "jpeg": jpeg, "crop_box": None}

# Timeout đo theo arrival_ms (wall-clock), KHÔNG theo ts_ms (camera-clock).
# ts_ms ở đây cố tình lệch arrival_ms để chứng minh timeout không dùng nó.
def test_groups_multi_frame_by_track():
    a = Assembler(track_timeout_ms=2000)
    a.add(_obj("t1", "Human", ts_ms=999999), arrival_ms=1000)
    a.add(_obj("t1", "Human", ts_ms=0), arrival_ms=1500)        # ts_ms=0 (parse-fail) vô hại
    a.add(_obj("t1", "HumanFace", ts_ms=12345), arrival_ms=1600)
    out = a.flush_expired(now_ms=4000)  # 4000 - 1600 = 2400 > 2000 → expired
    assert len(out) == 1
    ap = out[0]
    assert ap["track_id"] == "t1"
    assert len(ap["body_objs"]) == 2
    assert len(ap["face_objs"]) == 1
    assert ap["ts_ms"] == 12345          # ts cho DB = camera-clock crop cuối thêm vào

def test_not_expired_stays():
    a = Assembler(track_timeout_ms=2000)
    a.add(_obj("t1", "Human", ts_ms=1), arrival_ms=1000)
    assert a.flush_expired(now_ms=2500) == []   # 1500 <= 2000
    assert a.add(_obj("t1", "Human", ts_ms=2), arrival_ms=2400) is None

def test_two_tracks_independent():
    a = Assembler(track_timeout_ms=2000)
    a.add(_obj("t1", "Human", ts_ms=1), arrival_ms=1000)
    a.add(_obj("t2", "Human", ts_ms=2), arrival_ms=3000)
    out = a.flush_expired(now_ms=3500)   # t1 expired (2500>2000), t2 not (500)
    assert [ap["track_id"] for ap in out] == ["t1"]

def test_flush_all():
    a = Assembler(track_timeout_ms=2000)
    a.add(_obj("t1", "Human", ts_ms=1), arrival_ms=1000)
    a.add(_obj("t2", "Head", ts_ms=2), arrival_ms=1000)
    out = a.flush_all()
    assert {ap["track_id"] for ap in out} == {"t1", "t2"}
    assert a.flush_all() == []
