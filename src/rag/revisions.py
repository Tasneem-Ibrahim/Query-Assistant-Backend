"""Versions of administrative settings, with compare-and-swap reversals."""
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from heapq import merge

from fastapi import HTTPException

from . import cloud, config, security_store


def actor_header(request):
    headers = {"X-QA-Revision-Actor": request.state.user["id"]}
    snapshot = getattr(request.state, "delegation_snapshot", None)
    if snapshot is not None:
        headers["X-QA-Delegation"] = json.dumps(snapshot, separators=(",", ":"))
    return headers


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
        return {"modules": {}, "actions": {}, **value}
    if key == "status:local-admin":
        return {"active": value.get("active", True)}
    return value


def add_local(db, key, before, after, actor, reversed_id=None):
    created = datetime.now(timezone.utc).isoformat()
    row = db.execute("""insert into setting_revisions
        (created_at, kind, target, actor_id, before_state, after_state, reversed_id) values (?, ?, ?, ?, ?, ?, ?)""",
        (created, "security", key, actor, json.dumps(before), json.dumps(after), reversed_id))
    if reversed_id is not None or key.startswith(("policy:", "account:")):
        category = "user-details" if key.startswith("status:") else "role-management"
        action = (f"Reversed {display_id('L', reversed_id)}" if reversed_id is not None
                  else "Updated role permissions" if key.startswith("policy:") else "Updated original Admin permissions")
        db.execute("insert into activity_events (created_at, category, actor_id, target, action) values (?, ?, ?, ?, ?)",
                   (created, category, actor, key.split(":", 1)[1], action))
    return row.lastrowid


def write_setting(request, key, state, revision):
    if config.CLOUD_MODE:
        function = ("qa_write_delegated_policy" if getattr(request.state, "delegation_snapshot", None) is not None
                    else "qa_write_permission_state" if key.startswith(("policy:", "account:")) else "qa_write_security_state")
        return cloud._request("POST", "/rest/v1/rpc/" + function, headers=actor_header(request),
                              json={"p_key": key, "p_value": state, "p_revision": revision}).json()
    existing = getattr(request.state, "security_db", None)
    if existing is not None:
        return write_local_setting(existing, request, key, state, revision)
    with database() as db:
        db.execute("begin immediate")
        return write_local_setting(db, request, key, state, revision)


def write_local_setting(db, request, key, state, revision):
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


def lookup(source, value, db=None):
    if source == "C":
        rows = cloud._request("GET", "/rest/v1/qa_setting_revisions", params={"id": f"eq.{value}"}).json()
        return rows[0] if rows else None
    if db is None:
        return None
    row = db.execute("select * from setting_revisions where id=?", (value,)).fetchone()
    return public_row(row, source) if row else None


def reverse(source, value, actor, request=None):
    if source == "C":
        headers = actor_header(request) if request is not None else {}
        function = "qa_reverse_delegated_change" if request is not None else "qa_reverse_setting_revision"
        result = cloud._request("POST", "/rest/v1/rpc/" + function,
                                json={"p_id": value, "p_actor": actor},
                                **({"headers": headers} if headers else {})).json()
        if result["id"] < 0:
            raise HTTPException(404, "Revision not found.")
        if not result["id"]:
            raise HTTPException(409, "The current setting no longer matches this revision or cannot be restored.")
        if not config.CLOUD_MODE and request is None:
            security_store.record_activity(result["category"], actor, result["target"], f"Reversed {display_id(source, value)}")
        elif not config.CLOUD_MODE:
            request.state.activity_target = result["target"]
            request.state.activity_events = [(result["category"], f"Reversed {display_id(source, value)}")]
        return display_id(source, result["id"])
    if config.CLOUD_MODE:
        raise HTTPException(404, "Revision not found.")
    existing = getattr(request.state, "security_db", None) if request is not None else None
    if existing is not None:
        return reverse_local(existing, value, actor)
    with database() as db:
        db.execute("begin immediate")
        return reverse_local(db, value, actor)


def reverse_local(db, value, actor):
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
    return display_id("L", add_local(db, key, state, previous, actor, value))
