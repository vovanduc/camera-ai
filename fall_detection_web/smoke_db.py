"""Smoke test db.py trên Postgres (FDW không có pytest). Chạy: python smoke_db.py
Yêu cầu: DATABASE_URL trỏ Postgres sạch."""
import db

def main():
    db.init_db()
    # settings
    db.set_setting("k1", "v1")
    assert db.get_setting("k1") == "v1", "get_setting fail"
    db.set_settings_bulk({"k2": "v2", "k3": "v3"})
    alls = db.get_all_settings()
    assert alls.get("k2") == "v2" and alls.get("k3") == "v3", "bulk fail"
    db.delete_setting("k1")
    assert db.get_setting("k1", "MISSING") == "MISSING", "delete_setting fail"
    # users
    db.create_user("admin", "hash123")
    u = db.get_user("admin")
    assert u and u["username"] == "admin" and u["password_hash"] == "hash123", "user fail"
    assert any(x["username"] == "admin" for x in db.list_users()), "list_users fail"
    # incidents (insert without image)
    r = db.insert_event("verified", image_path=None, save_image=False,
                        camera="cam1", confidence=0.9, ai_result="EMERGENCY",
                        message="smoke")
    assert isinstance(r["id"], int) and r["id"] > 0, "insert_event id fail"
    evs = db.get_events(limit=10)
    assert any(e["id"] == r["id"] and e["camera"] == "cam1" for e in evs), "get_events fail"
    assert db.count_events() >= 1, "count_events fail"
    assert db.get_events_total(ai_result="EMERGENCY") >= 1, "events_total fail"
    trends = db.get_incident_trends(7)
    assert isinstance(trends, list), "trends fail"
    print("SMOKE OK: settings/users/incidents CRUD trên Postgres pass")

if __name__ == "__main__":
    main()
