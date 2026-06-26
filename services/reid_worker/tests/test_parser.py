import base64
from reid_worker.parser import parse_objsnap

_JPEG = base64.b64encode(b"\xff\xd8\xff\xe0fakejpeg").decode()

def test_parse_valid_human():
    p = {"data": _JPEG, "class": {"type": "Human", "score": 0.91},
         "object_track_id": "track-abc", "timestamp": "2026-06-24T10:00:00.500Z",
         "crop_box": {"x": 1}}
    o = parse_objsnap(p)
    assert o is not None
    assert o["track_id"] == "track-abc"
    assert o["cls"] == "Human"
    assert abs(o["score"] - 0.91) < 1e-6
    assert o["jpeg"].startswith(b"\xff\xd8")
    assert o["crop_box"] == {"x": 1}

def test_parse_missing_jpeg_returns_none():
    assert parse_objsnap({"object_track_id": "t", "class": {"type": "Human"}}) is None

def test_parse_missing_track_returns_none():
    assert parse_objsnap({"data": _JPEG, "class": {"type": "Human"}}) is None

def test_parse_face_class():
    p = {"data": _JPEG, "class": {"type": "HumanFace"}, "object_track_id": "t2",
         "timestamp": "2026-06-24T10:00:01Z"}
    o = parse_objsnap(p)
    assert o["cls"] == "HumanFace"
    assert o["score"] is None
