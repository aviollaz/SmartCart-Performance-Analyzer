"""API de ingesta de SmartCart Performance Analysis.

Un solo trabajo: recibir el evento de "carrito optimizado" y guardarlo. Nada de
lógica de negocio, nada de leer datos para el frontend. Los KPIs los arma Power
BI sobre las vistas del data warehouse.
"""

import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status

from src import db
from src.config import Settings, get_settings
from src.models import IngestEvent
from src.repository import insert_optimization

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.open_pool()
    yield
    db.close_pool()


app = FastAPI(
    title="SmartCart Performance Analysis",
    description=(
        "Microservicio de ingesta analítica. Recibe un evento por cada carrito "
        "optimizado en SmartCart y lo persiste en el data warehouse que consume Power BI."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# Sin CORS a propósito: a este endpoint lo llama el backend de SmartCart, no un
# browser. Habilitar CORS acá sólo serviría para que cualquier página pueda
# inyectar métricas falsas.


def require_api_key(
    x_api_key: str = Header(default="", alias="X-API-Key"),
    settings: Settings = Depends(get_settings),
) -> None:
    """Secreto compartido con SmartCart.

    `compare_digest` y no `!=`: la comparación de strings de Python corta en el
    primer byte distinto y filtra el largo del prefijo acertado por timing.
    """
    if not secrets.compare_digest(x_api_key, settings.analytics_api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key inválida o ausente")


@app.get("/health")
def health() -> dict:
    """Estado del servicio y de su base.

    SmartCart lo puede consultar antes de reintentar un envío fallido, para no
    machacar un servicio que está caído.
    """
    db_ok = db.ping()
    return {"status": "ok" if db_ok else "degraded", "db": "ok" if db_ok else "unreachable"}


@app.post(
    "/events/cart-optimized",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
async def ingest_cart_optimized(
    event: IngestEvent,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Guarda un evento de carrito optimizado.

    - 201 `stored`    — se guardó
    - 200 `duplicate` — ya existía un evento con ese `event_id`; no se hizo nada
    - 401             — API key inválida
    - 422             — el payload no cumple el contrato (ver src/models.py)
    """
    # El body crudo, no `event.model_dump()`: en `raw_payload` queremos lo que
    # SmartCart mandó realmente, sin los defaults ni las coerciones de Pydantic.
    # Es lo que permite backfillear un KPI nuevo con confianza.
    raw_payload = await request.json()

    try:
        with db.get_connection() as conn:
            result = insert_optimization(
                conn, event, raw_payload, default_env=settings.environment
            )
    except Exception as exc:
        logger.exception("Error guardando el evento %s", event.event_id)
        raise HTTPException(status_code=500, detail=f"No se pudo guardar el evento: {exc}") from exc

    if result.duplicate:
        # 200 y no 4xx: un reintento no es un error del emisor. Que SmartCart
        # reintente hasta tener confirmación es exactamente lo que queremos.
        response.status_code = status.HTTP_200_OK
        return {"status": "duplicate", "event_id": str(event.event_id)}

    return {
        "status": "stored",
        "id": result.optimization_id,
        "event_id": str(event.event_id),
    }
