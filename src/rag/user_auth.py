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
from pydantic import AliasChoices, BaseModel, Field, field_validator

from starlette.concurrency import run_in_threadpool

from . import access_control, config, admin_sessions, mfa, security_store

router = APIRouter(prefix="/api/auth", tags=["Authentication"])
ACCESS = "qa_access"
REFRESH = "qa_refresh"
DEADLINE = "qa_session"
PUBLIC_PATHS = {"/api/auth/signup", "/api/auth/login", "/api/auth/admin/login", "/api/auth/refresh", "/api/auth/logout", "/api/auth/accept-invite"}
PUBLIC_PATHS.update({"/api/auth/mfa/verify", "/api/auth/mfa/cancel"})
_client = httpx.AsyncClient(timeout=15)


def configured():
    return bool(config.AUTH_COOKIE_SECRET and (admin_sessions.configured() or admin_sessions.configured(True) or
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
        raise HTTPException(503, "User signup/login requires Supabase configuration. Administrators can sign in with their configured username.")
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
        if path == "/verify" and code == "otp_expired":
            raise HTTPException(400, "This invitation link is invalid or has expired.")
        if code == "email_not_confirmed":
            raise HTTPException(400, "Confirm your email before signing in. Check your inbox.")
        if path == "/signup":
            raise HTTPException(400, "Could not create the account. Check your details, password requirements, or try signing in.")
        if result.status_code in {400, 401, 403, 422}:
            raise HTTPException(401, "Email or password is incorrect, or your session has expired. Please sign in.")
        raise HTTPException(503, "Authentication service is unavailable. Please try again.")
    return result.json() if result.content else {}


def sign_deadline(deadline, refresh, owner=""):
    payload = f"{deadline}.{owner}"
    signature = mfa.signature(payload + ":" + refresh, "verified-session")
    return f"{payload}.{signature}"


def session_deadline(request):
    refresh = request.cookies.get(REFRESH, "")
    try:
        value = request.cookies.get(DEADLINE, "")
        expires, owner, _ = value.split(".")
        deadline = int(expires)
        if not refresh or not owner or deadline <= int(time.time()) or not hmac.compare_digest(value, sign_deadline(deadline, refresh, owner)):
            raise ValueError
        return deadline, owner
    except (ValueError, TypeError):
        raise HTTPException(401, "Your session has expired. Please sign in again.") from None


def claims(token):
    # Used ONLY after Supabase verifies the token, or for tokens returned by Supabase.
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, IndexError, TypeError):
        raise HTTPException(401, "Invalid session. Please sign in again.") from None


def profile(user):
    access = user.get("app_metadata", {}).get("qa_access") or {}
    if access.get("status") in {"inactive", "deleting"}:
        raise HTTPException(403, "This account is deactivated.")
    local_admin = user.get("_local_admin") is True
    super_admin = user.get("_local_super_admin") is True
    role = "super_admin" if super_admin else "admin" if local_admin or access.get("role") == "admin" else "user"
    return {"id": user["id"], "email": user.get("email", ""),
            "name": user.get("user_metadata", {}).get("name") or user.get("email", "User"),
            "role": role, "permissions": access.get("permissions", {}),
            "onboarding_seen": role != "user" or user.get("user_metadata", {}).get("workspace_guide_seen") is True}


async def authenticate(request):
    _, owner = session_deadline(request)
    token = request.cookies.get(ACCESS)
    if not token:
        raise HTTPException(401, "Please sign in to continue.")
    refresh = request.cookies.get(REFRESH, "")
    user = (await run_in_threadpool(admin_sessions.authenticate, token, refresh) if refresh.startswith(("admin_", "super_"))
            else await provider("GET", "/user", token=token))
    if user["id"] != owner:
        raise HTTPException(401, "Invalid session. Please sign in again.")
    data = claims(token)
    now = time.time()
    if min(int(data.get("exp", 0)), int(data.get("iat", 0)) + config.AUTH_ACCESS_SECONDS) <= now:
        raise HTTPException(401, "Access token expired. Refresh your session.")
    request.state.user = await active_profile(user)
    return request.state.user


async def active_profile(user):
    account = profile(user)
    if account["role"] == "admin" and user.get("_local_admin") is not True:
        if not await run_in_threadpool(access_control.managed_admin_active):
            raise HTTPException(403, "The Admin role is inactive. Contact Super Admin.")
    if account["role"] != "super_admin":
        account["permissions"] = await run_in_threadpool(access_control.effective, account)
    if account["id"] == "local-admin":
        status, _ = await run_in_threadpool(access_control.original_admin_status)
        if status.get("active") is False:
            account["access_suspended"] = True
            account["permissions"] = dict.fromkeys(account["permissions"], False)
    return account


def cookie_options():
    if config.AUTH_COOKIE_SAMESITE not in {"lax", "strict", "none"}:
        raise HTTPException(503, "Invalid authentication cookie configuration.")
    if config.AUTH_COOKIE_SAMESITE == "none" and not config.AUTH_COOKIE_SECURE:
        raise HTTPException(503, "Cross-site authentication cookies require HTTPS.")
    return dict(httponly=True, secure=config.AUTH_COOKIE_SECURE, samesite=config.AUTH_COOKIE_SAMESITE)


def clear_cookies(response):
    for name, path in [(ACCESS, "/api"), (REFRESH, "/api"), (DEADLINE, "/api")]:
        response.delete_cookie(name, path=path, **cookie_options())


def clear_pending(response):
    response.delete_cookie(mfa.PENDING, path="/api/auth/mfa", **cookie_options())


async def begin_mfa(response, data, deadline=None):
    account = await active_profile(data["user"])
    result = await run_in_threadpool(mfa.begin, account)
    expires = int(time.time()) + mfa.LIFETIME
    pending = admin_sessions.encode(json.dumps({
        "refresh": data["refresh_token"], "id": account["id"], "role": account["role"],
        "expires": expires, "deadline": deadline or int(time.time()) + config.AUTH_SESSION_SECONDS,
    }).encode())
    clear_cookies(response)
    response.set_cookie(mfa.PENDING, pending + "." + mfa.signature(pending, "pending-mfa"),
                        path="/api/auth/mfa", max_age=mfa.LIFETIME, **cookie_options())
    return result


def pending_login(request):
    try:
        pending, signature = request.cookies.get(mfa.PENDING, "").split(".")
        if not hmac.compare_digest(signature, mfa.signature(pending, "pending-mfa")):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(pending + "=" * (-len(pending) % 4)))
        if min(data["expires"], data["deadline"]) <= int(time.time()):
            raise ValueError
        return data
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "This sign-in attempt expired. Return to sign in.") from None


