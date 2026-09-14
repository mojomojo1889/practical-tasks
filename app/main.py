from __future__ import annotations
import hashlib
import io
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from sqlalchemy import delete, func, inspect, select, text, update
from sqlalchemy.orm import Session, selectinload

from .auth import DUMMY, admin, create_session, csrf, current_session, current_user, hash_password, rate_limit, teacher, verify_password
from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .models import AcademicGroup, Attachment, AuditLog, Enrollment, GroupMembership, GroupTeacher, LoginSession, MigrationVersion, RoleRequest, Task, User
from .schemas import AcademicGroupIn, AssignIn, GroupJoinIn, GroupStudentIn, GroupTeacherIn, LoginIn, MeOut, NoteIn, PasswordChangeIn, PasswordResetIn, RegisterIn, StatusIn, TaskIn, TeacherRequestDecisionIn, UserOut, UserUpdateIn

app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None)
STATIC = Path(__file__).parent / "static"
settings.upload_dir.mkdir(parents=True, exist_ok=True)
Image.MAX_IMAGE_PIXELS = 25_000_000

STANDARD_CATEGORIES = {"cable", "classroom", "server", "network", "hardware", "other"}
LEGACY_GROUP_NAME = "Imported legacy data"


def ensure_legacy_group(db: Session) -> AcademicGroup:
    group = db.scalar(select(AcademicGroup).where(AcademicGroup.name == LEGACY_GROUP_NAME))
    if group is None:
        group = AcademicGroup(name=LEGACY_GROUP_NAME, description="Migrated data", is_active=True)
        db.add(group)
        db.flush()
    return group


def normalize_category(data: TaskIn | dict[str, Any]) -> tuple[str, str | None]:
    if isinstance(data, TaskIn):
        category_key = (data.category_key or data.category or "other").strip().lower()
        custom_category = (data.custom_category or "").strip() if data.custom_category is not None else None
    else:
        category_key = str((data.get("category_key") or data.get("category") or "other")).strip().lower()
        custom_category = str(data.get("custom_category") or "").strip() if data.get("custom_category") is not None else None
    if category_key not in STANDARD_CATEGORIES:
        raise HTTPException(422, "Invalid category")
    if category_key == "other":
        if not custom_category or len(custom_category) < 2 or len(custom_category) > 80:
            raise HTTPException(422, "Custom category must be 2-80 characters")
        return "other", custom_category
    return category_key, None


def log_audit(db: Session, actor_id: int | None, action: str, target_type: str, target_id: int | None, details: Any):
    payload = details if isinstance(details, str) else json.dumps(details, ensure_ascii=False, default=str)
    db.add(AuditLog(actor_id=actor_id, action=action, target_type=target_type, target_id=target_id, details=payload or ""))


def get_user_group_ids(db: Session, user: User) -> set[int]:
    if user.role == "admin":
        return set(db.scalars(select(AcademicGroup.id)).all())
    if user.role == "teacher":
        if not user.teacher_approved:
            return set()
        return set(db.scalars(select(GroupTeacher.group_id).where(GroupTeacher.teacher_id == user.id)).all())
    if user.role == "student":
        ids = set(db.scalars(select(GroupMembership.group_id).where(GroupMembership.user_id == user.id, GroupMembership.status == "active")).all())
        if ids:
            return ids
        legacy_ids = set(db.scalars(select(AcademicGroup.id).where(AcademicGroup.name == LEGACY_GROUP_NAME, AcademicGroup.is_active.is_(True))).all())
        if legacy_ids:
            return legacy_ids
        active_ids = set(db.scalars(select(AcademicGroup.id).where(AcademicGroup.is_active.is_(True))).all())
        if len(active_ids) == 1:
            return active_ids
        return set()
    return set()


def get_group_scope(db: Session, user: User):
    if user.role == "admin":
        return {"all": True, "groups": set()}
    return {"all": False, "groups": get_user_group_ids(db, user)}


def task_dict(task: Task):
    return {
        "id": task.id,
        "title": task.title,
        "category": task.category,
        "category_key": task.category_key,
        "custom_category": task.custom_category,
        "description": task.description,
        "location": task.location,
        "room": task.room,
        "scheduled_at": task.scheduled_at,
        "due_at": task.due_at,
        "capacity": task.capacity,
        "is_open": task.is_open,
        "is_archived": task.is_archived,
        "academic_group_id": task.academic_group_id,
        "created_at": task.created_at,
        "enrolled_count": len(task.enrollments),
    }


def enrollment_dict(e: Enrollment, include_student: bool = False):
    d = {
        "id": e.id,
        "status": e.status,
        "note": e.note,
        "created_at": e.created_at,
        "started_at": e.started_at,
        "ready_at": e.ready_at,
        "completed_at": e.completed_at,
        "task": task_dict(e.task),
        "photos": [{"id": p.id, "name": p.original_name, "mime_type": p.mime_type, "size": p.size, "url": f"/api/photos/{p.id}"} for p in e.photos],
    }
    if include_student:
        d["student"] = UserOut.model_validate(e.student).model_dump()
    return d


