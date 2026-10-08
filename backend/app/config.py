from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str
    redis_url: str

    groq_api_key: SecretStr
    groq_model: str = "openai/gpt-oss-120b"

    github_webhook_secret: SecretStr
    github_token: SecretStr

    github_api_url: str = "https://api.github.com"
    github_api_version: str = "2026-03-10"

    github_timeout_seconds: float = 20.0

    ai_timeout_seconds: float = 60.0

    max_diff_chars: int = 120000

    environment: str = "development"

    @property
    def sqlalchemy_database_url(self) -> str:
        if self.database_url.startswith("postgres://"):
            return self.database_url.replace(
                "postgres://",
                "postgresql+psycopg2://",
                1,
            )

        if self.database_url.startswith("postgresql://"):
            return self.database_url.replace(
                "postgresql://",
                "postgresql+psycopg2://",
                1,
            )

        return self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()