class VerifyBody(BaseModel):
    code: str = Field(pattern=r"^[0-9]{6}$")


async def rotate_session(refresh_token, deadline):
    if refresh_token.startswith(("admin_", "super_")):
        return await run_in_threadpool(admin_sessions.rotate, refresh_token, deadline)
    return await provider("POST", "/token?grant_type=refresh_token", body={"refresh_token": refresh_token})


@router.post("/mfa/verify")
async def verify_mfa(body: VerifyBody, request: Request, response: Response):
    pending = pending_login(request)
    if not await run_in_threadpool(mfa.verify, pending, body.code):
        await run_in_threadpool(security_store.log, pending, "invalid_code")
        raise HTTPException(400, "Code incorrect or already used. Wait for the next code and try again.")
    data = await rotate_session(pending["refresh"], pending["deadline"])
    account = await active_profile(data["user"])
    if account["id"] != pending["id"]:
        raise HTTPException(401, "Invalid session. Please sign in again.")
    await run_in_threadpool(security_store.log, account, "signed_in")
    clear_pending(response)
    return await save_session(response, data, pending["deadline"], account=account)


@router.post("/mfa/cancel")
async def cancel_mfa(request: Request, response: Response):
    try:
        pending = pending_login(request)
    except HTTPException:
        pending = None
    if pending and pending["refresh"].startswith(("admin_", "super_")):
        await run_in_threadpool(admin_sessions.revoke, pending["refresh"])
    clear_pending(response)
    return {"ok": True}


async def save_session(response, data, deadline=None, *, account=None):
    now = int(time.time())
    deadline = deadline or now + config.AUTH_SESSION_SECONDS
    remaining = deadline - now
    token = data["access_token"]
    refresh = data["refresh_token"]
    account = account or await active_profile(data["user"])
    token_claims = claims(token)
    access_seconds = min(config.AUTH_ACCESS_SECONDS, int(token_claims["exp"]) - now,
                         int(token_claims["iat"]) + config.AUTH_ACCESS_SECONDS - now, remaining)
    if access_seconds <= 0:
        raise HTTPException(401, "Your session has expired. Please sign in again.")
    options = cookie_options()
    response.set_cookie(ACCESS, token, max_age=access_seconds, path="/api", **options)
    response.set_cookie(REFRESH, refresh, max_age=remaining, path="/api", **options)
    response.set_cookie(DEADLINE, sign_deadline(deadline, refresh, account["id"]), max_age=remaining, path="/api", **options)
    return {"user": account, "expires_in": access_seconds, "session_expires_at": deadline}


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


class UnifiedLoginBody(BaseModel):
    # Keep existing email-based clients compatible; UI uses one identity field.
    identity: str = Field(min_length=1, max_length=254, validation_alias=AliasChoices("identity", "email"))
    password: str = Field(min_length=1, max_length=256)


@router.post("/admin/login")
async def admin_login(body: AdminLoginBody, response: Response):
    if not admin_sessions.configured():
        raise HTTPException(503, "Admin sign-in is not configured.")
    try:
        data, deadline = await run_in_threadpool(admin_sessions.create, body.username.strip(), body.password)
    except HTTPException as exc:
        if exc.status_code == 401:
            await run_in_threadpool(security_store.log, {}, "invalid_credentials")
        raise
    return await begin_mfa(response, data, deadline)


