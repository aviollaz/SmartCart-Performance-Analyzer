"""Configuración del servicio, leída del entorno (o del .env local)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Base de datos ---
    # El default es 5433 y no 5432 a propósito: el 5432 lo ocupa el Postgres de
    # la app principal. Un default de 5432 acá haría que este servicio escriba
    # sus métricas en la base de SmartCart si alguien se olvida del .env.
    postgres_user: str = "analytics"
    postgres_password: str = "analytics_pass"
    postgres_db: str = "smartcart_analytics"
    postgres_host: str = "localhost"
    postgres_port: int = 5433

    # --- API ---
    analytics_api_key: str = "dev-local-key-change-me"
    environment: str = "dev"

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """Instancia única. El lru_cache evita releer el .env en cada request."""
    return Settings()
