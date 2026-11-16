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

    # Connection string completa (la de Neon en producción, con
    # `?sslmode=require`). Si está seteada, le gana a las POSTGRES_* de arriba:
    # en Cloud Run llega como un único secreto y no hace falta partirla.
    database_url: str | None = None

    # --- Pool ---
    # Mismo criterio que SMARTCART_POOL_MIN_SIZE en la app principal: contra Neon,
    # una conexión sostenida por el pool impide que la base se suspenda y gasta
    # las horas de cómputo del plan gratuito. En producción va 0; en local da
    # igual y 1 evita el handshake del primer evento.
    pool_min_size: int = 1
    pool_max_size: int = 10
    # Segundos que una conexión ociosa por encima de min_size sobrevive. Con
    # min_size=0 es lo que efectivamente libera la base entre eventos.
    pool_max_idle: float = 60.0

    # --- API ---
    analytics_api_key: str = "dev-local-key-change-me"
    environment: str = "dev"

    @property
    def dsn(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """Instancia única. El lru_cache evita releer el .env en cada request."""
    return Settings()
