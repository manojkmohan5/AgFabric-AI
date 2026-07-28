"""User provisioning.

This exists because the app previously had no way to create an account except
`python -m app.seed`, which drops every table first. That made a non-seeded
deployment unreachable — no users, so nobody can log in.

Design decisions worth knowing:

* **Exec-only.** Creating accounts is the most privileged action in the app, so
  it sits behind the narrowest role rather than "any authenticated user".
* **Deactivate, never delete.** An ex-employee's audit rows must remain
  attributable, and a foreign key from `audit_logs.user_id` means a delete would
  either fail or orphan history. `current_user` rejects inactive accounts, so a
  token already in someone's browser stops working the moment they are
  deactivated — no waiting for expiry.
* **A user cannot deactivate themselves.** That is how you lock every admin out
  of a running system.
* **The password is never echoed back.** Not in the create response, not in a
  log line. The caller supplied it; they already have it.
"""

import logging
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import current_user, hash_password, require_role
from .config import settings
from .db import get_db
from .models import ROLES, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[User, Depends(require_role("exec"))]


class CreateUser(BaseModel):
    # A plain str with a structural check rather than pydantic's EmailStr, which
    # requires `email-validator` (and dnspython, and DNS deliverability lookups by
    # default). Full RFC email validation by regex is a known trap; the only
    # question that matters here is "is this plausibly an address", and the real
    # test is whether the person can log in.
    email: str = Field(min_length=3, max_length=255)
    full_name: str = Field(min_length=1, max_length=255)
    role: str
    # Bounded at both ends: too short is weak, and scrypt cost scales with input
    # so an unbounded password is a cheap way to burn CPU.
    password: str = Field(min_length=settings.min_password_length, max_length=200)

    @field_validator("email")
    @classmethod
    def plausible_email(cls, v: str) -> str:
        candidate = v.strip().lower()
        local, _, domain = candidate.partition("@")
        if not local or not domain or "." not in domain or " " in candidate:
            raise ValueError("must be an email address like name@example.com")
        return candidate


@router.get("/users")
def list_users(db: DbDep, admin: AdminDep) -> dict:
    rows = db.scalars(select(User).order_by(User.created_at.asc())).all()
    return {
        "count": len(rows),
        "users": [
            {
                "id": u.id,
                "email": u.email,
                "full_name": u.full_name,
                "role": u.role,
                "is_active": u.is_active,
                "created_at": u.created_at.isoformat(),
            }
            for u in rows
        ],
    }


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(body: CreateUser, db: DbDep, admin: AdminDep) -> dict:
    if body.role not in ROLES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"role must be one of: {', '.join(ROLES)}",
        )
    email = body.email.strip().lower()
    if db.scalar(select(User).where(func.lower(User.email) == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "that email already exists")

    user = User(
        email=email,
        full_name=body.full_name.strip(),
        role=body.role,
        password_hash=hash_password(body.password),
        is_active=True,
        created_by=admin.id,
    )
    db.add(user)
    db.commit()
    # Email and role only — never the password, and never the hash.
    logger.info("user %s created by %s", email, admin.email)
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": True,
    }


@router.post("/users/{user_id}/deactivate")
def deactivate_user(user_id: int, db: DbDep, admin: AdminDep) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
    if user.id == admin.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "you cannot deactivate your own account",
        )
    # Refuse to remove the last route in. Counting active execs rather than
    # active users: a system with only warehouse accounts left has nobody who
    # can provision, which is the same lockout by a slower path.
    remaining = db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.role == "exec", User.is_active.is_(True), User.id != user.id)
    )
    if user.role == "exec" and not remaining:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "that is the last active exec account; promote another first",
        )

    user.is_active = False
    db.commit()
    logger.info("user %s deactivated by %s", user.email, admin.email)
    return {"id": user.id, "email": user.email, "is_active": False}


@router.post("/users/{user_id}/reactivate")
def reactivate_user(user_id: int, db: DbDep, admin: AdminDep) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
    user.is_active = True
    db.commit()
    return {"id": user.id, "email": user.email, "is_active": True}


class ChangePassword(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=settings.min_password_length, max_length=200)


@router.post("/me/password")
def change_own_password(
    body: ChangePassword,
    db: DbDep,
    user: Annotated[User, Depends(current_user)],
) -> dict:
    """Any authenticated user can rotate their own password.

    Requires the current one, so a stolen token alone cannot lock the real owner
    out of their account.
    """
    from .auth import verify_password

    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "current password is wrong")
    if body.new_password == body.current_password:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "new password must be different"
        )
    user.password_hash = hash_password(body.new_password)
    db.commit()
    logger.info("password changed for %s", user.email)
    # ponytail: existing tokens stay valid until they expire — there is no
    # deny-list yet. Add one (Redis) when a compromised token must die on
    # password change rather than within the hour.
    return {"status": "ok", "note": "existing sessions remain valid until expiry"}


def bootstrap_first_admin(db: Session) -> str | None:
    """Create the first exec account if the users table is empty.

    The whole point is that a freshly migrated database with no seed data is
    still reachable. Deliberately a no-op once any user exists, so it can never
    be used to add an account or reset a password on a live system.
    """
    if not (settings.bootstrap_admin_email and settings.bootstrap_admin_password):
        return None
    if db.scalar(select(func.count()).select_from(User)):
        return None
    if len(settings.bootstrap_admin_password) < settings.min_password_length:
        logger.error(
            "BOOTSTRAP_ADMIN_PASSWORD is shorter than %s characters; skipping",
            settings.min_password_length,
        )
        return None

    email = settings.bootstrap_admin_email.strip().lower()
    db.add(
        User(
            email=email,
            full_name="Bootstrap Admin",
            role="exec",
            password_hash=hash_password(settings.bootstrap_admin_password),
            is_active=True,
        )
    )
    db.commit()
    logger.warning(
        "bootstrapped first admin %s — change this password and clear "
        "BOOTSTRAP_ADMIN_PASSWORD from the environment",
        email,
    )
    return email


def suggest_password() -> str:
    """A password an admin can hand to a new user. Not stored anywhere."""
    return secrets.token_urlsafe(16)
