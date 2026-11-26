"""Append-only annotations for the audit and revision displays."""
import re
from contextlib import nullcontext
from datetime import datetime, timezone

from fastapi import HTTPException

from . import cloud, config, revisions, security_store

MANUAL = re.compile(r"manual:([1-9][0-9]{0,17})")
LOGIN = re.compile(r"login:([1-9][0-9]{0,17})")
ACTIVITY = re.compile(r"activity:(user-details|knowledge-base|role-management):([1-9][0-9]{0,17})")
REVISION = re.compile(r"revision:([LC]):([1-9][0-9]{0,17})")
REFERENCE = {"logs": (MANUAL, LOGIN, ACTIVITY), "revisions": (MANUAL, REVISION)}


def database_table(db):
    db.execute("""create table if not exists audit_journal (
        id integer primary key autoincrement, created_at text not null,
        resource text not null, operation text not null, reference text,
        actor_id text not null, subject text, note text not null)""")
    db.execute("""create index if not exists audit_journal_reference
        on audit_journal(resource, reference, id desc)""")


def lookup(reference, resource, db=None):
    manual = MANUAL.fullmatch(reference)
    if manual:
        if config.CLOUD_MODE:
            matches = cloud.rows("qa_audit_journal", resource="eq." + resource,
                                 operation="eq.add", id="eq." + manual[1], select="subject")
            return matches[0] if matches else None
        database_table(db)
        return db.execute("select subject from audit_journal where resource=? and operation='add' and id=?",
                          (resource, int(manual[1]))).fetchone()
    revision = REVISION.fullmatch(reference)
    if revision:
        if config.CLOUD_MODE and revision[1] == "L":
            return None
        record = revisions.lookup(revision[1], int(revision[2]), db)
        return {"subject": record["target"]} if record else None
    login = LOGIN.fullmatch(reference)
    activity = ACTIVITY.fullmatch(reference)
    if login:
        if config.CLOUD_MODE:
            matches = cloud.rows("qa_login_events", id="eq." + login[1], select="account_id")
            return {"subject": matches[0]["account_id"]} if matches else None
        return db.execute("select account_id as subject from login_events where id=?", (int(login[1]),)).fetchone()
    if activity:
        if config.CLOUD_MODE:
            matches = cloud.rows("qa_activity_events", category="eq." + activity[1],
                                 id="eq." + activity[2], select="target")
            return {"subject": matches[0]["target"]} if matches else None
        return db.execute("select target as subject from activity_events where category=? and id=?",
                          (activity[1], int(activity[2]))).fetchone()
    return None


def states(resource, references):
    references = sorted({ref for ref in references if any(pattern.fullmatch(ref) for pattern in REFERENCE[resource])})
    if not references:
        return {}
    if config.CLOUD_MODE:
        rows = cloud._request("POST", "/rest/v1/rpc/qa_audit_annotations",
                              json={"p_resource": resource, "p_refs": references}).json()
    else:
        with security_store.database() as db:
            database_table(db)
            placeholders = ",".join("?" for _ in references)
            rows = [dict(row) for row in db.execute(f"""select * from (
                select reference, operation, note, actor_id, created_at, row_number() over (
                  partition by reference, case when operation='correct' then 'correct' else 'visibility' end
                  order by id desc) as position
                from audit_journal where resource=? and reference in ({placeholders})
                ) where position=1""", (resource, *references))]
    result = {}
    for row in rows:
        item = result.setdefault(row["reference"], {})
        if row["operation"] == "correct":
            item["correction"] = {key: row[key] for key in ("note", "actor_id", "created_at")}
        else:
            item["archived"] = row["operation"] == "archive"
    return result


def history(resource, before=None):
    if config.CLOUD_MODE:
        params = {"resource": "eq." + resource, "order": "id.desc", "limit": 51}
        if before is not None:
            params["id"] = f"lt.{before}"
        rows = cloud._request("GET", "/rest/v1/qa_audit_journal", params=params).json()
    else:
        with security_store.database() as db:
            database_table(db)
            rows = [dict(row) for row in db.execute("""select * from audit_journal
                where resource=? and (? is null or id<?) order by id desc limit 51""",
                (resource, before, before))]
    return {"entries": rows[:50], "next": rows[49]["id"] if len(rows) > 50 else None}


def append(request, resource, operation, reference, subject, note):
    actor = request.state.user["id"]
    if config.CLOUD_MODE:
        value = cloud._request("POST", "/rest/v1/rpc/qa_append_audit_operation",
            headers=revisions.actor_header(request), json={"p_resource": resource, "p_operation": operation,
                "p_reference": reference, "p_subject": subject, "p_note": note}).json()
        if not value:
            raise HTTPException(409, "The audit record already has this visibility.")
        return value
    existing = getattr(request.state, "security_db", None)
    with (nullcontext(existing) if existing is not None else revisions.database()) as db:
        if existing is None:
            db.execute("begin immediate")
        database_table(db)
        if operation in {"archive", "restore"}:
            last = db.execute("""select operation from audit_journal where resource=? and reference=?
                and operation in ('archive','restore') order by id desc limit 1""", (resource, reference)).fetchone()
            if (operation == "archive") == bool(last and last["operation"] == "archive"):
                raise HTTPException(409, "The audit record already has this visibility.")
        created = datetime.now(timezone.utc).isoformat()
        row = db.execute("""insert into audit_journal(created_at,resource,operation,reference,actor_id,subject,note)
            values(?,?,?,?,?,?,?)""", (created, resource, operation, reference, actor, subject, note))
        db.execute("""insert into activity_events(created_at,category,actor_id,target,action)
            values(?, 'role-management', ?, ?, ?)""",
            (created, actor, subject, f"Audit {resource}: {operation} (journal #{row.lastrowid})"))
        return row.lastrowid
