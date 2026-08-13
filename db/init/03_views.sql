-- =============================================================================
-- Vistas de consumo para Power BI
-- =============================================================================
-- Power BI se conecta ÚNICAMENTE a estas vistas, nunca a las tablas. Dos motivos:
--   1. No sabe qué hacer con JSONB ni con TEXT[]; acá se aplanan.
--   2. Las tablas pueden cambiar sin romper el reporte, mientras la vista
--      mantenga sus columnas.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- vw_optimizations — grano: una optimización
-- -----------------------------------------------------------------------------
-- Las partes de fecha vienen calculadas para que el reporte no dependa de DAX de
-- calendario para lo básico. En AR/Buenos Aires: las columnas *_local convierten
-- desde UTC, porque un evento de las 22h de Buenos Aires es del día siguiente en
-- UTC y arruinaría cualquier corte diario.
-- -----------------------------------------------------------------------------
CREATE VIEW vw_optimizations AS
SELECT
    f.id                        AS optimization_id,
    f.event_id,
    f.occurred_at,
    (f.occurred_at AT TIME ZONE 'America/Argentina/Buenos_Aires')            AS occurred_at_local,
    (f.occurred_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::date      AS event_date,
    EXTRACT(YEAR   FROM f.occurred_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::int AS event_year,
    EXTRACT(MONTH  FROM f.occurred_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::int AS event_month,
    EXTRACT(DAY    FROM f.occurred_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::int AS event_day,
    EXTRACT(ISODOW FROM f.occurred_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::int AS day_of_week, -- 1=lunes
    EXTRACT(HOUR   FROM f.occurred_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::int AS event_hour,

    f.source_env,
    f.anon_user_id,
    f.zone,
    -- Los arrays se aplanan a texto: Power BI no tiene tipo lista.
    array_to_string(f.memberships, ', ')     AS memberships,
    array_to_string(f.cards, ', ')           AS cards,
    array_to_string(f.excluded_stores, ', ') AS excluded_stores,
    cardinality(f.excluded_stores) > 0       AS had_exclusions,

    f.status,
    (f.status = 'success')                   AS is_success,
    f.items_count,
    f.units_count,
    f.stores_used,
    -- Un carrito "multi-tienda" es la propuesta de valor del producto: si casi
    -- nadie termina comprando en 2+ supermercados, el optimizador no está
    -- moviendo la aguja. Es un KPI, no una curiosidad.
    (f.stores_used > 1)                      AS is_multi_store,

    f.total_spent_net,
    f.total_subtotal,
    f.total_delivery_cost,
    f.total_bank_discount,
    f.price_savings_total,
    -- Ahorro total percibido = elegir bien la tienda + descuento bancario.
    -- NO incluye el envío (es un costo, no un ahorro) y NO es comparable contra
    -- total_spent_net: price_savings es producto-a-producto contra el peor
    -- precio disponible (ver el docstring de _compute_price_savings en
    -- ../SmartCart/src/api.py).
    COALESCE(f.price_savings_total, 0) + COALESCE(f.total_bank_discount, 0) AS total_savings,

    f.suggestions_count,
    f.strategic_swaps_count,
    f.coto_covered,
    f.duration_ms
FROM fact_optimization f;


-- -----------------------------------------------------------------------------
-- vw_store_participation — grano: una optimización × un supermercado
-- -----------------------------------------------------------------------------
-- Tabla base del Market Share. Cada fila es una participación: la tienda quedó
-- en el split de ese carrito.
-- -----------------------------------------------------------------------------
CREATE VIEW vw_store_participation AS
SELECT
    s.id                        AS participation_id,
    s.optimization_id,
    f.event_id,
    f.occurred_at,
    (f.occurred_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::date AS event_date,
    f.source_env,
    f.anon_user_id,
    f.zone,

    s.store_id,
    d.display_name              AS store_name,
    d.brand_color,
    d.chart_color,

    s.items_count,
    s.units_count,
    s.subtotal_products,
    s.delivery_cost,
    s.bank_discount_amount,
    s.bank_card,
    (s.bank_card IS NOT NULL)   AS used_bank_discount,
    s.store_total,

    -- Constante 1 para que el share por participación sea un SUM más y no exija
    -- un DISTINCTCOUNT en DAX.
    1                           AS participation
FROM fact_optimization_store s
JOIN fact_optimization f ON f.id = s.optimization_id
JOIN dim_store          d ON d.store_id = s.store_id;


-- -----------------------------------------------------------------------------
-- vw_store_share_daily — Market Share, las tres definiciones
-- -----------------------------------------------------------------------------
-- "Market share" es ambiguo y las tres lecturas dan números distintos. Conviven
-- acá, calculadas en SQL, para que el número no dependa de cómo alguien escriba
-- la medida DAX y para poder auditarlo con un SELECT.
--
--   share_revenue_pct       — de cada $100 que se gastan, cuántos van a esta tienda
--   share_units_pct         — de cada 100 unidades compradas, cuántas salen de acá
--   share_participation_pct — en qué % de los carritos aparece esta tienda
--
-- Los dos primeros suman 100% por día. El tercero NO: un carrito repartido entre
-- dos supermercados cuenta para los dos, así que la suma pasa de 100. Es
-- correcto y es justamente lo que mide (penetración, no reparto).
-- -----------------------------------------------------------------------------
CREATE VIEW vw_store_share_daily AS
WITH daily AS (
    SELECT
        (f.occurred_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::date AS event_date,
        f.source_env,
        s.store_id,
        SUM(s.store_total)  AS revenue,
        SUM(s.units_count)  AS units,
        COUNT(*)            AS carts_with_store
    FROM fact_optimization_store s
    JOIN fact_optimization f ON f.id = s.optimization_id
    GROUP BY 1, 2, 3
),
daily_totals AS (
    SELECT
        (f.occurred_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::date AS event_date,
        f.source_env,
        COUNT(*) AS total_carts
    FROM fact_optimization f
    WHERE f.status = 'success'
    GROUP BY 1, 2
)
SELECT
    d.event_date,
    d.source_env,
    d.store_id,
    ds.display_name AS store_name,
    ds.chart_color,

    d.revenue,
    d.units,
    d.carts_with_store,
    t.total_carts,

    ROUND(100.0 * d.revenue / NULLIF(SUM(d.revenue) OVER (PARTITION BY d.event_date, d.source_env), 0), 2) AS share_revenue_pct,
    ROUND(100.0 * d.units   / NULLIF(SUM(d.units)   OVER (PARTITION BY d.event_date, d.source_env), 0), 2) AS share_units_pct,
    ROUND(100.0 * d.carts_with_store / NULLIF(t.total_carts, 0), 2)                                        AS share_participation_pct
FROM daily d
JOIN dim_store ds ON ds.store_id = d.store_id
LEFT JOIN daily_totals t
       ON t.event_date = d.event_date AND t.source_env = d.source_env;


-- -----------------------------------------------------------------------------
-- vw_daily_kpis — grano: un día
-- -----------------------------------------------------------------------------
-- Los KPIs de cabecera del dashboard, ya agregados.
-- -----------------------------------------------------------------------------
CREATE VIEW vw_daily_kpis AS
SELECT
    (occurred_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::date AS event_date,
    source_env,

    -- Volumen de uso
    COUNT(*)                                    AS optimizations,
    COUNT(DISTINCT anon_user_id)                AS unique_users,

    -- Calidad del resultado
    COUNT(*) FILTER (WHERE status = 'success')  AS successful,
    ROUND(100.0 * COUNT(*) FILTER (WHERE status = 'infeasible') / NULLIF(COUNT(*), 0), 2) AS infeasible_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE stores_used > 1)
          / NULLIF(COUNT(*) FILTER (WHERE status = 'success'), 0), 2)                     AS multi_store_pct,
    ROUND(AVG(stores_used) FILTER (WHERE status = 'success'), 2)                          AS avg_stores_per_cart,

    -- Plata. Todas las medias se calculan sólo sobre los carritos exitosos: un
    -- 'infeasible' no tiene monto y arrastraría los promedios hacia abajo.
    ROUND(AVG(total_spent_net)   FILTER (WHERE status = 'success'), 2) AS avg_ticket,
    SUM(total_spent_net)                                              AS total_spent,
    SUM(COALESCE(price_savings_total, 0) + COALESCE(total_bank_discount, 0)) AS total_savings,
    ROUND(AVG(COALESCE(price_savings_total, 0) + COALESCE(total_bank_discount, 0))
          FILTER (WHERE status = 'success'), 2)                       AS avg_savings_per_cart,

    -- Tamaño de canasta y performance
    ROUND(AVG(items_count), 2)  AS avg_items,
    ROUND(AVG(units_count), 2)  AS avg_units,
    ROUND(AVG(duration_ms), 0)  AS avg_duration_ms
FROM fact_optimization
GROUP BY 1, 2;