def column_exists(connection, table_name: str, column_name: str) -> bool:
    cols = connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    return any(row[1] == column_name for row in cols)


def migrate_database():
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        tables = set(inspect(engine).get_table_names())
        if "users" in tables:
            if not column_exists(conn, "users", "teacher_approved"):
                conn.execute(text("ALTER TABLE users ADD COLUMN teacher_approved BOOLEAN NOT NULL DEFAULT 0"))
            if not column_exists(conn, "users", "must_change_password"):
                conn.execute(text("ALTER TABLE users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT 0"))
        if "tasks" in tables:
            if not column_exists(conn, "tasks", "academic_group_id"):
                conn.execute(text("ALTER TABLE tasks ADD COLUMN academic_group_id INTEGER"))
            if not column_exists(conn, "tasks", "category_key"):
                conn.execute(text("ALTER TABLE tasks ADD COLUMN category_key TEXT NOT NULL DEFAULT 'other'"))
            if not column_exists(conn, "tasks", "custom_category"):
                conn.execute(text("ALTER TABLE tasks ADD COLUMN custom_category TEXT"))
        with SessionLocal() as db:
            existing_version = db.scalar(select(MigrationVersion).where(MigrationVersion.version == "groups-admin-migration"))
            if existing_version is None:
                db.add(MigrationVersion(version="groups-admin-migration"))
            admin_email = settings.admin_email.lower().strip()
            admin_user = db.scalar(select(User).where(User.email == admin_email))
            if admin_user is None:
                admin_user = User(name=settings.admin_name, email=admin_email, password_hash=hash_password(settings.admin_password), role="admin", teacher_approved=True, active=True)
                db.add(admin_user)
                db.flush()
            else:
                admin_user.role = "admin"
                admin_user.teacher_approved = True
                admin_user.active = True
            if db.scalar(select(User).where(User.email == "teacher@test.fi")) is None and db.scalar(select(func.count()).select_from(User)) == 1:
                demo_teacher = User(name="Demo Teacher", email="teacher@test.fi", password_hash=hash_password("LongTeacherPass123"), role="teacher", teacher_approved=True, active=True)
                db.add(demo_teacher)
                db.flush()
            legacy_group = db.scalar(select(AcademicGroup).where(AcademicGroup.name == LEGACY_GROUP_NAME))
            needs_import_group = (
                db.scalar(select(Task).where(Task.academic_group_id.is_(None))) is not None
                or db.scalar(select(User).where(User.role == "student")) is not None
                or db.scalar(select(User).where(User.role.in_(["teacher", "admin"]))) is not None
            )
            if needs_import_group and legacy_group is None:
                legacy_group = AcademicGroup(name=LEGACY_GROUP_NAME, description="Imported legacy data", is_active=True)
                db.add(legacy_group)
                db.flush()
            if legacy_group is not None:
                for student in db.scalars(select(User).where(User.role == "student")).all():
                    has_membership = db.scalar(select(GroupMembership).where(GroupMembership.group_id == legacy_group.id, GroupMembership.user_id == student.id))
                    if has_membership is None and not db.scalar(select(GroupMembership).where(GroupMembership.user_id == student.id)):
                        db.add(GroupMembership(group_id=legacy_group.id, user_id=student.id, status="active"))
                for teacher_user in db.scalars(select(User).where(User.role.in_(["teacher", "admin"]))).all():
                    if not db.scalar(select(GroupTeacher).where(GroupTeacher.group_id == legacy_group.id, GroupTeacher.teacher_id == teacher_user.id)):
                        db.add(GroupTeacher(group_id=legacy_group.id, teacher_id=teacher_user.id, permission="owner"))
                db.execute(update(Task).where(Task.academic_group_id.is_(None)).values(academic_group_id=legacy_group.id))
            for task in db.scalars(select(Task)).all():
                if not task.category_key or task.category_key == "":
                    task.category_key = (task.category or "other").strip().lower() or "other"
                if task.category_key not in STANDARD_CATEGORIES:
                    task.category_key = "other"
                if task.category_key == "other":
                    task.category = "other"
                else:
                    task.category = task.category_key
                    task.custom_category = None
            db.commit()


@app.on_event("startup")
def startup():
    migrate_database()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/auth/register", response_model=UserOut, status_code=201)
def register(data: RegisterIn, db: Session = Depends(get_db)):
    requested_role = (data.requested_role or data.role or "student").strip().lower()
    if requested_role not in {"student", "teacher"}:
        raise HTTPException(422, "Role must be 'student' or 'teacher'")
    email = data.email.lower().strip()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "Email already registered")
    user = User(
        name=data.name.strip(),
        email=email,
        password_hash=hash_password(data.password),
        role=requested_role,
        group_name=(data.group_name or "").strip() or None,
        active=True,
        teacher_approved=(requested_role == "student"),
        must_change_password=False,
    )
    db.add(user)
    db.flush()
    if requested_role == "teacher":
        rr = RoleRequest(user_id=user.id, requested_role="teacher", status="pending", note="Pending teacher approval")
        db.add(rr)
    log_audit(db, None, "register", "user", user.id, {"role": requested_role, "email": email})
    db.commit()
    db.refresh(user)
    return user


