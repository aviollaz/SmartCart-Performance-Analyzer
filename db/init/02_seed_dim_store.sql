-- Supermercados soportados hoy por SmartCart. Los store_id son los canónicos del
-- optimizador (../SmartCart/src/optimizer.py) y no se inventan acá: si no
-- coinciden, la FK de fact_optimization_store rechaza el evento.
--
-- brand_color es el color real de cada marca; chart_color es el que usa el
-- reporte. Difieren en Día a propósito: su rojo es casi el de Coto y las dos
-- juntas en un gráfico de market share no se distinguen (ver comentario en
-- 01_schema.sql).
--
-- El ON CONFLICT hace este script re-ejecutable a mano sobre un volumen ya
-- creado. Además, la API inserta las tiendas desconocidas al vuelo
-- (src/repository.py), así que una tienda nueva en SmartCart no tira el ingest:
-- entra con display_name = su id y se corrige después con un UPDATE.
INSERT INTO dim_store (store_id, display_name, brand_color, chart_color) VALUES
    ('coto_online',       'Coto',       '#E30613', '#D64545'),
    ('dia_online',        'Día',        '#E30613', '#E8A33D'),
    ('carrefour_online',  'Carrefour',  '#004E9F', '#3D6FB0')
ON CONFLICT (store_id) DO NOTHING;
