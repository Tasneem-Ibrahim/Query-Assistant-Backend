"""Persistence for security settings, authenticator state and login events."""
import json
import logging
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone

from . import cloud, config

logger = logging.getLogger(__name__)
ACTIVITY_CATEGORIES = ("user-details", "knowledge-base", "role-management")


@contextmanager
def database():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(config.DATA_DIR / "security.sqlite3", timeout=10)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("create table if not exists security_state (key text primary key, value text not null, revision integer not null)")
        db.execute("""create table if not exists login_events (
            id integer primary key autoincrement, created_at text not null, account_id text, role text, outcome text not null)""")
        db.execute("""create table if not exists activity_events (
            id integer primary key autoincrement, created_at text not null, category text not null,
            actor_id text not null, target text, action text not null)""")
        db.execute("create index if not exists activity_events_category_id on activity_events(category, id desc)")
        yield db


def read(key):
    if config.CLOUD_MODE:
        rows = cloud.rows("qa_security_state", key="eq." + key, select="value,revision")
        return (rows[0]["value"], rows[0]["revision"]) if rows else ({}, 0)
    with database() as db:
        row = db.execute("select value, revision from security_state where key=?", (key,)).fetchone()
        return (json.loads(row["value"]), row["revision"]) if row else ({}, 0)


def write(key, state, revision):
    if config.CLOUD_MODE:
        return cloud._request("POST", "/rest/v1/rpc/qa_write_security_state",
                              json={"p_key": key, "p_value": state, "p_revision": revision}).json()
    with database() as db:
        return write_local(db, key, state, revision)


def write_local(db, key, state, revision):
    if revision == 0:
        result = db.execute("insert or ignore into security_state values (?, ?, 1)", (key, json.dumps(state)))
    else:
        result = db.execute("update security_state set value=?, revision=revision+1 where key=? and revision=?",
                            (json.dumps(state), key, revision))
    return result.rowcount == 1


def log(account, outcome):
    row = {"created_at": datetime.now(timezone.utc).isoformat(), "account_id": account.get("id"),
           "role": account.get("role"), "outcome": outcome}
    if config.CLOUD_MODE:
        cloud._request("POST", "/rest/v1/qa_login_events", json=row)
    else:
        with database() as db:
            db.execute("insert into login_events (created_at, account_id, role, outcome) values (?, ?, ?, ?)",
                       tuple(row.values()))


def remove_factor(owner):
    key = "factor:" + owner
    if config.CLOUD_MODE:
        cloud.remove("qa_security_state", key="eq." + key)
    else:
        with database() as db:
            db.execute("delete from security_state where key=?", (key,))


def events(limit, before=None):
    if config.CLOUD_MODE:
        params = {"order": "id.desc", "limit": limit}
        if before is not None:
            params["id"] = f"lt.{before}"
        return cloud._request("GET", "/rest/v1/qa_login_events", params=params).json()
    with database() as db:
        return [dict(row) for row in db.execute(
            "select * from login_events where (? is null or id < ?) order by id desc limit ?", (before, before, limit))]


def log_logout(account):
    try:
        log(account, "signed_out")
    except (cloud.CloudStorageError, sqlite3.Error, OSError, ValueError):
        logger.warning("Unable to record logout activity.")


def record_activity(category, actor_id, target, action):
    row = {"created_at": datetime.now(timezone.utc).isoformat(), "category": category,
           "actor_id": actor_id, "target": target, "action": action}
    try:
        if config.CLOUD_MODE:
            cloud._request("POST", "/rest/v1/qa_activity_events", json=row, timeout=3)
        else:
            with database() as db:
                db.execute("insert into activity_events (created_at, category, actor_id, target, action) values (?, ?, ?, ?, ?)",
                           tuple(row.values()))
    except (cloud.CloudStorageError, sqlite3.Error, OSError, ValueError):
        logger.warning("Unable to record %s activity.", category)


def activity_events(category, limit, before=None):
    if config.CLOUD_MODE:
        filters = {"category": f"eq.{category}", "order": "id.desc", "limit": limit}
        if before is not None:
            filters["id"] = f"lt.{before}"
        return cloud._request("GET", "/rest/v1/qa_activity_events", params=filters).json()
    with database() as db:
        return [dict(row) for row in db.execute(
            "select * from activity_events where category=? and (? is null or id < ?) order by id desc limit ?",
            (category, before, before, limit))]
