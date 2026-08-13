"""Traducción del evento a filas del data warehouse.

Todo lo agregado (conteos y totales) se calcula acá y no lo manda SmartCart: es
una sola definición, en un solo lugar, y no puede desincronizarse de la app
principal si mañana alguien toca el frontend.
"""

import logging
from typing import NamedTuple

import psycopg
from psycopg.types.json import Json

from src.models import IngestEvent

logger = logging.getLogger(__name__)


class InsertResult(NamedTuple):
    optimization_id: int | None
    duplicate: bool


_INSERT_FACT = """
    INSERT INTO fact_optimization (
        event_id, schema_version, occurred_at, source_app, source_env,
        anon_user_id, zone, memberships, cards,
        status, items_count, units_count,
        stores_used, total_spent_net, total_subtotal, total_delivery_cost,
        total_bank_discount, price_savings_total, excluded_stores,
        suggestions_count, strategic_swaps_count, coto_covered, duration_ms,
        raw_payload
    ) VALUES (
        %(event_id)s, %(schema_version)s, %(occurred_at)s, %(source_app)s, %(source_env)s,
        %(anon_user_id)s, %(zone)s, %(memberships)s, %(cards)s,
        %(status)s, %(items_count)s, %(units_count)s,
        %(stores_used)s, %(total_spent_net)s, %(total_subtotal)s, %(total_delivery_cost)s,
        %(total_bank_discount)s, %(price_savings_total)s, %(excluded_stores)s,
        %(suggestions_count)s, %(strategic_swaps_count)s, %(coto_covered)s, %(duration_ms)s,
        %(raw_payload)s
    )
    -- El corazón de la idempotencia. Un reintento del envío fire-and-forget cae
    -- acá y no devuelve fila: el evento ya estaba y no se duplica nada.
    ON CONFLICT (event_id) DO NOTHING
    RETURNING id
"""

_INSERT_STORE_LINE = """
    INSERT INTO fact_optimization_store (
        optimization_id, store_id, items_count, units_count,
        subtotal_products, delivery_cost, bank_discount_amount, bank_card, store_total
    ) VALUES (
        %(optimization_id)s, %(store_id)s, %(items_count)s, %(units_count)s,
        %(subtotal_products)s, %(delivery_cost)s, %(bank_discount_amount)s, %(bank_card)s, %(store_total)s
    )
"""

# Una tienda nueva en SmartCart (o un id con typo) rompería la FK y tiraría el
# evento entero a la basura. Se la da de alta al vuelo con el id como nombre:
# el dato se salva, y en Power BI se ve un "carrefour_expres" feo que delata que
# falta corregir el display_name a mano. Perder la métrica sería peor.
_ENSURE_STORE = """
    INSERT INTO dim_store (store_id, display_name)
    VALUES (%(store_id)s, %(store_id)s)
    ON CONFLICT (store_id) DO NOTHING
"""


def insert_optimization(
    conn: psycopg.Connection,
    event: IngestEvent,
    raw_payload: dict,
    default_env: str = "dev",
) -> InsertResult:
    """Persiste un evento completo. Header + una fila por tienda participante.

    Corre entero dentro de la transacción de `conn`: o se guarda todo, o nada.
    Un header sin sus filas por tienda sería un carrito con facturación cero en
    el Market Share.
    """
    is_success = event.result.status == "success"

    # --- Derivados del carrito ---
    items_count = len(event.cart.items)
    units_count = sum(item.quantity for item in event.cart.items)

    # --- Derivados del resultado ---
    # En un carrito 'infeasible' el optimizador no devuelve montos. Van a NULL y
    # no a 0: un cero se promediaría como una compra de $0 y hundiría el ticket
    # promedio con carritos que nunca existieron.
    if is_success:
        total_subtotal = sum(s.subtotal_products for s in event.result.split.values())
        total_delivery = sum(s.delivery_cost for s in event.result.split.values())
        total_discount = sum(
            s.bank_discount.amount for s in event.result.split.values() if s.bank_discount
        )
    else:
        total_subtotal = total_delivery = total_discount = None

    params = {
        "event_id": event.event_id,
        "schema_version": event.schema_version,
        "occurred_at": event.occurred_at,
        "source_app": event.source.app,
        "source_env": event.source.env or default_env,
        "anon_user_id": event.user.anon_user_id,
        "zone": event.user.zone,
        "memberships": event.user.memberships,
        "cards": event.user.cards,
        "status": event.result.status,
        "items_count": items_count,
        "units_count": units_count,
        "stores_used": len(event.result.split),
        "total_spent_net": event.result.total_spent_net,
        "total_subtotal": total_subtotal,
        "total_delivery_cost": total_delivery,
        "total_bank_discount": total_discount,
        "price_savings_total": event.result.price_savings_total,
        "excluded_stores": event.result.excluded_stores,
        "suggestions_count": event.engine.suggestions_count,
        "strategic_swaps_count": event.engine.strategic_swaps_count,
        "coto_covered": event.engine.coto_covered,
        "duration_ms": event.engine.duration_ms,
        "raw_payload": Json(raw_payload),
    }

    with conn.cursor() as cur:
        cur.execute(_INSERT_FACT, params)
        row = cur.fetchone()

        if row is None:
            logger.info("Evento %s ya estaba guardado; se descarta el duplicado.", event.event_id)
            return InsertResult(optimization_id=None, duplicate=True)

        optimization_id = row[0]

        if not event.result.split:
            return InsertResult(optimization_id=optimization_id, duplicate=False)

        # Las tiendas se aseguran antes de las líneas: la FK las exige presentes.
        cur.executemany(
            _ENSURE_STORE,
            [{"store_id": store_id} for store_id in event.result.split],
        )

        cur.executemany(
            _INSERT_STORE_LINE,
            [
                {
                    "optimization_id": optimization_id,
                    "store_id": store_id,
                    "items_count": len(split.products),
                    "units_count": sum(p.quantity for p in split.products),
                    "subtotal_products": split.subtotal_products,
                    "delivery_cost": split.delivery_cost,
                    "bank_discount_amount": split.bank_discount.amount if split.bank_discount else 0.0,
                    "bank_card": split.bank_discount.card if split.bank_discount else None,
                    "store_total": split.store_total,
                }
                for store_id, split in event.result.split.items()
            ],
        )

    return InsertResult(optimization_id=optimization_id, duplicate=False)
