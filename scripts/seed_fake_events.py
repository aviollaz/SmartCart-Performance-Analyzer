"""Generador de eventos sintéticos para poder diseñar el dashboard.

Sin datos no se puede diseñar ni validar un solo KPI: Power BI abre una tabla
vacía y no hay forma de saber si el Market Share está bien calculado. Este script
llena el warehouse con eventos que respetan las reglas del negocio real (mínimos
de compra, costos de envío por zona, topes de descuento bancario).

    python scripts/seed_fake_events.py --n 500

IMPORTANTE: los eventos se marcan con `source.env = "seed"` (configurable con
--env). Es a propósito: los datos falsos tienen que ser distinguibles de los
reales en el warehouse, y todas las vistas exponen `source_env` para filtrarlos.
Para borrarlos después:

    DELETE FROM fact_optimization WHERE source_env = 'seed';
"""

import argparse
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx

# --- Constantes calcadas de la app principal ---------------------------------
# Valores reales de ../SmartCart: los mínimos de compra del optimizador y la
# tabla de envíos del frontend (frontend/src/utils/deliveryCosts.js). Si los
# datos sintéticos no los respetan, el dashboard muestra situaciones que el
# optimizador nunca podría producir.

MIN_SPEND = {"coto_online": 15000.0, "dia_online": 12000.0, "carrefour_online": 12000.0}

DELIVERY_COSTS_BY_ZONE = {
    "CABA":       {"coto_online": 2500.0, "dia_online": 2000.0, "carrefour_online": 3500.0},
    "GBA Norte":  {"coto_online": 3500.0, "dia_online": 3000.0, "carrefour_online": 3500.0},
    "GBA Sur":    {"coto_online": 4000.0, "dia_online": 4000.0, "carrefour_online": 3500.0},
    "GBA Oeste":  {"coto_online": 3800.0, "dia_online": 3500.0, "carrefour_online": 3500.0},
}

# La mayor parte del uso es de CABA; el conurbano pesa menos pero existe.
ZONE_WEIGHTS = {"CABA": 0.55, "GBA Norte": 0.2, "GBA Sur": 0.13, "GBA Oeste": 0.12}

STORES = ["coto_online", "dia_online", "carrefour_online"]
# Día suele ganar por precio y Carrefour queda tercero: sesgo intencional para
# que el Market Share del dashboard no dé un empate triple sin información.
STORE_WEIGHTS = [0.34, 0.42, 0.24]

MEMBERSHIPS = ["Comunidad Coto", "Mi Carrefour"]
CARDS = ["Visa Galicia", "Mastercard Santander", "Visa BBVA", "Amex", "Visa Nación"]

# Promo bancaria típica del mercado argentino: un % con tope en pesos.
BANK_PROMO = {"discount_pct": 0.20, "cap": 8000.0}


def weighted_zone(rng: random.Random) -> str:
    return rng.choices(list(ZONE_WEIGHTS), weights=list(ZONE_WEIGHTS.values()))[0]


def random_occurred_at(rng: random.Random, days: int) -> datetime:
    """Timestamp con estacionalidad: pico de tarde/noche y fines de semana flojos."""
    day_offset = rng.randint(0, days - 1)
    day = datetime.now(timezone.utc) - timedelta(days=day_offset)

    # Los sábados y domingos se compra menos online: se descarta parte de esos
    # eventos moviéndolos a un día hábil.
    if day.weekday() >= 5 and rng.random() < 0.45:
        day -= timedelta(days=rng.randint(1, 3))

    # Distribución horaria (local AR): mediodía y after office.
    hour_local = rng.choices(
        [8, 10, 12, 14, 16, 18, 20, 22],
        weights=[3, 6, 10, 8, 9, 14, 16, 9],
    )[0]
    # El evento se emite en UTC; Buenos Aires es UTC-3.
    return day.replace(
        hour=(hour_local + 3) % 24, minute=rng.randint(0, 59), second=rng.randint(0, 59), microsecond=0
    )


def build_cart(rng: random.Random) -> list[dict]:
    n_items = rng.choices([4, 7, 11, 15, 20, 28], weights=[10, 22, 26, 20, 14, 8])[0]
    return [
        {"unified_id": f"u_{rng.randint(100000, 999999)}", "quantity": rng.choices([1, 2, 3, 4], weights=[60, 25, 10, 5])[0]}
        for _ in range(n_items)
    ]


