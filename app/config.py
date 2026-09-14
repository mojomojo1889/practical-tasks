from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "Practical Tasks"
    database_url: str = "sqlite:////data/app.db"
    upload_dir: Path = Path("/data/uploads")
    session_days: int = 7
    cookie_secure: bool = False
    invite_code: str = "CHANGE-ME"
    admin_name: str = "Teacher"
    admin_email: str = "teacher@example.invalid"
    admin_password: str = "CHANGE-ME-NOW"
    max_photo_mb: int = 5
    max_photos: int = 5
    seed_demo: bool = False
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