@app.post("/api/auth/login", response_model=MeOut)
def login(data: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "unknown"
    rate_limit(ip)
    user = db.scalar(select(User).where(User.email == data.email.lower().strip()))
    ok = verify_password(user.password_hash if user else DUMMY, data.password)
    if not user or not ok or not user.active:
        raise HTTPException(401, "Incorrect email or password")
    raw, row = create_session(db, user)
    response.set_cookie("session", raw, max_age=settings.session_days * 86400, httponly=True, secure=settings.cookie_secure, samesite="lax", path="/")
    return MeOut(**UserOut.model_validate(user).model_dump(), csrf_token=row.csrf_token)


@app.post("/api/auth/logout", status_code=204)
def logout(response: Response, s: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    db.delete(s)
    db.commit()
    response.delete_cookie("session", path="/")


@app.get("/api/me", response_model=MeOut)
def me(s: LoginSession = Depends(current_session)):
    return MeOut(**UserOut.model_validate(s.user).model_dump(), csrf_token=s.csrf_token)


@app.get("/api/tasks")
def tasks(user: User = Depends(current_user), db: Session = Depends(get_db)):
    query = select(Task).options(selectinload(Task.enrollments)).where(Task.is_archived.is_(False)).order_by(Task.scheduled_at.is_(None), Task.scheduled_at, Task.created_at.desc())
    if user.role == "student":
        allowed_groups = get_user_group_ids(db, user)
        if not allowed_groups:
            return []
        query = query.where(Task.academic_group_id.in_(list(allowed_groups)))
    elif user.role == "teacher":
        allowed_groups = get_user_group_ids(db, user)
        if not allowed_groups:
            return []
        query = query.where(Task.academic_group_id.in_(list(allowed_groups)))
    elif user.role != "admin":
        raise HTTPException(403, "Access denied")
    rows = db.scalars(query).all()
    my = {e.task_id: e for e in db.scalars(select(Enrollment).where(Enrollment.student_id == user.id)).all()} if user.role == "student" else {}
    out = []
    for t in rows:
        d = task_dict(t)
        if user.role == "student":
            d["my_enrollment"] = ({"id": my[t.id].id, "status": my[t.id].status} if t.id in my else None)
        out.append(d)
    return out


@app.post("/api/tasks", status_code=201)
def create_task(data: TaskIn, user: User = Depends(teacher), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    category_key, custom_category = normalize_category(data)
    if user.role == "teacher" and user.teacher_approved is False:
        raise HTTPException(403, "Teacher not approved")
    allowed_groups = get_user_group_ids(db, user)
    if user.role == "teacher" and not allowed_groups:
        raise HTTPException(403, "No assigned group")
    if data.academic_group_id is not None:
        if user.role != "admin":
            if data.academic_group_id not in allowed_groups:
                raise HTTPException(403, "Group access denied")
        group_id = data.academic_group_id
    else:
        if user.role == "admin":
            group_id = db.scalar(select(AcademicGroup.id).limit(1))
        elif len(allowed_groups) == 1:
            group_id = next(iter(allowed_groups))
        else:
            group_id = db.scalar(select(AcademicGroup.id).where(AcademicGroup.name == LEGACY_GROUP_NAME))
    task = Task(
        title=data.title.strip(),
        category="other" if category_key == "other" else category_key,
        category_key=category_key,
        custom_category=custom_category,
        description=data.description,
        location=data.location,
        room=data.room,
        scheduled_at=data.scheduled_at,
        due_at=data.due_at,
        capacity=data.capacity,
        is_open=data.is_open,
        teacher_id=user.id,
        academic_group_id=group_id,
    )
    db.add(task)
    db.flush()
    log_audit(db, user.id, "create_task", "task", task.id, {"group_id": group_id})
    db.commit()
    db.refresh(task)
    task.enrollments = []
    return task_dict(task)


@app.put("/api/tasks/{task_id}")
def update_task(task_id: int, data: TaskIn, user: User = Depends(teacher), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if user.role != "admin":
        if task.academic_group_id not in get_user_group_ids(db, user):
            raise HTTPException(403, "Group access denied")
    target_group_id = data.academic_group_id if data.academic_group_id is not None else task.academic_group_id
    if target_group_id is not None:
        target_group = db.get(AcademicGroup, target_group_id)
        if not target_group or not target_group.is_active:
            raise HTTPException(422, "Group not found or inactive")
        if user.role != "admin" and target_group_id not in get_user_group_ids(db, user):
            raise HTTPException(403, "Group access denied")
    category_key, custom_category = normalize_category(data)
    for key, value in {
        "title": data.title.strip(),
        "category": "other" if category_key == "other" else category_key,
        "category_key": category_key,
        "custom_category": custom_category,
        "description": data.description,
        "location": data.location,
        "room": data.room,
        "scheduled_at": data.scheduled_at,
        "due_at": data.due_at,
        "capacity": data.capacity,
        "is_open": data.is_open,
        "academic_group_id": target_group_id,
    }.items():
        setattr(task, key, value)
    db.commit()
    db.refresh(task)
    db.refresh(task, ["enrollments"])
    return task_dict(task)


@app.post("/api/tasks/{task_id}/archive")
def archive_task(task_id: int, user: User = Depends(teacher), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if user.role != "admin" and task.academic_group_id not in get_user_group_ids(db, user):
        raise HTTPException(403, "Group access denied")
    task.is_archived = True
    task.is_open = False
    db.commit()
    return {"ok": True}


@app.post("/api/tasks/{task_id}/enroll", status_code=201)
def enroll(task_id: int, user: User = Depends(current_user), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    if user.role != "student":
        raise HTTPException(403, "Student access required")
    task = db.scalar(select(Task).options(selectinload(Task.enrollments)).where(Task.id == task_id))
    if not task or task.is_archived:
        raise HTTPException(404, "Task not found")
    if task.academic_group_id not in get_user_group_ids(db, user):
        raise HTTPException(403, "Task is not available in your groups")
    if not task.is_open:
        raise HTTPException(409, "Enrollment is closed")
    if db.scalar(select(Enrollment).where(Enrollment.task_id == task_id, Enrollment.student_id == user.id)):
        raise HTTPException(409, "Already enrolled")
    if task.capacity and len(task.enrollments) >= task.capacity:
        raise HTTPException(409, "Task is full")
    e = Enrollment(task_id=task_id, student_id=user.id)
    db.add(e)
    db.commit()
    return {"id": e.id, "status": e.status}


@app.post("/api/tasks/{task_id}/assign", status_code=201)
def assign(task_id: int, data: AssignIn, user: User = Depends(teacher), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task or task.is_archived:
        raise HTTPException(404, "Task not found")
    student = db.get(User, data.student_id)
    if not student or student.role != "student":
        raise HTTPException(404, "Student not found")
    if user.role != "admin" and task.academic_group_id not in get_user_group_ids(db, user):
        raise HTTPException(403, "Group access denied")
    if db.scalar(select(Enrollment).where(Enrollment.task_id == task_id, Enrollment.student_id == student.id)):
        raise HTTPException(409, "Already assigned")
    e = Enrollment(task_id=task_id, student_id=student.id)
    db.add(e)
    db.commit()
    return {"id": e.id, "status": e.status}


@app.get("/api/my-work")
def my_work(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role != "student":
        raise HTTPException(403, "Student access required")
    rows = db.scalars(
        select(Enrollment)
        .options(selectinload(Enrollment.task).selectinload(Task.enrollments), selectinload(Enrollment.photos))
        .where(Enrollment.student_id == user.id)
        .order_by(Enrollment.completed_at.desc(), Enrollment.created_at.desc())
    ).all()
    return [enrollment_dict(e) for e in rows]


@app.post("/api/enrollments/{eid}/start")
def start_work(eid: int, user: User = Depends(current_user), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    e = db.get(Enrollment, eid)
    if not e or e.student_id != user.id:
        raise HTTPException(404, "Work not found")
    if e.status != "enrolled":
        raise HTTPException(409, "Work cannot be started")
    e.status = "in_progress"
    e.started_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": e.status}


@app.patch("/api/enrollments/{eid}/note")
def save_note(eid: int, data: NoteIn, user: User = Depends(current_user), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    e = db.get(Enrollment, eid)
    if not e or e.student_id != user.id:
        raise HTTPException(404, "Work not found")
    if e.status == "completed":
        raise HTTPException(409, "Completed work is locked")
    e.note = data.note
    db.commit()
    return {"ok": True}


@app.post("/api/enrollments/{eid}/submit")
def submit_work(eid: int, user: User = Depends(current_user), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    e = db.get(Enrollment, eid)
    if not e or e.student_id != user.id:
        raise HTTPException(404, "Work not found")
    if e.status not in ("enrolled", "in_progress"):
        raise HTTPException(409, "Work cannot be submitted")
    e.status = "ready"
    e.ready_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": e.status}


@app.post("/api/enrollments/{eid}/photos", status_code=201)
async def upload_photos(eid: int, files: list[UploadFile] = File(...), user: User = Depends(current_user), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    e = db.scalar(select(Enrollment).options(selectinload(Enrollment.photos), selectinload(Enrollment.task)).where(Enrollment.id == eid))
    if not e or (user.role not in {"teacher", "admin"} and e.student_id != user.id):
        raise HTTPException(404, "Work not found")
    if user.role == "teacher" and user.teacher_approved and e.task.academic_group_id not in get_user_group_ids(db, user):
        raise HTTPException(403, "Group access denied")
    if e.status == "completed":
        raise HTTPException(409, "Completed work is locked")
    if not files or len(e.photos) + len(files) > settings.max_photos:
        raise HTTPException(400, f"Maximum {settings.max_photos} photos")
    created = []
    limit = settings.max_photo_mb * 1024 * 1024
    for upload in files:
        raw = await upload.read(limit + 1)
        if len(raw) > limit:
            raise HTTPException(413, f"Each photo must be at most {settings.max_photo_mb} MB")
        try:
            img = Image.open(io.BytesIO(raw))
            img.verify()
            img = Image.open(io.BytesIO(raw))
            fmt = (img.format or "").upper()
            if fmt not in {"JPEG", "PNG", "WEBP"}:
                raise HTTPException(415, "Only JPEG, PNG and WebP are allowed")
            ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}[fmt]
            mime = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}[fmt]
            if fmt == "JPEG" and img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            name = f"{secrets.token_hex(20)}.{ext}"
            path = settings.upload_dir / name
            save_args = {"optimize": True}
            if fmt == "JPEG":
                save_args["quality"] = 88
            img.save(path, format=fmt, **save_args)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise HTTPException(415, "Invalid or unsafe image")
        att = Attachment(enrollment_id=e.id, stored_name=name, original_name=Path(upload.filename or name).name[:255], mime_type=mime, size=path.stat().st_size)
        db.add(att)
        db.flush()
        created.append({"id": att.id, "name": att.original_name, "url": f"/api/photos/{att.id}"})
    db.commit()
    return created


@app.delete("/api/photos/{photo_id}", status_code=204)
def delete_photo(photo_id: int, user: User = Depends(current_user), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    p = db.scalar(select(Attachment).options(selectinload(Attachment.enrollment).selectinload(Enrollment.task)).where(Attachment.id == photo_id))
    if not p or (user.role not in {"teacher", "admin"} and p.enrollment.student_id != user.id):
        raise HTTPException(404, "Photo not found")
    if user.role == "teacher" and user.teacher_approved and p.enrollment.task.academic_group_id not in get_user_group_ids(db, user):
        raise HTTPException(403, "Group access denied")
    if p.enrollment.status == "completed":
        raise HTTPException(409, "Completed work is locked")
    path = settings.upload_dir / p.stored_name
    db.delete(p)
    db.commit()
    path.unlink(missing_ok=True)


@app.get("/api/photos/{photo_id}")
def photo(photo_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = db.scalar(select(Attachment).options(selectinload(Attachment.enrollment).selectinload(Enrollment.task)).where(Attachment.id == photo_id))
    if not p or (user.role not in {"teacher", "admin"} and p.enrollment.student_id != user.id):
        raise HTTPException(404, "Photo not found")
    if user.role == "teacher" and user.teacher_approved and p.enrollment.task.academic_group_id not in get_user_group_ids(db, user):
        raise HTTPException(403, "Group access denied")
    path = settings.upload_dir / p.stored_name
    if not path.is_file():
        raise HTTPException(404, "Photo file missing")
    return FileResponse(path, media_type=p.mime_type, filename=p.original_name, content_disposition_type="inline")


@app.get("/api/admin/enrollments")
def all_work(user: User = Depends(teacher), db: Session = Depends(get_db)):
    query = select(Enrollment).options(selectinload(Enrollment.task).selectinload(Task.enrollments), selectinload(Enrollment.student), selectinload(Enrollment.photos)).order_by(Enrollment.completed_at.desc(), Enrollment.created_at.desc())
    if user.role != "admin":
        allowed = get_user_group_ids(db, user)
        if not allowed:
            return []
        query = query.where(Enrollment.task_id.in_(select(Task.id).where(Task.academic_group_id.in_(list(allowed)))))
    rows = db.scalars(query).all()
    return [enrollment_dict(e, True) for e in rows]


@app.patch("/api/admin/enrollments/{eid}/status")
def set_status(eid: int, data: StatusIn, user: User = Depends(teacher), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    if data.status not in {"enrolled", "in_progress", "ready", "completed"}:
        raise HTTPException(422, "Invalid status")
    e = db.get(Enrollment, eid)
    if not e:
        raise HTTPException(404, "Work not found")
    if user.role != "admin":
        if e.task.academic_group_id not in get_user_group_ids(db, user):
            raise HTTPException(403, "Group access denied")
    e.status = data.status
    if data.status == "in_progress" and not e.started_at:
        e.started_at = datetime.now(timezone.utc)
    if data.status == "ready":
        e.ready_at = datetime.now(timezone.utc)
    if data.status == "completed":
        e.completed_at = datetime.now(timezone.utc)
        e.completed_by = user.id
    else:
        e.completed_at = None
        e.completed_by = None
    db.commit()
    return {"status": e.status}


@app.get("/api/admin/students")
def students(user: User = Depends(teacher), db: Session = Depends(get_db)):
    if user.role == "admin":
        users = db.scalars(select(User).where(User.role == "student").order_by(User.name)).all()
    else:
        allowed = get_user_group_ids(db, user)
        if not allowed:
            return []
        users = db.scalars(
            select(User)
            .join(GroupMembership, GroupMembership.user_id == User.id)
            .where(User.role == "student", GroupMembership.group_id.in_(list(allowed)), GroupMembership.status == "active")
            .order_by(User.name)
        ).all()
    out = []
    for u in users:
        counts = dict(db.execute(select(Enrollment.status, func.count()).where(Enrollment.student_id == u.id).group_by(Enrollment.status)).all())
        out.append({**UserOut.model_validate(u).model_dump(), "counts": counts})
    return out


@app.get("/api/admin/users")
def admin_users(_u: User = Depends(admin), db: Session = Depends(get_db)):
    return [UserOut.model_validate(u).model_dump() for u in db.scalars(select(User).order_by(User.created_at.desc())).all()]


@app.patch("/api/admin/users/{user_id}")
def update_user(user_id: int, data: UserUpdateIn, _u: User = Depends(admin), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if data.name is not None:
        user.name = data.name.strip()
    if data.email is not None:
        candidate = db.scalar(select(User).where(User.email == data.email.lower().strip()))
        if candidate and candidate.id != user.id:
            raise HTTPException(409, "Email already in use")
        user.email = data.email.lower().strip()
    if data.role is not None:
        role = data.role.strip().lower()
        if role not in {"student", "teacher", "admin"}:
            raise HTTPException(422, "Invalid role")
        user.role = role
        if role == "teacher":
            user.teacher_approved = True
        elif role == "student":
            user.teacher_approved = False
    if data.active is not None:
        user.active = data.active
    db.commit()
    return UserOut.model_validate(user).model_dump()


@app.post("/api/admin/users/{user_id}/block")
def block_user(user_id: int, _u: User = Depends(admin), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    admin_count = db.scalar(select(func.count()).select_from(User).where(User.role == "admin"))
    if user.role == "admin" and admin_count <= 1:
        raise HTTPException(409, "Cannot disable the last admin")
    user.active = False
    db.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
    db.commit()
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/unblock")
def unblock_user(user_id: int, _u: User = Depends(admin), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    user.active = True
    db.commit()
    return {"ok": True}


@app.post("/api/auth/change-password")
@app.post("/api/auth/password-change")
def change_password(data: PasswordChangeIn, user: User = Depends(current_user), session: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    current = (data.current_password or "").strip()
    new_password = (data.new_password or "").strip()
    if len(new_password) < 10:
        raise HTTPException(422, "Password must be at least 10 characters")
    if current and not verify_password(user.password_hash, current):
        raise HTTPException(401, "Current password is incorrect")
    if user.must_change_password and not current:
        raise HTTPException(400, "Current password is required when changing a reset password")
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.execute(delete(LoginSession).where(LoginSession.user_id == user.id, LoginSession.id != session.id))
    db.commit()
    return {"ok": True}


@app.patch("/api/me/password")
def change_password_alias(data: PasswordChangeIn, user: User = Depends(current_user), session: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    return change_password(data=data, user=user, session=session, db=db)


@app.post("/api/admin/users/{user_id}/reset-password")
def reset_password(user_id: int, data: PasswordResetIn, _u: User = Depends(admin), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    temp = secrets.token_urlsafe(12)
    user.password_hash = hash_password(temp)
    user.must_change_password = True
    db.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
    db.commit()
    return {"ok": True, "temporary_password": temp}


@app.get("/api/groups")
def list_groups(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role == "admin":
        groups = db.scalars(select(AcademicGroup).order_by(AcademicGroup.name)).all()
    elif user.role == "teacher":
        allowed = get_user_group_ids(db, user)
        groups = db.scalars(select(AcademicGroup).where(AcademicGroup.id.in_(list(allowed))).order_by(AcademicGroup.name)).all()
    else:
        allowed = get_user_group_ids(db, user)
        groups = db.scalars(select(AcademicGroup).where(AcademicGroup.id.in_(list(allowed))).order_by(AcademicGroup.name)).all()
    return [{"id": g.id, "name": g.name, "description": g.description, "is_active": g.is_active, "has_join_code": bool(g.join_code_hash)} for g in groups]


@app.post("/api/groups", status_code=201)
def create_group(data: AcademicGroupIn, _u: User = Depends(admin), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    if not data.name or not data.name.strip():
        raise HTTPException(422, "Group name is required")
    if db.scalar(select(AcademicGroup).where(AcademicGroup.name == data.name.strip())):
        raise HTTPException(409, "Group name already exists")
    group = AcademicGroup(name=data.name.strip(), description=data.description.strip(), is_active=data.is_active)
    if data.join_code:
        group.join_code_hash = hashlib.sha256(data.join_code.strip().encode()).hexdigest()
    db.add(group)
    db.commit()
    db.refresh(group)
    return {"id": group.id, "name": group.name, "description": group.description, "is_active": group.is_active}


@app.put("/api/groups/{group_id}")
def update_group(group_id: int, data: AcademicGroupIn, _u: User = Depends(admin), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    group = db.get(AcademicGroup, group_id)
    if not group:
        raise HTTPException(404, "Group not found")
    if data.name.strip() and data.name.strip() != group.name:
        if db.scalar(select(AcademicGroup).where(AcademicGroup.name == data.name.strip(), AcademicGroup.id != group_id)):
            raise HTTPException(409, "Group name already exists")
        group.name = data.name.strip()
    group.description = data.description.strip()
    group.is_active = data.is_active
    if data.join_code is not None:
        if data.join_code.strip():
            group.join_code_hash = hashlib.sha256(data.join_code.strip().encode()).hexdigest()
        else:
            group.join_code_hash = None
    db.commit()
    return {"id": group.id, "name": group.name, "description": group.description, "is_active": group.is_active}


@app.post("/api/groups/{group_id}/archive")
def archive_group(group_id: int, _u: User = Depends(admin), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    group = db.get(AcademicGroup, group_id)
    if not group:
        raise HTTPException(404, "Group not found")
    group.is_active = False
    db.commit()
    return {"ok": True}


@app.post("/api/groups/{group_id}/reactivate")
def reactivate_group(group_id: int, _u: User = Depends(admin), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    group = db.get(AcademicGroup, group_id)
    if not group:
        raise HTTPException(404, "Group not found")
    group.is_active = True
    db.commit()
    return {"ok": True}


@app.post("/api/groups/join")
def join_group(data: GroupJoinIn, user: User = Depends(current_user), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    if user.role != "student":
        raise HTTPException(403, "Student access required")
    code_hash = hashlib.sha256(data.join_code.strip().encode()).hexdigest()
    group = db.scalar(select(AcademicGroup).where(AcademicGroup.join_code_hash == code_hash, AcademicGroup.is_active.is_(True)))
    if not group:
        raise HTTPException(404, "Group not found")
    existing = db.scalar(select(GroupMembership).where(GroupMembership.group_id == group.id, GroupMembership.user_id == user.id))
    if existing:
        if existing.status == "removed":
            existing.status = "pending"
        else:
            return {"status": existing.status, "group_id": group.id}
    else:
        db.add(GroupMembership(group_id=group.id, user_id=user.id, status="pending"))
    db.commit()
    return {"status": "pending", "group_id": group.id}


@app.get("/api/my-groups")
def my_groups(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role == "student":
        rows = db.scalars(select(GroupMembership).where(GroupMembership.user_id == user.id).options(selectinload(GroupMembership.group))).all()
        return [{"group_id": r.group_id, "status": r.status, "name": r.group.name} for r in rows]
    if user.role in {"teacher", "admin"}:
        rows = db.scalars(select(GroupTeacher).where(GroupTeacher.teacher_id == user.id).options(selectinload(GroupTeacher.group))).all()
        return [{"group_id": r.group_id, "permission": r.permission, "name": r.group.name} for r in rows]
    return []


@app.get("/api/admin/teacher-requests")
def teacher_requests(_u: User = Depends(admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(RoleRequest).options(selectinload(RoleRequest.user)).order_by(RoleRequest.created_at.desc())).all()
    return [{"id": r.id, "user_id": r.user_id, "user_name": r.user.name, "requested_role": r.requested_role, "status": r.status, "note": r.note, "created_at": r.created_at} for r in rows]


@app.post("/api/admin/teacher-requests/{request_id}/approve")
def approve_teacher_request(request_id: int, data: TeacherRequestDecisionIn, actor: User = Depends(admin), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    req = db.get(RoleRequest, request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    user = req.user
    user.teacher_approved = True
    user.role = "teacher"
    req.status = "approved"
    req.reviewer_id = actor.id
    req.reviewed_at = datetime.now(timezone.utc)
    if data.note:
        req.note = data.note
    db.commit()
    return {"ok": True}


@app.post("/api/admin/teacher-requests/{request_id}/reject")
def reject_teacher_request(request_id: int, data: TeacherRequestDecisionIn, actor: User = Depends(admin), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    req = db.get(RoleRequest, request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    user = req.user
    user.teacher_approved = False
    req.status = "rejected"
    req.reviewer_id = actor.id
    req.reviewed_at = datetime.now(timezone.utc)
    if data.note:
        req.note = data.note
    db.commit()
    return {"ok": True}


@app.post("/api/admin/groups/{group_id}/teachers")
def add_group_teacher(group_id: int, data: GroupTeacherIn, _u: User = Depends(admin), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    group = db.get(AcademicGroup, group_id)
    teacher_user = db.get(User, data.teacher_id)
    if not group or not teacher_user:
        raise HTTPException(404, "Group or teacher not found")
    permission = data.permission.strip().lower()
    if permission not in {"owner", "assistant", "viewer"}:
        raise HTTPException(422, "Invalid permission")
    if teacher_user.role not in {"teacher", "admin"}:
        raise HTTPException(409, "User is not a teacher")
    existing = db.scalar(select(GroupTeacher).where(GroupTeacher.group_id == group_id, GroupTeacher.teacher_id == data.teacher_id))
    if existing:
        existing.permission = permission
    else:
        db.add(GroupTeacher(group_id=group_id, teacher_id=data.teacher_id, permission=permission))
    db.commit()
    return {"ok": True}


@app.get("/api/admin/groups/{group_id}/teachers")
def list_group_teachers(group_id: int, _u: User = Depends(admin), db: Session = Depends(get_db)):
    if not db.get(AcademicGroup, group_id):
        raise HTTPException(404, "Group not found")
    rows = db.scalars(
        select(GroupTeacher)
        .options(selectinload(GroupTeacher.teacher))
        .where(GroupTeacher.group_id == group_id)
        .order_by(GroupTeacher.created_at)
    ).all()
    return [
        {
            "id": row.id,
            "teacher_id": row.teacher_id,
            "name": row.teacher.name,
            "email": row.teacher.email,
            "permission": row.permission,
        }
        for row in rows
    ]


@app.delete("/api/admin/groups/{group_id}/teachers/{teacher_id}")
def remove_group_teacher(group_id: int, teacher_id: int, _u: User = Depends(admin), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    row = db.scalar(select(GroupTeacher).where(GroupTeacher.group_id == group_id, GroupTeacher.teacher_id == teacher_id))
    if not row:
        raise HTTPException(404, "Teacher assignment not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


@app.post("/api/admin/groups/{group_id}/students")
def add_group_student(group_id: int, data: GroupStudentIn, _u: User = Depends(admin), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    group = db.get(AcademicGroup, group_id)
    user = db.get(User, data.user_id)
    if not group or not user:
        raise HTTPException(404, "Group or user not found")
    status = data.status.strip().lower()
    if status not in {"pending", "active", "removed"}:
        raise HTTPException(422, "Invalid status")
    existing = db.scalar(select(GroupMembership).where(GroupMembership.group_id == group_id, GroupMembership.user_id == data.user_id))
    if existing:
        existing.status = status
    else:
        db.add(GroupMembership(group_id=group_id, user_id=data.user_id, status=status))
    db.commit()
    return {"ok": True}


@app.delete("/api/admin/groups/{group_id}/students/{user_id}")
def remove_group_student(group_id: int, user_id: int, _u: User = Depends(admin), _: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    row = db.scalar(select(GroupMembership).where(GroupMembership.group_id == group_id, GroupMembership.user_id == user_id))
    if not row:
        raise HTTPException(404, "Membership not found")
    row.status = "removed"
    db.commit()
    return {"ok": True}


@app.get("/api/admin/groups/{group_id}/members")
def group_members(group_id: int, _u: User = Depends(admin), db: Session = Depends(get_db)):
    if not db.get(AcademicGroup, group_id):
        raise HTTPException(404, "Group not found")
    rows = db.scalars(
        select(GroupMembership)
        .options(selectinload(GroupMembership.user))
        .where(GroupMembership.group_id == group_id)
        .order_by(GroupMembership.status, GroupMembership.created_at)
    ).all()
    return [
        {
            "id": row.id,
            "user_id": row.user_id,
            "name": row.user.name,
            "email": row.user.email,
            "status": row.status,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@app.patch("/api/admin/groups/{group_id}/members/{membership_id}")
def update_group_membership(
    group_id: int,
    membership_id: int,
    data: GroupStudentIn,
    actor: User = Depends(admin),
    _: LoginSession = Depends(csrf),
    db: Session = Depends(get_db),
):
    row = db.scalar(
        select(GroupMembership).where(
            GroupMembership.id == membership_id,
            GroupMembership.group_id == group_id,
        )
    )
    if not row:
        raise HTTPException(404, "Membership not found")
    status_value = data.status.strip().lower()
    if status_value not in {"pending", "active", "removed"}:
        raise HTTPException(422, "Invalid status")
    row.status = status_value
    log_audit(db, actor.id, "update_group_membership", "group_membership", row.id, {"status": status_value})
    db.commit()
    return {"id": row.id, "status": row.status}


@app.get("/api/admin/groups")
def admin_groups(_u: User = Depends(admin), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(AcademicGroup)
        .options(selectinload(AcademicGroup.memberships), selectinload(AcademicGroup.teachers))
        .order_by(AcademicGroup.name)
    ).all()
    return [
        {
            "id": g.id,
            "name": g.name,
            "description": g.description,
            "is_active": g.is_active,
            "has_join_code": bool(g.join_code_hash),
            "member_count": sum(m.status == "active" for m in g.memberships),
            "pending_count": sum(m.status == "pending" for m in g.memberships),
            "teacher_count": len(g.teachers),
        }
        for g in rows
    ]


@app.get("/api/admin/audit-log")
def audit_log(_u: User = Depends(admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(200)).all()
    return [{"id": r.id, "actor_id": r.actor_id, "action": r.action, "target_type": r.target_type, "target_id": r.target_id, "details": r.details, "created_at": r.created_at} for r in rows]


@app.get("/api/admin/teacher-approval")
def teacher_approval(_u: User = Depends(admin), db: Session = Depends(get_db)):
    return teacher_requests(_u, db)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/{path:path}")
def spa(path: str):
    return FileResponse(STATIC / "index.html")
