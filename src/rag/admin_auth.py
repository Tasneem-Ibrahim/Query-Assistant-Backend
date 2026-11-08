"""Single-admin authentication; credentials are configured only on the backend."""
import base64
import binascii
import secrets

from . import config


def configured():
    return bool(config.ADMIN_USERNAME and config.ADMIN_PASSWORD)


def authorized(header):
    if not configured():
        return False
    try:
        scheme, encoded = header.split(' ', 1)
        if scheme.lower() != 'basic' or len(encoded) > 4096:
            return False
        username, password = base64.b64decode(encoded, validate=True).decode('utf-8').split(':', 1)
    except (ValueError, UnicodeError, binascii.Error):
        return False
    username_ok = secrets.compare_digest(username.encode(), config.ADMIN_USERNAME.encode())
    password_ok = secrets.compare_digest(password.encode(), config.ADMIN_PASSWORD.encode())
    return username_ok and password_ok
