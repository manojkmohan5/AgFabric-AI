import base64
import binascii
import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User

# stdlib scrypt instead of passlib/bcrypt: one less dependency, no version
# friction, and a real KDF with sane cost parameters.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}

oauth2 = OAuth2PasswordBearer(tokenUrl="/login")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, b64salt, b64dk = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(b64salt, validate=True)
        expected = base64.b64decode(b64dk, validate=True)
    except (ValueError, binascii.Error):
        return False
    dk = hashlib.scrypt(
        password.encode(), salt=salt, **{**_SCRYPT, "dklen": len(expected)}
    )
    return hmac.compare_digest(dk, expected)


def make_token(user: User) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user.id),
            "email": user.email,
            "role": user.role,
            "iat": now,
            "exp": now + timedelta(minutes=settings.jwt_ttl_minutes),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def read_token(token: str) -> dict:
    """Raises jwt.InvalidTokenError (incl. ExpiredSignatureError) if bad."""
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])


def current_user(
    token: Annotated[str, Depends(oauth2)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Could not validate credentials",
        {"WWW-Authenticate": "Bearer"},
    )
    try:
        claims = read_token(token)
    except jwt.InvalidTokenError:
        raise unauthorized from None
    user = db.get(User, int(claims["sub"]))
    if user is None:
        raise unauthorized
    # Checked here rather than only at login, so deactivating someone kills the
    # token already sitting in their browser instead of waiting for expiry.
    if not user.is_active:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "this account has been deactivated"
        )
    return user


def require_role(*roles: str):
    """RBAC dependency. Role comes from the DB row, not the token claim, so a
    revoked or downgraded role takes effect before the token expires."""

    def check(user: Annotated[User, Depends(current_user)]) -> User:
        if user.role not in roles:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"Requires one of: {', '.join(roles)}"
            )
        return user

    return check
