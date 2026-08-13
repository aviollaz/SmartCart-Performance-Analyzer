-- =============================================================================
-- SmartCart Performance Analysis — schema del data warehouse
-- =============================================================================
-- Un evento de negocio: "un usuario optimizó un carrito". Se modela con un
-- header (fact_optimization) y su detalle por supermercado
-- (fact_optimization_store), que es lo que hace posible el Market Share.
--
-- Convenciones:
--   * Plata siempre NUMERIC(12,2), nunca float. Es un DW: los redondeos de
--     punto flotante se acumulan y terminan en un KPI que no cierra.
--   * Tiempos siempre TIMESTAMPTZ. Los eventos llegan en UTC.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- dim_store — catálogo de supermercados
-- -----------------------------------------------------------------------------
-- Existe para que Power BI muestre "Coto" y no "coto_online", y para tener el
-- color de marca en un solo lugar en vez de hardcodeado en cada visual.
-- -----------------------------------------------------------------------------
CREATE TABLE dim_store (
    store_id      TEXT PRIMARY KEY,              -- 'coto_online' (id canónico de SmartCart)
    display_name  TEXT    NOT NULL,              -- 'Coto'
    brand_color   TEXT,                          -- color real de la marca: '#E30613'
    -- Coto y Día usan los dos el mismo rojo. Con el color de marca, un gráfico de
    -- market share queda ilegible justo entre las dos tiendas que más importa
    -- comparar, así que la paleta del reporte vive en su propia columna:
    -- distinguible, y sin perder el color real de la marca de arriba.
    chart_color   TEXT,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE
);


-- -----------------------------------------------------------------------------
-- fact_optimization — un registro por optimización de carrito
-- -----------------------------------------------------------------------------
CREATE TABLE fact_optimization (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- El UNIQUE es el corazón de la idempotencia. El envío desde SmartCart es
    -- fire-and-forget y puede reintentar; sin esta restricción un reintento
    -- duplicaría la facturación de una tienda y ensuciaría el Market Share.
    event_id              UUID        NOT NULL UNIQUE,
    schema_version        SMALLINT    NOT NULL DEFAULT 1,

    -- occurred_at lo pone SmartCart (cuándo pasó), received_at lo pone esta base
    -- (cuándo se guardó). La diferencia delata backlog o relojes desfasados.
    occurred_at           TIMESTAMPTZ NOT NULL,
    received_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_app            TEXT        NOT NULL DEFAULT 'smartcart',
    source_env            TEXT        NOT NULL DEFAULT 'dev',

    -- --- Dimensiones de usuario ---
    -- anon_user_id es un UUID generado en el browser (localStorage), no una
    -- identidad real: SmartCart no tiene autenticación. Nullable porque los
    -- perfiles guardados antes de esta feature no lo tienen.
    anon_user_id          UUID,
    zone                  TEXT,                             -- CABA / GBA Norte / GBA Sur / GBA Oeste
    memberships           TEXT[]      NOT NULL DEFAULT '{}',-- ['Comunidad Coto']
    cards                 TEXT[]      NOT NULL DEFAULT '{}',-- ['Visa Galicia']

    -- --- Medidas del carrito ---
    status                TEXT        NOT NULL
                          CHECK (status IN ('success', 'infeasible')),
    items_count           INT         NOT NULL,             -- líneas distintas
    units_count           INT         NOT NULL,             -- suma de quantity

    -- --- Medidas del resultado ---
    -- Todas nullable: en un carrito 'infeasible' el optimizador no devuelve
    -- montos, y un 0 ahí mentiría (bajaría el ticket promedio con carritos que
    -- nunca se compraron).
    stores_used           SMALLINT    NOT NULL DEFAULT 0,
    total_spent_net       NUMERIC(12,2),
    total_subtotal        NUMERIC(12,2),
    total_delivery_cost   NUMERIC(12,2),
    total_bank_discount   NUMERIC(12,2),
    price_savings_total   NUMERIC(12,2),
    excluded_stores       TEXT[]      NOT NULL DEFAULT '{}',

    -- --- Telemetría del motor ---
    suggestions_count     INT         NOT NULL DEFAULT 0,
    strategic_swaps_count INT         NOT NULL DEFAULT 0,
    coto_covered          BOOLEAN,
    duration_ms           INT,

    -- Red de seguridad: el payload entero, tal cual llegó. Un KPI nuevo se puede
    -- backfillear desde acá sin re-instrumentar SmartCart ni esperar a juntar
    -- datos nuevos durante meses.
    raw_payload           JSONB       NOT NULL
);

CREATE INDEX idx_fact_opt_occurred_at ON fact_optimization (occurred_at DESC);
CREATE INDEX idx_fact_opt_user        ON fact_optimization (anon_user_id);
CREATE INDEX idx_fact_opt_zone        ON fact_optimization (zone);


-- -----------------------------------------------------------------------------
-- fact_optimization_store — un registro por tienda participante
-- -----------------------------------------------------------------------------
-- El grano es (optimización × supermercado). Sólo se insertan las tiendas que
-- efectivamente quedaron en el split: una tienda que el optimizador descartó no
-- genera fila, así que contar filas de una tienda = contar en cuántos carritos
-- participó.
-- -----------------------------------------------------------------------------
CREATE TABLE fact_optimization_store (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    optimization_id       BIGINT NOT NULL
                          REFERENCES fact_optimization(id) ON DELETE CASCADE,
    store_id              TEXT   NOT NULL REFERENCES dim_store(store_id),

    items_count           INT           NOT NULL,
    units_count           INT           NOT NULL,
    subtotal_products     NUMERIC(12,2) NOT NULL,
    delivery_cost         NUMERIC(12,2) NOT NULL DEFAULT 0,
    bank_discount_amount  NUMERIC(12,2) NOT NULL DEFAULT 0,
    bank_card             TEXT,
    store_total           NUMERIC(12,2) NOT NULL,

    -- Una tienda no puede aparecer dos veces en el mismo carrito.
    UNIQUE (optimization_id, store_id)
);

CREATE INDEX idx_fact_opt_store_store ON fact_optimization_store (store_id);
