from datetime import timezone

from event_collector.parser import parse_event


def _payload(ts_ms: int = 1_700_000_000_000) -> dict:
    return {"timestamp": ts_ms, "serial": "B8A44F4627CE",
            "message": {"source": {}, "key": {}, "data": {"totalHuman": 3}}}


def test_scenario1_is_in():
    topic = "axis/B8A44F4627CE/event/.../ObjectAnalytics/Device1Scenario1"
    ev = parse_event(topic, _payload())
    assert ev["type"] == "counter"
    assert ev["direction"] == "in"
    assert ev["cam_uid"] == "B8A44F4627CE"
    assert ev["ts"].tzinfo == timezone.utc


def test_scenario2_is_out():
    topic = "axis/B8A44F4627CE/event/.../ObjectAnalytics/Device1Scenario2"
    ev = parse_event(topic, _payload())
    assert ev["direction"] == "out"


def test_interval_topic_ignored_as_counter():
    topic = "axis/B8A44F4627CE/.../ObjectAnalytics/Device1Scenario1Interval"
    ev = parse_event(topic, _payload())
    # 'Interval' loại khỏi counter; topic không match motion/health -> None
    assert ev is None


def test_bad_timestamp_returns_none():
    assert parse_event("axis/x/.../Device1Scenario1", {"no": "timestamp"}) is None


def test_unknown_topic_returns_none():
    assert parse_event("axis/x/event/Something/Else", _payload()) is None