@router.post("/signup", status_code=201)
async def signup(body: SignupBody, response: Response):
    data = await provider("POST", "/signup", body={"email": body.email, "password": body.password,
                                                  "data": {"name": body.name}})
    if data.get("access_token"):
        return await begin_mfa(response, data)
    return {"confirmation_required": True, "message": "Check your email to confirm your account, then sign in. If you already have an account, sign in."}


@router.post("/login")
async def login(body: UnifiedLoginBody, response: Response):
    identity = body.identity.strip()
    try:
        if hmac.compare_digest(identity.encode(), config.ADMIN_USERNAME.encode()):
            data, deadline = await run_in_threadpool(admin_sessions.create, identity, body.password)
            return await begin_mfa(response, data, deadline)
        if hmac.compare_digest(identity.encode(), config.SUPER_ADMIN_USERNAME.encode()):
            data, deadline = await run_in_threadpool(admin_sessions.create, identity, body.password, super_admin=True)
            return await begin_mfa(response, data, deadline)
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", identity):
            raise HTTPException(401, "Invalid credentials.")
        data = await provider("POST", "/token?grant_type=password",
                              body={"email": identity, "password": body.password})
        return await begin_mfa(response, data)
    except HTTPException as exc:
        if exc.status_code == 401:
            await run_in_threadpool(security_store.log, {}, "invalid_credentials")
            raise HTTPException(401, "Email or username, or password is incorrect.") from None
        raise


@router.post("/refresh")
async def refresh(request: Request, response: Response):
    deadline, owner = session_deadline(request)
    data = await rotate_session(request.cookies[REFRESH], deadline)
    if data["user"]["id"] != owner:
        raise HTTPException(401, "Invalid session. Please sign in again.")
    return await save_session(response, data, deadline)


def sign_invite(token_hash):
    signature = hmac.new(config.AUTH_COOKIE_SECRET.encode(),
                         ("qa-invite:" + token_hash).encode(), hashlib.sha256).hexdigest()
    return f"{token_hash}.{signature}"


class AcceptInviteBody(BaseModel):
    token_hash: str = Field(min_length=16, max_length=256)
    password: str = Field(min_length=8, max_length=256)


@router.post("/accept-invite")
async def accept_invite(body: AcceptInviteBody, response: Response):
    token_hash = body.token_hash.partition(".")[0]
    if not hmac.compare_digest(sign_invite(token_hash).encode(), body.token_hash.encode()):
        raise HTTPException(400, "This invitation link is invalid.")
    data = await provider("POST", "/verify", body={"token_hash": token_hash, "type": "invite"})
    settings = (data["user"].get("app_metadata") or {}).get("qa_access") or {}
    if settings.get("invited") is not True:
        raise HTTPException(403, "This link is not a Query Assistant invitation.")
    await provider("PUT", "/user", token=data["access_token"], body={"password": body.password})
    return await begin_mfa(response, data)


@router.get("/session")
async def session(request: Request):
    return {"user": request.state.user}


@router.post("/onboarding/complete")
async def complete_onboarding(request: Request):
    # Presentation preference only: never use editable metadata for authorization.
    if request.state.user["role"] != "user":
        return {"ok": True}
    await provider("PUT", "/user", token=request.cookies[ACCESS],
                   body={"data": {"workspace_guide_seen": True}})
    return {"ok": True}


@router.post("/logout")
async def logout(request: Request, response: Response):
    clear_pending(response)
    refresh_token = request.cookies.get(REFRESH, "")
    account = None
    try:
        _, owner = session_deadline(request)
        account = {"id": owner}
    except HTTPException:
        pass
    if refresh_token.startswith(("admin_", "super_")):
        await run_in_threadpool(admin_sessions.revoke, refresh_token)
        if account:
            account["role"] = "super_admin" if refresh_token.startswith("super_") else "admin"
            await run_in_threadpool(security_store.log_logout, account)
        clear_cookies(response)
        return {"ok": True}
    # Refresh if necessary so an expired access token can still revoke the session.
    token = request.cookies.get(ACCESS)
    if request.cookies.get(REFRESH):
        try:
            if account is None:
                raise HTTPException(401, "Session expired.")
            data = await provider("POST", "/token?grant_type=refresh_token", body={"refresh_token": request.cookies[REFRESH]})
            token = data["access_token"]
            if data.get("user", {}).get("id") == account["id"]:
                metadata = data["user"].get("app_metadata") or {}
                account["role"] = "admin" if (metadata.get("qa_access") or {}).get("role") == "admin" else "user"
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
    if token:
        try:
            await provider("POST", "/logout?scope=local", token=token)
            if account:
                await run_in_threadpool(security_store.log_logout, account)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
    clear_cookies(response)
    return {"ok": True}
