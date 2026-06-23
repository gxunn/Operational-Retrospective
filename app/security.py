import secrets

from fastapi import HTTPException, Request
from pwdlib import PasswordHash
from sqlalchemy.orm import Session

from .models import User


password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return password_hash.verify(password, hashed)


def current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, user_id)


def csrf_token(request: Request) -> str:
    if "csrf_token" not in request.session:
        request.session["csrf_token"] = secrets.token_urlsafe(24)
    return request.session["csrf_token"]


def verify_csrf(request: Request, token: str) -> None:
    if not token or not secrets.compare_digest(token, request.session.get("csrf_token", "")):
        raise HTTPException(status_code=403, detail="页面已过期，请刷新后重试")

