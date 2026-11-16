"""Pool de conexiones al data warehouse.

Un pool y no una conexión por request: el endpoint de ingesta hace un INSERT
corto y muy frecuente, y abrir un socket + handshake de Postgres cada vez
costaría más que la escritura en sí.
"""

import logging
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool

from src.config import get_settings

logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def open_pool() -> ConnectionPool:
    """Abre el pool. Lo llama el lifespan de FastAPI en el arranque."""
    global _pool
    if _pool is None:
        settings = get_settings()
        # `open=True` con `check` hace que un Postgres caído se note en el
        # arranque y no en el primer evento perdido.
        # Con min_size=0 el pool arranca sin conexiones, así que `open=True` ya
        # no valida la base en el arranque: un Neon suspendido no frena el
        # deploy, y GET /health sigue siendo el lugar donde mirar.
        _pool = ConnectionPool(
            conninfo=settings.dsn,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            max_idle=settings.pool_max_idle,
            open=True,
            check=ConnectionPool.check_connection,
        )
        # Nunca loguear el DSN: con DATABASE_URL trae la contraseña.
        target = "DATABASE_URL" if settings.database_url else f"{settings.postgres_host}:{settings.postgres_port}"
        logger.info(
            "Pool de conexiones abierto contra %s (min=%s, max=%s)",
            target, settings.pool_min_size, settings.pool_max_size,
        )
    return _pool


def close_pool() -> None:
    """Cierra el pool en el shutdown."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
        logger.info("Pool de conexiones cerrado.")


@contextmanager
def get_connection() -> Iterator[psycopg.Connection]:
    """Presta una conexión del pool.

    Al salir del `with`, psycopg hace COMMIT si no hubo excepción y ROLLBACK si
    la hubo. De eso depende que un evento se guarde entero o no se guarde: nunca
    un header sin sus filas por tienda.
    """
    pool = open_pool()
    with pool.connection() as conn:
        yield conn


def ping() -> bool:
    """True si la base responde. Lo usa GET /health."""
    try:
        with get_connection() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception as exc:  # noqa: BLE001 - health check: cualquier fallo es "no"
        logger.warning("Health check contra la base falló: %s", exc)
        return False
