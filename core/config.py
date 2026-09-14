import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Application
    APP_ENV: str = "development"
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000

    # RabbitMQ
    RABBITMQ_HOST: str = "localhost"
    RABBITMQ_PORT: int = 5672
    RABBITMQ_USER: str = "guest"
    RABBITMQ_PASSWORD: str = "guest"
    RABBITMQ_REPOSITORY_QUEUE: str = "repository_queue"
    RABBITMQ_REVIEW_QUEUE: str = "review_queue"
    RABBITMQ_DEAD_LETTER_QUEUE: str = "review_dlq"

    # PostgreSQL
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "code_review"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    DATABASE_URL: str | None = None

    # Storage & Upload Limits
    STORAGE_PATH: str = "./storage"
    MAX_ZIP_SIZE_MB: int = 25
    MAX_FILES_COUNT: int = 500
    MAX_SOURCE_FILE_SIZE_KB: int = 500

    # LLM Settings
    LLM_PROVIDER: str = "mock"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "gemini-1.5-flash"

    @property
    def sync_database_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@"
            f"{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def uploads_dir(self) -> Path:
        p = Path(self.STORAGE_PATH) / "uploads"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def extracted_dir(self) -> Path:
        p = Path(self.STORAGE_PATH) / "extracted"
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
