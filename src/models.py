"""Contrato del evento que SmartCart le manda a este servicio.

Es un espejo de la respuesta de `POST /optimize` de la app principal
(../SmartCart/src/api.py) envuelta en metadata de evento. Que sea casi 1:1 es
deliberado: el adapter del lado de SmartCart se limita a reenviar lo que ya
calculó, sin lógica propia que se pueda desincronizar de la real.

Los modelos anidados aceptan campos extra (`extra="allow"`). Si SmartCart agrega
una clave al split, el evento entra igual y el dato queda guardado en
`raw_payload`; sería absurdo perder una métrica entera por un campo nuevo.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BankDiscount(BaseModel):
    """Descuento bancario aplicado en una tienda. `null` si no hubo."""

    model_config = ConfigDict(extra="allow")

    card: str | None = None
    amount: float = 0.0
    description: str | None = None


class SplitProduct(BaseModel):
    """Una línea del carrito, ya asignada a una tienda."""

    model_config = ConfigDict(extra="allow")

    unified_id: str
    quantity: int = 1
    total_cost: float = 0.0


class StoreSplit(BaseModel):
    """Lo que le toca a un supermercado en la asignación óptima."""

    model_config = ConfigDict(extra="allow")

    products: list[SplitProduct] = Field(default_factory=list)
    subtotal_products: float = 0.0
    delivery_cost: float = 0.0
    bank_discount: BankDiscount | None = None
    store_total: float = 0.0


class OptimizationResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: Literal["success", "infeasible"]
    total_spent_net: float | None = None
    price_savings_total: float | None = None
    excluded_stores: list[str] = Field(default_factory=list)
    # Clave = store_id canónico ('coto_online', 'dia_online', 'carrefour_online').
    split: dict[str, StoreSplit] = Field(default_factory=dict)

    @model_validator(mode="after")
    def success_needs_numbers(self) -> "OptimizationResult":
        """Un 'success' sin split ni total no es un éxito: es un bug del emisor.

        Se rechaza en vez de guardarse porque una fila así envenena los KPIs en
        silencio (cuenta como optimización exitosa y baja el ticket promedio),
        y ese tipo de error es carísimo de encontrar tres meses después.
        """
        if self.status == "success":
            if not self.split:
                raise ValueError("un resultado 'success' tiene que traer al menos una tienda en `split`")
            if self.total_spent_net is None:
                raise ValueError("un resultado 'success' tiene que traer `total_spent_net`")
        return self


class CartItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    unified_id: str
    quantity: int = Field(ge=1)


class CartPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    # Obligatorio y no vacío: una optimización sin carrito no existe. Aceptarla
    # sumaría un evento con items_count=0 que arrastra los promedios de canasta.
    items: list[CartItem] = Field(min_length=1)


class EventUser(BaseModel):
    model_config = ConfigDict(extra="allow")

    # UUID anónimo generado en el browser. SmartCart no tiene autenticación, así
    # que esto identifica un navegador, no una persona. Nullable porque los
    # perfiles de localStorage anteriores a la feature no lo traen.
    anon_user_id: UUID | None = None
    zone: str | None = None
    memberships: list[str] = Field(default_factory=list)
    cards: list[str] = Field(default_factory=list)


class EventSource(BaseModel):
    model_config = ConfigDict(extra="allow")

    app: str = "smartcart"
    # None = "usá el ENVIRONMENT de este servicio" (ver repository.py).
    env: str | None = None
    version: str | None = None


class EngineTelemetry(BaseModel):
    """Cómo se portó el motor. No es plata, pero explica los números de arriba."""

    model_config = ConfigDict(extra="allow")

    suggestions_count: int = 0
    strategic_swaps_count: int = 0
    coto_covered: bool | None = None
    coto_delivery_source: str | None = None
    duration_ms: int | None = None


class IngestEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    # Clave de idempotencia: la genera SmartCart y es UNIQUE en la base. Un
    # reintento del envío fire-and-forget vuelve con el mismo event_id y se
    # descarta en vez de duplicar la facturación de las tiendas.
    event_id: UUID
    event_type: Literal["cart_optimized"] = "cart_optimized"
    schema_version: int = 1
    occurred_at: datetime

    source: EventSource = Field(default_factory=EventSource)
    user: EventUser = Field(default_factory=EventUser)
    cart: CartPayload
    result: OptimizationResult
    engine: EngineTelemetry = Field(default_factory=EngineTelemetry)

    @field_validator("occurred_at")
    @classmethod
    def must_be_timezone_aware(cls, v: datetime) -> datetime:
        """Se rechaza un timestamp sin zona horaria.

        Interpretarlo como UTC sería el default cómodo, pero el emisor corre en
        Buenos Aires (UTC-3): cada evento se correría tres horas y los cortes
        diarios del dashboard quedarían mal sin que nadie lo note. Preferimos
        que la integración falle fuerte y temprano, con este mensaje.
        """
        if v.tzinfo is None:
            raise ValueError(
                "`occurred_at` tiene que incluir zona horaria (ej. "
                "datetime.now(timezone.utc).isoformat()). Un timestamp sin zona "
                "es ambiguo y desalinea los KPIs diarios."
            )
        return v
