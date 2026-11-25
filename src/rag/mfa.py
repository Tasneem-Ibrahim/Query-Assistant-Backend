"""Mandatory TOTP with per-account throttling and replay protection."""
import base64
import hashlib
import hmac
import secrets
import time
from io import BytesIO
from urllib.parse import quote, urlencode

from fastapi import HTTPException
from PIL import Image, ImageOps
from reportlab.graphics.barcode.qrencoder import QRCode, QRErrorCorrectLevel

from . import config, security_store

PENDING = "qa_pending"
LIFETIME = 300
MAX_ATTEMPTS = 5


def signature(value, purpose):
    return hmac.new(config.AUTH_COOKIE_SECRET.encode(), (purpose + ":" + value).encode(), hashlib.sha256).hexdigest()


def secret(owner, nonce):
    return bytes.fromhex(signature(owner + ":" + nonce, "totp"))[:20]


def code_for(key, counter):
    digest = hmac.digest(key, counter.to_bytes(8, "big"), "sha1")
    index = digest[-1] & 15
    number = int.from_bytes(digest[index:index + 4], "big") & 0x7fffffff
    return f"{number % 1000000:06d}"


def setup_qr(account, setup_key):
    issuer = "Query Assistant"
    identity = (account.get("email") or account.get("name") or account["id"]).replace(":", " ")
    label = f"{quote(issuer, safe='')}:{quote(identity, safe='')}"
    query = urlencode({"secret": setup_key, "issuer": issuer}, quote_via=quote)
    qr = QRCode(None, QRErrorCorrectLevel.M)
    qr.addData(f"otpauth://totp/{label}?{query}")
    qr.make()

    size = qr.getModuleCount()
    image = Image.frombytes("L", (size, size),
                            bytes(0 if cell else 255 for row in qr.modules for cell in row))
    image = ImageOps.expand(image, border=4, fill=255)
    image = image.resize((image.width * 5, image.height * 5), Image.Resampling.NEAREST)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def begin(account):
    key = "factor:" + account["id"]
    for _ in range(8):
        state, revision = security_store.read(key)
        if not state.get("nonce"):
            state = {"nonce": secrets.token_hex(24), "enrolled": False}
            if not security_store.write(key, state, revision):
                continue
        if state["enrolled"]:
            return {"mfa_required": "verify"}
        setup_key = base64.b32encode(secret(account["id"], state["nonce"])).decode("ascii")
        return {"mfa_required": "setup", "setup_key": setup_key, "setup_qr_png": setup_qr(account, setup_key)}
    raise HTTPException(409, "Authenticator settings changed. Please sign in again.")


def verify(account, code):
    key = "factor:" + account["id"]
    now = int(time.time())
    for _ in range(8):
        state, revision = security_store.read(key)
        if not state.get("nonce"):
            raise HTTPException(400, "Authenticator setup expired. Please sign in again.")
        previous_window = state.get("attempt_window", 0)
        attempts = state.get("attempts", 0) if now < previous_window + LIFETIME else 0
        if attempts >= MAX_ATTEMPTS:
            raise HTTPException(429, "Too many codes entered. Wait five minutes, then sign in again.")
        authenticator = secret(account["id"], state["nonce"])
        matched = next((step for step in (now // 30, now // 30 - 1, now // 30 + 1)
                        if step > state.get("last_step", -1) and hmac.compare_digest(code, code_for(authenticator, step))), None)
        if matched is None:
            state.update(attempts=attempts + 1, attempt_window=previous_window if attempts else now)
        else:
            state.update(enrolled=True, last_step=matched, attempts=0)
        if security_store.write(key, state, revision):
            return matched is not None
    raise HTTPException(409, "Authenticator settings changed. Try again.")
