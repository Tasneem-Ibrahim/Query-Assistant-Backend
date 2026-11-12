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

from . import config

ISSUER = "query-assistant-admin"


def configured():
    return bool(config.ADMIN_USERNAME and config.ADMIN_PASSWORD and config.AUTH_COOKIE_SECRET)


def user():
    return {"id": "local-admin", "email": "", "user_metadata": {"name": config.ADMIN_USERNAME}, "_local_admin": True}


def signing_key():
    return hmac.new(config.AUTH_COOKIE_SECRET.encode(),
                    ("admin-session:" + config.ADMIN_USERNAME + ":" + config.ADMIN_PASSWORD).encode(), hashlib.sha256).digest()


def encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def jwt(sid):
    now = int(time.time())
    header = encode(b'{"alg":"HS256","typ":"JWT"}')
    payload = encode(json.dumps({"iss": ISSUER, "sub": "local-admin", "sid": sid,
                                 "iat": now, "exp": now + config.AUTH_ACCESS_SECONDS}).encode())
    body = header + "." + payload
    return body + "." + encode(hmac.new(signing_key(), body.encode(), hashlib.sha256).digest())


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
    return hmac.new(signing_key(), refresh.encode(), hashlib.sha256).hexdigest()


def invalid():
    return HTTPException(401, "Admin session expired. Please sign in again.")


def create(username, password):
    name_ok = secrets.compare_digest(username.encode(), config.ADMIN_USERNAME.encode())
    password_ok = secrets.compare_digest(password.encode(), config.ADMIN_PASSWORD.encode())
    if not configured() or not (name_ok and password_ok):
        raise HTTPException(401, "Admin username or password is incorrect.")
    sid, refresh = secrets.token_urlsafe(24), "admin_" + secrets.token_urlsafe(48)
    deadline = int(time.time()) + config.AUTH_SESSION_SECONDS
    with database() as db:
        db.execute("delete from admin_sessions where expires <= ?", (int(time.time()),))
        db.execute("insert into admin_sessions values (?, ?, ?)", (sid, digest(refresh), deadline))
    return {"access_token": jwt(sid), "refresh_token": refresh, "user": user()}, deadline


def authenticate(token, refresh):
    try:
        header, payload, signature = token.split(".")
        expected = encode(hmac.new(signing_key(), (header + "." + payload).encode(), hashlib.sha256).digest())
        if not configured() or not hmac.compare_digest(expected, signature):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if data["iss"] != ISSUER or data["sub"] != "local-admin" or data["exp"] <= time.time():
            raise ValueError
        with database() as db:
            row = db.execute("select id from admin_sessions where id=? and refresh_hash=? and expires>?",
                             (data["sid"], digest(refresh), int(time.time()))).fetchone()
        if not row:
            raise ValueError
        return user()
    except (ValueError, KeyError, TypeError):
        raise invalid() from None


def rotate(refresh, deadline):
    replacement = "admin_" + secrets.token_urlsafe(48)
    with database() as db:
        row = db.execute("select id, expires from admin_sessions where refresh_hash=? and expires>?",
                         (digest(refresh), int(time.time()))).fetchone()
        if not configured() or not row or row[1] != deadline:
            raise invalid()
        result = db.execute("update admin_sessions set refresh_hash=? where id=? and refresh_hash=?",
                            (digest(replacement), row[0], digest(refresh)))
        if result.rowcount != 1:
            raise invalid()
    return {"access_token": jwt(row[0]), "refresh_token": replacement, "user": user()}


def revoke(refresh):
    with database() as db:
        db.execute("delete from admin_sessions where refresh_hash=?", (digest(refresh),))
