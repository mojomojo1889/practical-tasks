import hashlib, secrets, time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from .config import settings
from .database import get_db
from .models import LoginSession, User

ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
DUMMY = ph.hash("not-a-real-user-password")
login_attempts: dict[str, deque] = defaultdict(deque)

def hash_password(value: str) -> str: return ph.hash(value)
def verify_password(stored: str, supplied: str) -> bool:
    try: return ph.verify(stored, supplied)
    except (VerifyMismatchError, InvalidHashError): return False

def digest(token: str) -> str: return hashlib.sha256(token.encode()).hexdigest()

def rate_limit(ip: str):
    now = time.monotonic(); q = login_attempts[ip]
    while q and now - q[0] > 300: q.popleft()
    if len(q) >= 10: raise HTTPException(429, "Too many login attempts. Try again later.")
    q.append(now)

def create_session(db: Session, user: User):
    raw = secrets.token_urlsafe(32); csrf = secrets.token_urlsafe(32)
    db.execute(delete(LoginSession).where(LoginSession.expires_at < datetime.now(timezone.utc)))
    row = LoginSession(token_hash=digest(raw), csrf_token=csrf, user_id=user.id,
                       expires_at=datetime.now(timezone.utc) + timedelta(days=settings.session_days))
    db.add(row); db.commit(); return raw, row

def current_session(request: Request, db: Session = Depends(get_db)) -> LoginSession:
    raw = request.cookies.get("session")
    if not raw: raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in required")
    row = db.scalar(select(LoginSession).where(LoginSession.token_hash == digest(raw)))
    if not row or row.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
    if not row.user.active: raise HTTPException(403, "Account disabled")
    return row

def current_user(s: LoginSession = Depends(current_session)) -> User: return s.user

def teacher(user: User = Depends(current_user)) -> User:
    if user.role != "teacher": raise HTTPException(403, "Teacher access required")
    return user

def csrf(request: Request, s: LoginSession = Depends(current_session)) -> LoginSession:
    given = request.headers.get("X-CSRF-Token", "")
    if not secrets.compare_digest(given, s.csrf_token): raise HTTPException(403, "Invalid CSRF token")
    return s
