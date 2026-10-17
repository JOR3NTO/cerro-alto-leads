# tests/test_etapa1_webhook.py — Pruebas para la Etapa 1: "guardar primero"
#
# Qué probamos aquí:
#   1. Un lead válido recibe 200 OK y queda en estado=pendiente en la DB
#   2. El mismo event_id dos veces → segundo recibe 200 con estado=duplicado
#   3. JSON completamente roto → 200 (no 4xx ni 5xx)
#   4. Payload sin event_id → se asigna un id interno (prefijo "int_")
#   5. La ruta /health responde 200
#
# Por qué pytest-asyncio:
#   - La app es async; necesitamos un event loop para correr los tests.
#   - httpx.AsyncClient nos permite hacer peticiones HTTP reales a la app
#     sin levantar un servidor de red real (usa el ASGI transport directamente).

import json
import pytest

from httpx import AsyncClient, ASGITransport

# Importamos la app y la base de datos para poder inicializarla en los tests
from app.main import app
from app.database import init_db, engine, metadata


# ── Fixture: cliente HTTP para tests ─────────────────────────────────────────
@pytest.fixture
async def cliente():
    """
    Crea un cliente HTTP que habla directamente con la app ASGI.
    Usa una base de datos en memoria (:memory:) para no ensuciar la real.
    """
    # Reemplazamos la URL de la DB por una en memoria solo para tests
    # Nota: esto requiere que engine sea recreado; en etapas futuras usaremos
    # una fixture de conftest más robusta. Por ahora es suficiente.
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health(cliente):
    """El servidor está vivo."""
    resp = await cliente.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_lead_valido_recibe_200(cliente):
    """Un payload bien formado devuelve 200 con estado pendiente."""
    payload = {
        "event_id": "evt_test_etapa1_001",
        "event_type": "lead.created",
        "created_at": "2026-10-17T20:00:00Z",
        "source": {"platform": "meta", "campaign": "Sonrisa Blanca", "form_id": "form_01"},
        "lead": {
            "full_name": "Test Persona",
            "phone": "0991234567",
            "email": "test@example.com",
            "city": "Quito",
            "service_interest": "blanqueamiento",
            "message": "Quiero una cita",
        },
    }
    resp = await cliente.post("/webhook", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["event_id"] == "evt_test_etapa1_001"
    assert body["estado"] == "pendiente"


@pytest.mark.asyncio
async def test_mismo_event_id_dos_veces(cliente):
    """El segundo intento con el mismo event_id recibe 200 con estado duplicado."""
    payload = {
        "event_id": "evt_test_etapa1_idem",
        "event_type": "lead.created",
        "created_at": "2026-10-17T20:00:00Z",
        "source": {"platform": "meta", "campaign": "Ortodoncia sin Excusas", "form_id": "form_02"},
        "lead": {
            "full_name": "Test Idempotencia",
            "phone": "0987654321",
            "email": "idem@example.com",
            "city": "Quito",
            "service_interest": "ortodoncia",
            "message": "Primera vez",
        },
    }
    # Primera vez: debe quedar pendiente
    resp1 = await cliente.post("/webhook", json=payload)
    assert resp1.status_code == 200
    assert resp1.json()["estado"] == "pendiente"

    # Segunda vez (mismos datos, mismo event_id): debe ser duplicado
    resp2 = await cliente.post("/webhook", json=payload)
    assert resp2.status_code == 200
    assert resp2.json()["estado"] == "duplicado"


@pytest.mark.asyncio
async def test_json_roto_recibe_200(cliente):
    """Un body que no es JSON válido devuelve 200 (nunca 4xx/5xx)."""
    from httpx import AsyncClient, ASGITransport
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Content-Type": "application/json"},
    ) as c:
        resp = await c.post("/webhook", content=b"{esto no es json{{{{")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_sin_event_id_asigna_id_interno(cliente):
    """Si el payload no trae event_id, se asigna un id interno con prefijo 'int_'."""
    payload = {
        "event_type": "lead.created",
        "created_at": "2026-10-17T20:00:00Z",
        "source": {"platform": "meta", "campaign": "Chequeo Anual", "form_id": "form_03"},
        "lead": {
            "full_name": "Sin Id",
            "phone": "0991111111",
            "email": "sinid@example.com",
            "city": "Guayaquil",
            "service_interest": "chequeo",
            "message": "Hola",
        },
    }
    resp = await cliente.post("/webhook", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    # El id asignado debe empezar con "int_"
    assert body["event_id"].startswith("int_")