def build_event(rng: random.Random, user_pool: list[str], env: str, days: int) -> dict:
    zone = weighted_zone(rng)
    cart = build_cart(rng)
    occurred_at = random_occurred_at(rng, days)

    memberships = rng.sample(MEMBERSHIPS, k=rng.choices([0, 1], weights=[65, 35])[0])
    cards = rng.sample(CARDS, k=rng.choices([0, 1, 2], weights=[30, 50, 20])[0])

    user = {
        "anon_user_id": rng.choice(user_pool),
        "zone": zone,
        "memberships": memberships,
        "cards": cards,
    }
    base = {
        "event_id": str(uuid.uuid4()),
        "event_type": "cart_optimized",
        "schema_version": 1,
        "occurred_at": occurred_at.isoformat(),
        "source": {"app": "smartcart", "env": env, "version": "1.0.0"},
        "user": user,
        "cart": {"items": cart},
    }

    # ~8% de carritos no alcanzan los mínimos de compra. Es un caso real y un KPI
    # de fricción: si sube, el producto está frustrando gente.
    if rng.random() < 0.08:
        base["result"] = {
            "status": "infeasible",
            "total_spent_net": None,
            "price_savings_total": None,
            "excluded_stores": [],
            "split": {},
        }
        base["engine"] = {
            "suggestions_count": 0,
            "strategic_swaps_count": 0,
            "coto_covered": rng.random() > 0.12,
            "duration_ms": rng.randint(150, 900),
        }
        return base

    # --- Carrito exitoso: repartir las líneas entre 1..3 tiendas ---
    n_stores = rng.choices([1, 2, 3], weights=[33, 52, 15])[0]
    chosen = _pick_stores(rng, n_stores)

    # Coto a veces no llega a la dirección. Cuando pasa NO puede participar del
    # split, sin excepciones: un evento con coto_covered=False y Coto facturando
    # es un dato imposible que después hace desconfiar de todo el dashboard.
    excluded = []
    coto_covered = rng.random() > 0.12
    if not coto_covered:
        excluded = ["coto_online"]
        chosen = [s for s in chosen if s != "coto_online"]
        if not chosen:
            chosen = [rng.choice(["dia_online", "carrefour_online"])]

    lines_by_store: dict[str, list[dict]] = {s: [] for s in chosen}
    for item in cart:
        store = rng.choice(chosen)
        # Precio unitario típico de góndola en pesos (2026).
        unit_price = round(rng.uniform(1200, 14000), 2)
        lines_by_store[store].append(
            {
                "unified_id": item["unified_id"],
                "quantity": item["quantity"],
                "total_cost": round(unit_price * item["quantity"], 2),
            }
        )

    # Una tienda sin líneas no participa; una que no llega a su mínimo tampoco
    # habría sido elegida por el optimizador. Se descartan y sus productos se
    # reasignan a la tienda más grande, que es lo que haría el solver.
    lines_by_store = {s: lines for s, lines in lines_by_store.items() if lines}
    if not lines_by_store:
        lines_by_store = {chosen[0]: [
            {"unified_id": i["unified_id"], "quantity": i["quantity"],
             "total_cost": round(rng.uniform(1200, 14000) * i["quantity"], 2)}
            for i in cart
        ]}

    biggest = max(lines_by_store, key=lambda s: sum(l["total_cost"] for l in lines_by_store[s]))
    for store in list(lines_by_store):
        if store == biggest:
            continue
        if sum(l["total_cost"] for l in lines_by_store[store]) < MIN_SPEND[store]:
            lines_by_store[biggest].extend(lines_by_store.pop(store))

    split = {}
    total_spent_net = 0.0
    total_subtotal = 0.0
    # El descuento bancario se aplica en una sola tienda: es la promo del día de
    # una tarjeta puntual, no un beneficio universal.
    discount_store = rng.choice(list(lines_by_store)) if cards and rng.random() < 0.55 else None

    for store, lines in lines_by_store.items():
        subtotal = round(sum(l["total_cost"] for l in lines), 2)
        delivery = DELIVERY_COSTS_BY_ZONE[zone][store]

        bank_discount = None
        discount_amount = 0.0
        if store == discount_store:
            discount_amount = round(min(subtotal * BANK_PROMO["discount_pct"], BANK_PROMO["cap"]), 2)
            bank_discount = {
                "card": cards[0],
                "amount": discount_amount,
                "description": f"{int(BANK_PROMO['discount_pct'] * 100)}% de descuento, tope ${BANK_PROMO['cap']:.0f}",
            }

        store_total = round(subtotal + delivery - discount_amount, 2)
        total_spent_net += store_total
        total_subtotal += subtotal

        split[store] = {
            "products": lines,
            "subtotal_products": subtotal,
            "delivery_cost": delivery,
            "bank_discount": bank_discount,
            "store_total": store_total,
        }

    base["result"] = {
        "status": "success",
        "total_spent_net": round(total_spent_net, 2),
        # Ahorro contra el peor precio disponible de cada producto: entre 6% y 19%
        # del subtotal, que es el orden de magnitud que devuelve el motor real.
        "price_savings_total": round(total_subtotal * rng.uniform(0.06, 0.19), 2),
        "excluded_stores": excluded,
        "split": split,
    }
    base["engine"] = {
        "suggestions_count": rng.choices([0, 1, 2, 3, 4, 5], weights=[22, 20, 20, 18, 12, 8])[0],
        "strategic_swaps_count": rng.choices([0, 1, 2], weights=[65, 27, 8])[0],
        "coto_covered": coto_covered,
        "coto_delivery_source": "live" if rng.random() < 0.8 else "fallback",
        "duration_ms": rng.randint(300, 2200),
    }
    return base


