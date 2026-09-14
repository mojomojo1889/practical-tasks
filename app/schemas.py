from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    group_name: str | None = Field(default=None, max_length=80)
    invite_code: str = Field(min_length=1, max_length=100)

class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int; name: str; email: str; role: str; group_name: str | None

class MeOut(UserOut):
    csrf_token: str

class TaskIn(BaseModel):
    title: str = Field(min_length=3, max_length=180)
    category: str = Field(default="other", max_length=80)
    description: str = Field(default="", max_length=10000)
    location: str = Field(default="", max_length=160)
    room: str = Field(default="", max_length=80)
    scheduled_at: datetime | None = None
    due_at: datetime | None = None
    capacity: int | None = Field(default=None, ge=1, le=500)
    is_open: bool = True
    @field_validator("title", "category", "location", "room")
    @classmethod
    def strip_text(cls, v): return v.strip()

class TaskOut(TaskIn):
    model_config = ConfigDict(from_attributes=True)
    id: int; is_archived: bool; created_at: datetime; enrolled_count: int = 0

class NoteIn(BaseModel):
    note: str = Field(default="", max_length=10000)

class StatusIn(BaseModel):
    status: str

class AssignIn(BaseModel):
    student_id: int
