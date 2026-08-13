"""Fixtures compartidas.

Los tests corren contra el Postgres dockerizado, no contra un mock: lo que se
está verificando es justamente el contrato con la base (la restricción UNIQUE que
da la idempotencia, la FK contra dim_store, el CHECK del status). Un mock de la
capa de datos no probaría nada de eso.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import db
from src.config import get_settings
from src.main import app

FIXTURES = Path(__file__).parent / "fixtures"

# Marca propia para los eventos de test: el teardown borra por acá, así que nunca
# puede llevarse por delante datos reales ni los del seed.
TEST_ENV = "pytest"


@pytest.fixture(scope="session", autouse=True)
def require_database():
    """Si la base no está levantada, se saltan los tests con un mensaje útil."""
    if not db.ping():
        pytest.skip(
            "El data warehouse no responde. Levantalo con `docker compose up -d` "
            "y revisá POSTGRES_PORT en el .env (tiene que ser 5433).",
            allow_module_level=True,
        )


# scope="session" a propósito: el lifespan de FastAPI abre y cierra el pool de
# conexiones, y con un cliente por test cada teardown lo cerraba mientras la
# limpieza de abajo seguía necesitándolo. Eso dejaba un pool huérfano por test,
# que aparecía al final como una catarata de "I/O operation on closed file".
# Un solo cliente para toda la sesión = un solo pool, abierto y cerrado una vez.
@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def api_key() -> str:
    return get_settings().analytics_api_key


@pytest.fixture
def auth_headers(api_key: str) -> dict:
    return {"X-API-Key": api_key}


@pytest.fixture
def base_event() -> dict:
    """El payload de ejemplo, con un event_id nuevo en cada test.

    Sin el event_id nuevo, el segundo test que lo usara recibiría 'duplicate' del
    test anterior y fallaría por un motivo que no tiene nada que ver con lo que
    quiere probar.
    """
    event = json.loads((FIXTURES / "optimize_success.json").read_text(encoding="utf-8"))
    event["event_id"] = str(uuid.uuid4())
    event["occurred_at"] = datetime.now(timezone.utc).isoformat()
    event["source"]["env"] = TEST_ENV
    return event


@pytest.fixture(autouse=True)
def clean_test_events():
    """Borra los eventos de test al terminar.

    El ON DELETE CASCADE de fact_optimization_store se lleva las líneas por
    tienda, así que alcanza con borrar los headers.
    """
    yield
    with db.get_connection() as conn:
        conn.execute("DELETE FROM fact_optimization WHERE source_env = %s", (TEST_ENV,))


def count_store_lines(optimization_id: int) -> int:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM fact_optimization_store WHERE optimization_id = %s",
            (optimization_id,),
        ).fetchone()
    return row[0]


def fetch_header(optimization_id: int) -> dict:
    with db.get_connection() as conn:
        cur = conn.execute(
            """
            SELECT items_count, units_count, stores_used, status,
                   total_spent_net, total_subtotal, total_delivery_cost,
                   total_bank_discount, price_savings_total, zone, memberships
            FROM fact_optimization WHERE id = %s
            """,
            (optimization_id,),
        )
        columns = [c.name for c in cur.description]
        return dict(zip(columns, cur.fetchone()))
