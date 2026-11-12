"""Supabase Auth via its REST API. Browser tokens live only in HttpOnly cookies."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from . import config, admin_sessions

router = APIRouter(prefix="/api/auth", tags=["Authentication"])
ACCESS = "qa_access"
REFRESH = "qa_refresh"
DEADLINE = "qa_session"
PUBLIC_PATHS = {"/api/auth/signup", "/api/auth/login", "/api/auth/admin/login", "/api/auth/refresh", "/api/auth/logout"}
_client = httpx.AsyncClient(timeout=15)


def configured():
    return bool(config.AUTH_COOKIE_SECRET and (admin_sessions.configured() or
                (config.SUPABASE_URL and (config.SUPABASE_ANON_KEY or config.SUPABASE_SERVICE_ROLE_KEY))))


def check_origin(request: Request):
    """A custom header prevents HTML form CSRF; CORS restricts who may send it."""
    origin = request.headers.get("origin")
    allowed = {*config.CORS_ORIGINS, str(request.base_url).rstrip("/")}
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if request.headers.get("x-csrf-protection") != "1" or (origin and origin not in allowed):
            raise HTTPException(403, "Request origin is not allowed. Refresh the page and try again.")


async def provider(method, path, *, body=None, token=None):
    if not config.SUPABASE_URL or not (config.SUPABASE_ANON_KEY or config.SUPABASE_SERVICE_ROLE_KEY):
        raise HTTPException(503, "User signup/login requires Supabase configuration. Admin can use Admin sign in.")
    key = config.SUPABASE_ANON_KEY or config.SUPABASE_SERVICE_ROLE_KEY
    headers = {"apikey": key}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        result = await _client.request(method, config.SUPABASE_URL + "/auth/v1" + path,
                                       headers=headers, json=body)
    except httpx.RequestError:
        raise HTTPException(503, "Cannot reach the authentication service. Please try again.") from None
    if not result.is_success:
        data = result.json() if "json" in result.headers.get("content-type", "") else {}
        code = data.get("error_code") or data.get("code")
        if result.status_code == 429:
            raise HTTPException(429, "Too many attempts. Please wait before trying again.")
        if code == "email_not_confirmed":
            raise HTTPException(400, "Confirm your email before signing in. Check your inbox.")
        if path == "/signup":
            raise HTTPException(400, "Could not create the account. Check your details, password requirements, or try signing in.")
        if result.status_code in {400, 401, 403, 422}:
            raise HTTPException(401, "Email or password is incorrect, or your session has expired. Please sign in.")
        raise HTTPException(503, "Authentication service is unavailable. Please try again.")
    return result.json() if result.content else {}


def sign_deadline(deadline, refresh):
    payload = f"{deadline}:{hashlib.sha256(refresh.encode()).hexdigest()}"
    signature = hmac.new(config.AUTH_COOKIE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{deadline}.{signature}"


def session_deadline(request):
    refresh = request.cookies.get(REFRESH, "")
    try:
        value = request.cookies.get(DEADLINE, "")
        deadline = int(value.split(".")[0])
        if not refresh or deadline <= int(time.time()) or not hmac.compare_digest(value, sign_deadline(deadline, refresh)):
            raise ValueError
        return deadline
    except (ValueError, IndexError):
        raise HTTPException(401, "Your session has expired. Please sign in again.") from None


def claims(token):
    # Used ONLY after Supabase verifies the token, or for tokens returned by Supabase.
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, IndexError, TypeError):
        raise HTTPException(401, "Invalid session. Please sign in again.") from None


def profile(user):
    return {"id": user["id"], "email": user.get("email", ""),
            "name": user.get("user_metadata", {}).get("name") or user.get("email", "User"),
            "role": "admin" if user.get("_local_admin") is True else "user"}


async def authenticate(request):
    session_deadline(request)
    token = request.cookies.get(ACCESS)
    if not token:
        raise HTTPException(401, "Please sign in to continue.")
    refresh = request.cookies.get(REFRESH, "")
    user = (admin_sessions.authenticate(token, refresh) if refresh.startswith("admin_")
            else await provider("GET", "/user", token=token))
    data = claims(token)
    now = time.time()
    if min(int(data.get("exp", 0)), int(data.get("iat", 0)) + config.AUTH_ACCESS_SECONDS) <= now:
        raise HTTPException(401, "Access token expired. Refresh your session.")
    request.state.user = profile(user)
    return request.state.user


def cookie_options():
    if config.AUTH_COOKIE_SAMESITE not in {"lax", "strict", "none"}:
        raise HTTPException(503, "Invalid authentication cookie configuration.")
    if config.AUTH_COOKIE_SAMESITE == "none" and not config.AUTH_COOKIE_SECURE:
        raise HTTPException(503, "Cross-site authentication cookies require HTTPS.")
    return dict(httponly=True, secure=config.AUTH_COOKIE_SECURE, samesite=config.AUTH_COOKIE_SAMESITE)


def clear_cookies(response):
    for name, path in [(ACCESS, "/api"), (REFRESH, "/api"), (DEADLINE, "/api")]:
        response.delete_cookie(name, path=path, **cookie_options())


def save_session(response, data, deadline=None):
    now = int(time.time())
    deadline = deadline or now + config.AUTH_SESSION_SECONDS
    remaining = deadline - now
    token = data["access_token"]
    refresh = data["refresh_token"]
    token_claims = claims(token)
    access_seconds = min(config.AUTH_ACCESS_SECONDS, int(token_claims["exp"]) - now,
                         int(token_claims["iat"]) + config.AUTH_ACCESS_SECONDS - now, remaining)
    if access_seconds <= 0:
        raise HTTPException(401, "Your session has expired. Please sign in again.")
    options = cookie_options()
    response.set_cookie(ACCESS, token, max_age=access_seconds, path="/api", **options)
    response.set_cookie(REFRESH, refresh, max_age=remaining, path="/api", **options)
    response.set_cookie(DEADLINE, sign_deadline(deadline, refresh), max_age=remaining, path="/api", **options)
    return {"user": profile(data["user"]), "expires_in": access_seconds, "session_expires_at": deadline}


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def email_valid(cls, value):
        value = value.strip()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid email address")
        return value


class SignupBody(LoginBody):
    name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=256)

    @field_validator("name")
    @classmethod
    def name_valid(cls, value):
        if not value.strip():
            raise ValueError("Enter your name")
        return value.strip()


class AdminLoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


@router.post("/admin/login")
async def admin_login(body: AdminLoginBody, response: Response):
    data, deadline = admin_sessions.create(body.username.strip(), body.password)
    return save_session(response, data, deadline)


@router.post("/signup", status_code=201)
async def signup(body: SignupBody, response: Response):
    data = await provider("POST", "/signup", body={"email": body.email, "password": body.password,
                                                  "data": {"name": body.name}})
    if data.get("access_token"):
        return save_session(response, data)
    return {"confirmation_required": True, "message": "Check your email to confirm your account, then sign in. If you already have an account, sign in."}


@router.post("/login")
async def login(body: LoginBody, response: Response):
    data = await provider("POST", "/token?grant_type=password", body=body.model_dump())
    return save_session(response, data)


@router.post("/refresh")
async def refresh(request: Request, response: Response):
    deadline = session_deadline(request)
    refresh_token = request.cookies[REFRESH]
    data = (admin_sessions.rotate(refresh_token, deadline) if refresh_token.startswith("admin_") else
            await provider("POST", "/token?grant_type=refresh_token", body={"refresh_token": refresh_token}))
    return save_session(response, data, deadline)


@router.get("/session")
async def session(request: Request):
    return {"user": request.state.user}


@router.post("/logout")
async def logout(request: Request, response: Response):
    refresh_token = request.cookies.get(REFRESH, "")
    if refresh_token.startswith("admin_"):
        admin_sessions.revoke(refresh_token)
        clear_cookies(response)
        return {"ok": True}
    # Refresh if necessary so an expired access token can still revoke the session.
    token = request.cookies.get(ACCESS)
    if request.cookies.get(REFRESH):
        try:
            session_deadline(request)
            data = await provider("POST", "/token?grant_type=refresh_token", body={"refresh_token": request.cookies[REFRESH]})
            token = data["access_token"]
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
    if token:
        try:
            await provider("POST", "/logout?scope=local", token=token)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
    clear_cookies(response)
    return {"ok": True}
