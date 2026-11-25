"""Local admin login with signed JWTs and revocable, hashed refresh sessions."""
import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager

from fastapi import HTTPException

from . import cloud, config

ISSUER = "query-assistant-admin"


SUPER_ID = "00000000-0000-4000-8000-000000000001"
SESSION_TABLE = "/rest/v1/qa_admin_sessions"

def credentials(super_admin):
    if super_admin:
        return config.SUPER_ADMIN_USERNAME, config.SUPER_ADMIN_PASSWORD
    return config.ADMIN_USERNAME, config.ADMIN_PASSWORD


def configured(super_admin=False):
    name, password = credentials(super_admin)
    return bool(name and password and config.AUTH_COOKIE_SECRET and
                (not super_admin or name != config.ADMIN_USERNAME))


def user(super_admin=False):
    if super_admin:
        return {"id": SUPER_ID, "email": "", "_local_super_admin": True,
                "user_metadata": {"name": config.SUPER_ADMIN_USERNAME}}
    return {"id": "local-admin", "email": "", "user_metadata": {"name": config.ADMIN_USERNAME}, "_local_admin": True}


def signing_key(super_admin=False):
    name, password = credentials(super_admin)
    scope = "super-admin-session:" if super_admin else "admin-session:"
    return hmac.new(config.AUTH_COOKIE_SECRET.encode(),
                    (scope + name + ":" + password).encode(), hashlib.sha256).digest()


def encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def jwt(sid, super_admin=False):
    now = int(time.time())
    header = encode(b'{"alg":"HS256","typ":"JWT"}')
    payload = encode(json.dumps({"iss": ISSUER, "sub": user(super_admin)["id"], "sid": sid,
                                 "iat": now, "exp": now + config.AUTH_ACCESS_SECONDS}).encode())
    body = header + "." + payload
    return body + "." + encode(hmac.new(signing_key(super_admin), body.encode(), hashlib.sha256).digest())


@contextmanager
def database():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(config.DATA_DIR / "admin_sessions.sqlite3", timeout=10)
    try:
        db.execute("create table if not exists admin_sessions (id text primary key, refresh_hash text not null unique, expires integer not null)")
        with db:
            yield db
    finally:
        db.close()


def digest(refresh):
    # Bind stored refresh tokens to current admin credentials as well.
    return hmac.new(signing_key(refresh.startswith("super_")), refresh.encode(), hashlib.sha256).hexdigest()


def invalid():
    return HTTPException(401, "Admin session expired. Please sign in again.")


def create(username, password, super_admin=False):
    expected_name, expected_password = credentials(super_admin)
    name_ok = secrets.compare_digest(username.encode(), expected_name.encode())
    password_ok = secrets.compare_digest(password.encode(), expected_password.encode())
    if not configured(super_admin) or not (name_ok and password_ok):
        raise HTTPException(401, "Admin username or password is incorrect.")
    sid, refresh = secrets.token_urlsafe(24), ("super_" if super_admin else "admin_") + secrets.token_urlsafe(48)
    deadline = int(time.time()) + config.AUTH_SESSION_SECONDS
    if config.CLOUD_MODE:
        cloud._request("DELETE", SESSION_TABLE, params={"expires": f"lte.{int(time.time())}"})
        cloud._request("POST", SESSION_TABLE, json={"id": sid, "refresh_hash": digest(refresh), "expires": deadline})
    else:
        with database() as db:
            db.execute("delete from admin_sessions where expires <= ?", (int(time.time()),))
            db.execute("insert into admin_sessions values (?, ?, ?)", (sid, digest(refresh), deadline))
    return {"access_token": jwt(sid, super_admin), "refresh_token": refresh, "user": user(super_admin)}, deadline


def authenticate(token, refresh):
    super_admin = refresh.startswith("super_")
    try:
        header, payload, signature = token.split(".")
        expected = encode(hmac.new(signing_key(super_admin), (header + "." + payload).encode(), hashlib.sha256).digest())
        if not configured(super_admin) or not hmac.compare_digest(expected, signature):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if data["iss"] != ISSUER or data["sub"] != user(super_admin)["id"] or data["exp"] <= time.time():
            raise ValueError
        if config.CLOUD_MODE:
            row = cloud._request("GET", SESSION_TABLE, params={
                "select": "id", "id": "eq." + data["sid"], "refresh_hash": "eq." + digest(refresh),
                "expires": f"gt.{int(time.time())}",
            }).json()
        else:
            with database() as db:
                row = db.execute("select id from admin_sessions where id=? and refresh_hash=? and expires>?",
                                 (data["sid"], digest(refresh), int(time.time()))).fetchone()
        if not row:
            raise ValueError
        return user(super_admin)
    except (ValueError, KeyError, TypeError):
        raise invalid() from None


def rotate(refresh, deadline):
    super_admin = refresh.startswith("super_")
    replacement = ("super_" if super_admin else "admin_") + secrets.token_urlsafe(48)
    if not configured(super_admin) or deadline <= int(time.time()):
        raise invalid()
    if config.CLOUD_MODE:
        rows = cloud._request("PATCH", SESSION_TABLE, params={
            "refresh_hash": "eq." + digest(refresh), "expires": f"eq.{deadline}", "select": "id",
        }, headers={"Prefer": "return=representation"}, json={"refresh_hash": digest(replacement)}).json()
        if len(rows) != 1:
            raise invalid()
        return {"access_token": jwt(rows[0]["id"], super_admin), "refresh_token": replacement, "user": user(super_admin)}
    with database() as db:
        row = db.execute("select id, expires from admin_sessions where refresh_hash=? and expires>?",
                         (digest(refresh), int(time.time()))).fetchone()
        if not configured(super_admin) or not row or row[1] != deadline:
            raise invalid()
        result = db.execute("update admin_sessions set refresh_hash=? where id=? and refresh_hash=?",
                            (digest(replacement), row[0], digest(refresh)))
        if result.rowcount != 1:
            raise invalid()
    return {"access_token": jwt(row[0], super_admin), "refresh_token": replacement, "user": user(super_admin)}


def revoke(refresh):
    if config.CLOUD_MODE:
        cloud._request("DELETE", SESSION_TABLE, params={"refresh_hash": "eq." + digest(refresh)})
        return
    with database() as db:
        db.execute("delete from admin_sessions where refresh_hash=?", (digest(refresh),))