def _pick_stores(rng: random.Random, n: int) -> list[str]:
    """Elige n tiendas distintas respetando el sesgo de STORE_WEIGHTS."""
    picked: list[str] = []
    while len(picked) < n:
        candidate = rng.choices(STORES, weights=STORE_WEIGHTS)[0]
        if candidate not in picked:
            picked.append(candidate)
    return picked


def main() -> int:
    parser = argparse.ArgumentParser(description="Genera eventos sintéticos contra la API de ingesta.")
    parser.add_argument("--n", type=int, default=500, help="cantidad de eventos (default: 500)")
    parser.add_argument("--days", type=int, default=90, help="ventana hacia atrás en días (default: 90)")
    parser.add_argument("--users", type=int, default=120, help="usuarios anónimos distintos (default: 120)")
    parser.add_argument("--url", default="http://localhost:8001", help="base URL de la API")
    parser.add_argument("--api-key", default="dev-local-key-change-me", help="valor del header X-API-Key")
    parser.add_argument("--env", default="seed", help="source.env de los eventos (default: seed)")
    parser.add_argument("--seed", type=int, default=None, help="semilla para reproducir la misma tanda")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    # Pool fijo de usuarios para que haya recurrencia: sin esto cada evento sería
    # un usuario nuevo y el KPI de usuarios únicos sería igual al de volumen.
    user_pool = [str(uuid.UUID(int=rng.getrandbits(128), version=4)) for _ in range(args.users)]

    stored = duplicates = failed = 0
    with httpx.Client(base_url=args.url, timeout=10.0, headers={"X-API-Key": args.api_key}) as client:
        for i in range(args.n):
            event = build_event(rng, user_pool, args.env, args.days)
            try:
                response = client.post("/events/cart-optimized", json=event)
            except httpx.HTTPError as exc:
                print(f"  error de red: {exc}", file=sys.stderr)
                failed += 1
                continue

            if response.status_code == 201:
                stored += 1
            elif response.status_code == 200:
                duplicates += 1
            else:
                failed += 1
                if failed <= 3:  # las tres primeras alcanzan para diagnosticar
                    print(f"  HTTP {response.status_code}: {response.text[:300]}", file=sys.stderr)

            if (i + 1) % 100 == 0:
                print(f"  {i + 1}/{args.n} enviados...")

    print(f"\nListo. guardados={stored} duplicados={duplicates} fallidos={failed}")
    if stored:
        print(f"Los eventos quedaron con source_env='{args.env}'. Para borrarlos:")
        print(f"  DELETE FROM fact_optimization WHERE source_env = '{args.env}';")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
