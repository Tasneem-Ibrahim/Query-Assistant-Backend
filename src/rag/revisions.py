"""Versions of administrative settings, with compare-and-swap reversals."""
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from heapq import merge

from fastapi import HTTPException

from . import cloud, config, security_store


def actor_header(request):
    return {"X-QA-Revision-Actor": request.state.user["id"]}


@contextmanager
def database():
    with security_store.database() as db:
        db.execute("""create table if not exists setting_revisions (
            id integer primary key autoincrement, created_at text not null, kind text not null,
            target text not null, actor_id text not null, before_state text not null,
            after_state text not null, reversed_id integer)""")
        yield db


def local_state(db, key):
    row = db.execute("select value, revision from security_state where key=?", (key,)).fetchone()
    return (json.loads(row["value"]), row["revision"]) if row else ({}, 0)


def settings_state(key, value):
    if key.startswith("policy:"):
        return {"modules": value.get("modules", {}), "actions": value.get("actions", {})}
    if key == "status:local-admin":
        return {"active": value.get("active", True)}
    return value


def add_local(db, key, before, after, actor, reversed_id=None):
    created = datetime.now(timezone.utc).isoformat()
    row = db.execute("""insert into setting_revisions
        (created_at, kind, target, actor_id, before_state, after_state, reversed_id) values (?, ?, ?, ?, ?, ?, ?)""",
        (created, "security", key, actor, json.dumps(before), json.dumps(after), reversed_id))
    if reversed_id is not None:
        category = "user-details" if key.startswith("status:") else "role-management"
        db.execute("insert into activity_events (created_at, category, actor_id, target, action) values (?, ?, ?, ?, ?)",
                   (created, category, actor, key.split(":", 1)[1], f"Reversed {display_id('L', reversed_id)}"))
    return row.lastrowid


def write_setting(request, key, state, revision):
    if config.CLOUD_MODE:
        return cloud._request("POST", "/rest/v1/rpc/qa_write_security_state", headers=actor_header(request),
                              json={"p_key": key, "p_value": state, "p_revision": revision}).json()
    with database() as db:
        db.execute("begin immediate")
        before, current = local_state(db, key)
        if current != revision or not security_store.write_local(db, key, state, revision):
            return False
        before, state = settings_state(key, before), settings_state(key, state)
        if before != state:
            add_local(db, key, before, state, request.state.user["id"])
        return True


def display_id(source, value):
    return f"RV-{source}{value:06d}"


def public_row(row, source):
    data = dict(row)
    if source == "L":
        for name in ("before_state", "after_state"):
            data[name] = json.loads(data[name])
    data["revision_id"] = display_id(source, data.pop("id"))
    reversed_id = data.pop("reversed_id")
    data["reversed_revision"] = display_id(source, reversed_id) if reversed_id is not None else None
    return data


def history(local_before=None, cloud_before=None):
    local_items, cloud_items = [], []
    cloud_available = True
    if not config.CLOUD_MODE:
        with database() as db:
            rows = db.execute("select * from setting_revisions where (? is null or id < ?) order by id desc limit 51",
                              (local_before, local_before))
            local_items = [("L", dict(row)) for row in rows]
    params = {"order": "id.desc", "limit": 51}
    if cloud_before is not None:
        params["id"] = f"lt.{cloud_before}"
    try:
        rows = cloud._request("GET", "/rest/v1/qa_setting_revisions", params=params, timeout=3).json()
        cloud_items = [("C", row) for row in rows]
    except (cloud.CloudStorageError, ValueError):
        if config.CLOUD_MODE:
            raise
        cloud_available = False
    combined = list(merge(local_items, cloud_items, key=lambda item: datetime.fromisoformat(item[1]["created_at"]), reverse=True))
    before = {"local_before": local_before, "cloud_before": cloud_before}
    for source, row in combined[:50]:
        before["local_before" if source == "L" else "cloud_before"] = row["id"]
    return {"revisions": [public_row(row, source) for source, row in combined[:50]],
            "next": before if len(combined) > 50 else None, "cloud_available": cloud_available}


def reverse(source, value, actor):
    if source == "C":
        result = cloud._request("POST", "/rest/v1/rpc/qa_reverse_setting_revision",
                                json={"p_id": value, "p_actor": actor}).json()
        if result["id"] < 0:
            raise HTTPException(404, "Revision not found.")
        if not result["id"]:
            raise HTTPException(409, "The current setting no longer matches this revision or cannot be restored.")
        if not config.CLOUD_MODE:
            security_store.record_activity(result["category"], actor, result["target"], f"Reversed {display_id(source, value)}")
        return display_id(source, result["id"])
    if config.CLOUD_MODE:
        raise HTTPException(404, "Revision not found.")
    with database() as db:
        db.execute("begin immediate")
        item = db.execute("select * from setting_revisions where id=?", (value,)).fetchone()
        if not item:
            raise HTTPException(404, "Revision not found.")
        key = item["target"]
        state, revision = local_state(db, key)
        state = settings_state(key, state)
        if state != json.loads(item["after_state"]):
            raise HTTPException(409, "The current setting no longer matches this revision.")
        previous = json.loads(item["before_state"])
        if not security_store.write_local(db, key, previous, revision):
            raise HTTPException(409, "The setting changed while it was being restored. Refresh and try again.")
        return display_id(source, add_local(db, key, state, previous, actor, value))
