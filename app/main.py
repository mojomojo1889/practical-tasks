from __future__ import annotations
import io, secrets
from datetime import datetime, timezone
from pathlib import Path
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from .auth import (DUMMY, create_session, csrf, current_session, current_user, digest,
                   hash_password, ph, rate_limit, teacher, verify_password)
from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .models import Attachment, Enrollment, LoginSession, Task, User
from .schemas import AssignIn, LoginIn, MeOut, NoteIn, RegisterIn, StatusIn, TaskIn, UserOut

app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None)
STATIC = Path(__file__).parent / "static"
settings.upload_dir.mkdir(parents=True, exist_ok=True)
Image.MAX_IMAGE_PIXELS = 25_000_000

@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        email = settings.admin_email.lower().strip()
        if not db.scalar(select(User).where(User.email == email)):
            if settings.admin_password == "CHANGE-ME-NOW":
                raise RuntimeError("Set ADMIN_PASSWORD in .env before first start")
            db.add(User(name=settings.admin_name, email=email, password_hash=hash_password(settings.admin_password), role="teacher"))
            db.commit()

@app.get("/health")
def health(): return {"status": "ok"}

@app.post("/api/auth/register", response_model=UserOut, status_code=201)
def register(data: RegisterIn, db: Session = Depends(get_db)):
    if not secrets.compare_digest(data.invite_code, settings.invite_code): raise HTTPException(403, "Invalid invite code")
    email = data.email.lower().strip()
    if db.scalar(select(User).where(User.email == email)): raise HTTPException(409, "Email already registered")
    user = User(name=data.name.strip(), email=email, password_hash=hash_password(data.password), group_name=(data.group_name or "").strip() or None)
    db.add(user); db.commit(); db.refresh(user); return user

