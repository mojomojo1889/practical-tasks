from __future__ import annotations
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    role: str | None = Field(default=None, max_length=20)
    requested_role: str | None = Field(default=None, max_length=20)

    @field_validator("role", "requested_role", mode="before")
    @classmethod
    def normalize_role(cls, value: Any):
        if value is None:
            return value
        if isinstance(value, str):
            value = value.strip().lower()
        return value

    @model_validator(mode="after")
    def validate_role_choice(self):
        role = (self.requested_role or self.role or "student").strip().lower()
        if role not in {"student", "teacher"}:
            raise ValueError("role must be 'student' or 'teacher'")
        self.requested_role = role
        if self.role is not None:
            self.role = role
        return self


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    email: str
    role: str
    group_name: str | None = None
    active: bool = True
    teacher_approved: bool = False
    must_change_password: bool = False


class MeOut(UserOut):
    csrf_token: str


class TaskIn(BaseModel):
    title: str = Field(min_length=3, max_length=180)
    category: str = Field(default="other", max_length=80)
    category_key: str | None = Field(default=None, max_length=30)
    custom_category: str | None = Field(default=None, max_length=80)
    priority: str = Field(default="normal", max_length=20)
    description: str = Field(default="", max_length=10000)
    location: str = Field(default="", max_length=160)
    room: str = Field(default="", max_length=80)
    scheduled_at: datetime | None = None
    due_at: datetime | None = None
    capacity: int | None = Field(default=None, ge=1, le=500)
    is_open: bool = True
    academic_group_id: int | None = None

    @field_validator("title", "category", "category_key", "custom_category", "location", "room", "priority")
    @classmethod
    def strip_text(cls, v):
        if v is None:
            return v
        return v.strip()

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, value):
        if value not in {"urgent", "important", "normal"}:
            raise ValueError("priority must be urgent, important, or normal")
        return value


class TaskOut(TaskIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    is_archived: bool
    created_at: datetime
    enrolled_count: int = 0


class NoteIn(BaseModel):
    note: str = Field(default="", max_length=10000)


class StatusIn(BaseModel):
    status: str


class AssignIn(BaseModel):
    student_id: int


class AcademicGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    join_code: str | None = Field(default=None, min_length=1, max_length=50)
    is_active: bool = True


class GroupJoinIn(BaseModel):
    join_code: str = Field(min_length=1, max_length=50)


class UserUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    email: EmailStr | None = None
    role: str | None = Field(default=None, max_length=20)
    active: bool | None = None


class TeacherRequestDecisionIn(BaseModel):
    action: str = Field(default="approve")
    note: str | None = Field(default=None, max_length=500)


class GroupTeacherIn(BaseModel):
    teacher_id: int
    permission: str = Field(default="assistant")


class GroupStudentIn(BaseModel):
    user_id: int
    status: str = Field(default="active")


class PasswordChangeIn(BaseModel):
    current_password: str | None = Field(default=None, min_length=1, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)


class PasswordResetIn(BaseModel):
    action: str = Field(default="reset")
