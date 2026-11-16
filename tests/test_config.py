"""Tests de configuración: no tocan la base."""

import pytest
from fastapi.testclient import TestClient

from src import main
from src.config import Settings


def test_database_url_le_gana_a_las_postgres_sueltas():
    url = "postgresql://u:p@ep-algo.us-east-1.aws.neon.tech/smartcart_analytics?sslmode=require"
    settings = Settings(_env_file=None, database_url=url, postgres_host="otro-host")
    assert settings.dsn == url


def test_sin_database_url_arma_el_dsn_con_las_postgres_sueltas():
    settings = Settings(_env_file=None, postgres_host="h", postgres_port=1234)
    assert settings.dsn.endswith("@h:1234/smartcart_analytics")


def test_no_arranca_en_prod_con_la_key_por_defecto(monkeypatch):
    """La key por defecto es pública (está en el repo): en prod sería no tener auth."""
    insecure = Settings(_env_file=None, environment="prod", analytics_api_key=main.DEFAULT_API_KEY)
    monkeypatch.setattr(main, "get_settings", lambda: insecure)
    with pytest.raises(RuntimeError, match="ANALYTICS_API_KEY"):
        with TestClient(main.app):
            pass
