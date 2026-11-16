"""Tests del endpoint de ingesta."""

import copy
import uuid
from decimal import Decimal

from tests.conftest import count_store_lines, fetch_header


# --- Camino feliz -------------------------------------------------------------

def test_evento_exitoso_guarda_header_y_una_linea_por_tienda(client, auth_headers, base_event):
    response = client.post("/track-optimization", json=base_event, headers=auth_headers)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "stored"

    header = fetch_header(body["id"])
    assert header["status"] == "success"
    assert header["stores_used"] == 2
    assert count_store_lines(body["id"]) == 2

    # El fixture tiene 3 líneas con cantidades 2+1+3.
    assert header["items_count"] == 3
    assert header["units_count"] == 6

    # Las dimensiones del usuario viajan tal cual.
    assert header["zone"] == "CABA"
    assert header["memberships"] == ["Comunidad Coto"]


def test_los_totales_se_derivan_del_split_y_cierran(client, auth_headers, base_event):
    """El header tiene que ser consistente con la suma de sus tiendas.

    Es la propiedad de la que dependen todos los KPIs de plata: si el subtotal
    del header no coincide con el de las líneas, el ahorro y el ticket promedio
    del dashboard están mal y nadie se entera.
    """
    response = client.post("/track-optimization", json=base_event, headers=auth_headers)
    header = fetch_header(response.json()["id"])

    # 41000.00 + 41810.55
    assert header["total_subtotal"] == Decimal("82810.55")
    # 2000.00 + 3500.00
    assert header["total_delivery_cost"] == Decimal("5500.00")
    # Sólo Día tuvo descuento.
    assert header["total_bank_discount"] == Decimal("4100.00")
    # subtotal + envío - descuento == lo que reportó el optimizador
    assert (
        header["total_subtotal"] + header["total_delivery_cost"] - header["total_bank_discount"]
        == header["total_spent_net"]
    )


# --- Idempotencia -------------------------------------------------------------

def test_reenviar_el_mismo_event_id_no_duplica(client, auth_headers, base_event):
    """El envío desde SmartCart es fire-and-forget y puede reintentar."""
    first = client.post("/track-optimization", json=base_event, headers=auth_headers)
    second = client.post("/track-optimization", json=base_event, headers=auth_headers)

    assert first.status_code == 201
    # 200 y no 4xx: reintentar no es un error del emisor.
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"

    # Y sobre todo: la facturación de las tiendas no se duplicó.
    assert count_store_lines(first.json()["id"]) == 2


# --- Carritos que no alcanzan los mínimos -------------------------------------

def test_infeasible_guarda_header_sin_lineas_y_con_montos_nulos(client, auth_headers, base_event):
    """Un carrito 'infeasible' es un KPI de fricción, no basura a descartar."""
    event = copy.deepcopy(base_event)
    event["result"] = {
        "status": "infeasible",
        "total_spent_net": None,
        "price_savings_total": None,
        "excluded_stores": ["coto_online"],
        "split": {},
    }

    response = client.post("/track-optimization", json=event, headers=auth_headers)
    assert response.status_code == 201

    header = fetch_header(response.json()["id"])
    assert header["status"] == "infeasible"
    assert header["stores_used"] == 0
    assert count_store_lines(response.json()["id"]) == 0

    # NULL y no 0: un cero se promediaría como una compra de $0 y hundiría el
    # ticket promedio con carritos que nunca ocurrieron.
    assert header["total_spent_net"] is None
    assert header["total_subtotal"] is None


# --- Rechazos del contrato ----------------------------------------------------

def test_success_sin_split_es_rechazado(client, auth_headers, base_event):
    event = copy.deepcopy(base_event)
    event["result"]["split"] = {}

    response = client.post("/track-optimization", json=event, headers=auth_headers)
    assert response.status_code == 422


def test_timestamp_sin_zona_horaria_es_rechazado(client, auth_headers, base_event):
    """Sin zona, el emisor (UTC-3) desalinearía todos los cortes diarios."""
    event = copy.deepcopy(base_event)
    event["occurred_at"] = "2026-08-12T18:42:10.512000"

    response = client.post("/track-optimization", json=event, headers=auth_headers)
    assert response.status_code == 422
    assert "zona horaria" in response.text


def test_carrito_vacio_es_rechazado(client, auth_headers, base_event):
    event = copy.deepcopy(base_event)
    event["cart"]["items"] = []

    response = client.post("/track-optimization", json=event, headers=auth_headers)
    assert response.status_code == 422


def test_campos_desconocidos_no_rompen_la_ingesta(client, auth_headers, base_event):
    """SmartCart puede agregar claves sin coordinar un deploy de este servicio."""
    event = copy.deepcopy(base_event)
    event["result"]["split"]["dia_online"]["campo_futuro"] = {"algo": 1}
    event["engine"]["metrica_nueva"] = 99

    response = client.post("/track-optimization", json=event, headers=auth_headers)
    assert response.status_code == 201


# --- Autenticación ------------------------------------------------------------

def test_api_key_invalida(client, base_event):
    response = client.post(
        "/track-optimization", json=base_event, headers={"X-API-Key": "no-es-la-key"}
    )
    assert response.status_code == 401


def test_sin_api_key(client, base_event):
    response = client.post("/track-optimization", json=base_event)
    assert response.status_code == 401


# --- Robustez -----------------------------------------------------------------

def test_tienda_desconocida_se_da_de_alta_en_lugar_de_perder_el_evento(
    client, auth_headers, base_event
):
    """Una tienda nueva en SmartCart no puede costarnos la métrica entera."""
    event = copy.deepcopy(base_event)
    nueva = f"tienda_nueva_{uuid.uuid4().hex[:8]}"
    event["result"]["split"][nueva] = event["result"]["split"].pop("carrefour_online")

    response = client.post("/track-optimization", json=event, headers=auth_headers)
    assert response.status_code == 201
    assert count_store_lines(response.json()["id"]) == 2


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


def test_ruta_vieja_sigue_funcionando_como_alias(client, auth_headers, base_event):
    """`/events/cart-optimized` era la ruta original: un emisor viejo no puede
    quedar en 404 por el cambio de nombre."""
    response = client.post("/events/cart-optimized", json=base_event, headers=auth_headers)
    assert response.status_code == 201
    assert response.json()["status"] == "stored"