@app.post("/api/auth/login", response_model=MeOut)
def login(data: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "unknown"; rate_limit(ip)
    user = db.scalar(select(User).where(User.email == data.email.lower().strip()))
    ok = verify_password(user.password_hash if user else DUMMY, data.password)
    if not user or not ok or not user.active: raise HTTPException(401, "Incorrect email or password")
    raw, row = create_session(db, user)
    response.set_cookie("session", raw, max_age=settings.session_days*86400, httponly=True,
                        secure=settings.cookie_secure, samesite="lax", path="/")
    return MeOut(**UserOut.model_validate(user).model_dump(), csrf_token=row.csrf_token)

@app.post("/api/auth/logout", status_code=204)
def logout(response: Response, s: LoginSession = Depends(csrf), db: Session = Depends(get_db)):
    db.delete(s); db.commit(); response.delete_cookie("session", path="/")

@app.get("/api/me", response_model=MeOut)
def me(s: LoginSession = Depends(current_session)):
    return MeOut(**UserOut.model_validate(s.user).model_dump(), csrf_token=s.csrf_token)

def task_dict(task: Task):
    return {"id":task.id,"title":task.title,"category":task.category,"description":task.description,
            "location":task.location,"room":task.room,"scheduled_at":task.scheduled_at,"due_at":task.due_at,
            "capacity":task.capacity,"is_open":task.is_open,"is_archived":task.is_archived,
            "created_at":task.created_at,"enrolled_count":len(task.enrollments)}

def enrollment_dict(e: Enrollment, include_student=False):
    d={"id":e.id,"status":e.status,"note":e.note,"created_at":e.created_at,"started_at":e.started_at,
       "ready_at":e.ready_at,"completed_at":e.completed_at,"task":task_dict(e.task),
       "photos":[{"id":p.id,"name":p.original_name,"mime_type":p.mime_type,"size":p.size,"url":f"/api/photos/{p.id}"} for p in e.photos]}
    if include_student: d["student"] = UserOut.model_validate(e.student).model_dump()
    return d

@app.get("/api/tasks")
def tasks(user: User=Depends(current_user), db: Session=Depends(get_db)):
    rows=db.scalars(select(Task).options(selectinload(Task.enrollments)).where(Task.is_archived==False).order_by(Task.scheduled_at.is_(None),Task.scheduled_at,Task.created_at.desc())).all()
    my={e.task_id:e for e in db.scalars(select(Enrollment).where(Enrollment.student_id==user.id)).all()} if user.role=="student" else {}
    out=[]
    for t in rows:
        d=task_dict(t); d["my_enrollment"] = ({"id":my[t.id].id,"status":my[t.id].status} if t.id in my else None); out.append(d)
    return out

@app.post("/api/tasks", status_code=201)
def create_task(data: TaskIn, user: User=Depends(teacher), _:LoginSession=Depends(csrf), db:Session=Depends(get_db)):
    task=Task(**data.model_dump(),teacher_id=user.id); db.add(task); db.commit(); db.refresh(task); task.enrollments=[]; return task_dict(task)

@app.put("/api/tasks/{task_id}")
def update_task(task_id:int,data:TaskIn,_u:User=Depends(teacher),_:LoginSession=Depends(csrf),db:Session=Depends(get_db)):
    task=db.get(Task,task_id)
    if not task: raise HTTPException(404,"Task not found")
    for k,v in data.model_dump().items(): setattr(task,k,v)
    db.commit(); db.refresh(task); db.refresh(task,["enrollments"]); return task_dict(task)

@app.post("/api/tasks/{task_id}/archive")
def archive_task(task_id:int,_u:User=Depends(teacher),_:LoginSession=Depends(csrf),db:Session=Depends(get_db)):
    task=db.get(Task,task_id)
    if not task: raise HTTPException(404,"Task not found")
    task.is_archived=True; task.is_open=False; db.commit(); return {"ok":True}

@app.post("/api/tasks/{task_id}/enroll", status_code=201)
def enroll(task_id:int,user:User=Depends(current_user),_:LoginSession=Depends(csrf),db:Session=Depends(get_db)):
    if user.role!="student": raise HTTPException(403,"Student access required")
    task=db.scalar(select(Task).options(selectinload(Task.enrollments)).where(Task.id==task_id))
    if not task or task.is_archived: raise HTTPException(404,"Task not found")
    if not task.is_open: raise HTTPException(409,"Enrollment is closed")
    if db.scalar(select(Enrollment).where(Enrollment.task_id==task_id,Enrollment.student_id==user.id)): raise HTTPException(409,"Already enrolled")
    if task.capacity and len(task.enrollments)>=task.capacity: raise HTTPException(409,"Task is full")
    e=Enrollment(task_id=task_id,student_id=user.id); db.add(e); db.commit(); return {"id":e.id,"status":e.status}

@app.post("/api/tasks/{task_id}/assign",status_code=201)
def assign(task_id:int,data:AssignIn,_u:User=Depends(teacher),_:LoginSession=Depends(csrf),db:Session=Depends(get_db)):
    task=db.get(Task,task_id); student=db.get(User,data.student_id)
    if not task or task.is_archived: raise HTTPException(404,"Task not found")
    if not student or student.role!="student": raise HTTPException(404,"Student not found")
    if db.scalar(select(Enrollment).where(Enrollment.task_id==task_id,Enrollment.student_id==student.id)): raise HTTPException(409,"Already assigned")
    e=Enrollment(task_id=task_id,student_id=student.id);db.add(e);db.commit();return {"id":e.id,"status":e.status}

@app.get("/api/my-work")
def my_work(user:User=Depends(current_user),db:Session=Depends(get_db)):
    if user.role!="student": raise HTTPException(403,"Student access required")
    rows=db.scalars(select(Enrollment).options(selectinload(Enrollment.task).selectinload(Task.enrollments),selectinload(Enrollment.photos)).where(Enrollment.student_id==user.id).order_by(Enrollment.completed_at.desc(),Enrollment.created_at.desc())).all()
    return [enrollment_dict(e) for e in rows]

@app.post("/api/enrollments/{eid}/start")
def start_work(eid:int,user:User=Depends(current_user),_:LoginSession=Depends(csrf),db:Session=Depends(get_db)):
    e=db.get(Enrollment,eid)
    if not e or e.student_id!=user.id: raise HTTPException(404,"Work not found")
    if e.status!="enrolled": raise HTTPException(409,"Work cannot be started")
    e.status="in_progress";e.started_at=datetime.now(timezone.utc);db.commit();return {"status":e.status}

@app.patch("/api/enrollments/{eid}/note")
def save_note(eid:int,data:NoteIn,user:User=Depends(current_user),_:LoginSession=Depends(csrf),db:Session=Depends(get_db)):
    e=db.get(Enrollment,eid)
    if not e or e.student_id!=user.id: raise HTTPException(404,"Work not found")
    if e.status=="completed": raise HTTPException(409,"Completed work is locked")
    e.note=data.note;db.commit();return {"ok":True}

@app.post("/api/enrollments/{eid}/submit")
def submit_work(eid:int,user:User=Depends(current_user),_:LoginSession=Depends(csrf),db:Session=Depends(get_db)):
    e=db.get(Enrollment,eid)
    if not e or e.student_id!=user.id: raise HTTPException(404,"Work not found")
    if e.status not in ("enrolled","in_progress"): raise HTTPException(409,"Work cannot be submitted")
    e.status="ready";e.ready_at=datetime.now(timezone.utc);db.commit();return {"status":e.status}

@app.post("/api/enrollments/{eid}/photos",status_code=201)
async def upload_photos(eid:int,files:list[UploadFile]=File(...),user:User=Depends(current_user),_:LoginSession=Depends(csrf),db:Session=Depends(get_db)):
    e=db.scalar(select(Enrollment).options(selectinload(Enrollment.photos)).where(Enrollment.id==eid))
    if not e or (user.role!="teacher" and e.student_id!=user.id): raise HTTPException(404,"Work not found")
    if e.status=="completed": raise HTTPException(409,"Completed work is locked")
    if not files or len(e.photos)+len(files)>settings.max_photos: raise HTTPException(400,f"Maximum {settings.max_photos} photos")
    created=[]; limit=settings.max_photo_mb*1024*1024
    for upload in files:
        raw=await upload.read(limit+1)
        if len(raw)>limit: raise HTTPException(413,f"Each photo must be at most {settings.max_photo_mb} MB")
        try:
            img=Image.open(io.BytesIO(raw)); img.verify(); img=Image.open(io.BytesIO(raw))
            fmt=(img.format or "").upper()
            if fmt not in {"JPEG","PNG","WEBP"}: raise HTTPException(415,"Only JPEG, PNG and WebP are allowed")
            ext={"JPEG":"jpg","PNG":"png","WEBP":"webp"}[fmt]; mime={"JPEG":"image/jpeg","PNG":"image/png","WEBP":"image/webp"}[fmt]
            if fmt=="JPEG" and img.mode not in ("RGB","L"): img=img.convert("RGB")
            name=f"{secrets.token_hex(20)}.{ext}"; path=settings.upload_dir/name
            save_args={"optimize":True};
            if fmt=="JPEG": save_args["quality"]=88
            img.save(path,format=fmt,**save_args)
        except (UnidentifiedImageError,OSError,Image.DecompressionBombError): raise HTTPException(415,"Invalid or unsafe image")
        att=Attachment(enrollment_id=e.id,stored_name=name,original_name=Path(upload.filename or name).name[:255],mime_type=mime,size=path.stat().st_size)
        db.add(att);db.flush();created.append({"id":att.id,"name":att.original_name,"url":f"/api/photos/{att.id}"})
    db.commit();return created

@app.delete("/api/photos/{photo_id}",status_code=204)
def delete_photo(photo_id:int,user:User=Depends(current_user),_:LoginSession=Depends(csrf),db:Session=Depends(get_db)):
    p=db.scalar(select(Attachment).options(selectinload(Attachment.enrollment)).where(Attachment.id==photo_id))
    if not p or (user.role!="teacher" and p.enrollment.student_id!=user.id): raise HTTPException(404,"Photo not found")
    if p.enrollment.status=="completed": raise HTTPException(409,"Completed work is locked")
    path=settings.upload_dir/p.stored_name; db.delete(p);db.commit();path.unlink(missing_ok=True)

@app.get("/api/photos/{photo_id}")
def photo(photo_id:int,user:User=Depends(current_user),db:Session=Depends(get_db)):
    p=db.scalar(select(Attachment).options(selectinload(Attachment.enrollment)).where(Attachment.id==photo_id))
    if not p or (user.role!="teacher" and p.enrollment.student_id!=user.id): raise HTTPException(404,"Photo not found")
    path=settings.upload_dir/p.stored_name
    if not path.is_file(): raise HTTPException(404,"Photo file missing")
    return FileResponse(path,media_type=p.mime_type,filename=p.original_name,content_disposition_type="inline")

@app.get("/api/admin/enrollments")
def all_work(_u:User=Depends(teacher),db:Session=Depends(get_db)):
    rows=db.scalars(select(Enrollment).options(selectinload(Enrollment.task).selectinload(Task.enrollments),selectinload(Enrollment.student),selectinload(Enrollment.photos)).order_by(Enrollment.completed_at.desc(),Enrollment.created_at.desc())).all()
    return [enrollment_dict(e,True) for e in rows]

@app.patch("/api/admin/enrollments/{eid}/status")
def set_status(eid:int,data:StatusIn,user:User=Depends(teacher),_:LoginSession=Depends(csrf),db:Session=Depends(get_db)):
    if data.status not in {"enrolled","in_progress","ready","completed"}: raise HTTPException(422,"Invalid status")
    e=db.get(Enrollment,eid)
    if not e: raise HTTPException(404,"Work not found")
    e.status=data.status
    if data.status=="in_progress" and not e.started_at:e.started_at=datetime.now(timezone.utc)
    if data.status=="ready":e.ready_at=datetime.now(timezone.utc)
    if data.status=="completed":e.completed_at=datetime.now(timezone.utc);e.completed_by=user.id
    else:e.completed_at=None;e.completed_by=None
    db.commit();return {"status":e.status}

@app.get("/api/admin/students")
def students(_u:User=Depends(teacher),db:Session=Depends(get_db)):
    users=db.scalars(select(User).where(User.role=="student").order_by(User.name)).all()
    out=[]
    for u in users:
        counts=dict(db.execute(select(Enrollment.status,func.count()).where(Enrollment.student_id==u.id).group_by(Enrollment.status)).all())
        out.append({**UserOut.model_validate(u).model_dump(),"counts":counts})
    return out

app.mount("/static",StaticFiles(directory=STATIC),name="static")
@app.get("/{path:path}")
def spa(path:str): return FileResponse(STATIC/"index.html")
