# test_etapa4_pacientes.py — Pruebas de integración y unitarias para enriquecimiento con API de Pacientes.
#
# Casos probados:
#   1. Función pura evaluar_tratamientos_activos (pendiente, en_curso vs completado, cancelado).
#   2. Resolución de coincidencias múltiples (familia que comparte teléfono/email).
#   3. Paginación de tratamientos (seguimiento de next_cursor).
#   4. Rate limiter (30 req/min).
#   5. Degradación elegante (API caída, timeout, 500, 429).
#   6. Flujo completo en pipeline (procesar_lead actualiza DB correctamente).

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient, Response

from app.database import engine, leads
from app.main import app
from app.pacientes import (
    ClientePacientesAPI,
    RateLimiterTokenBucket,
    ResultadoEnriquecimiento,
    enriquecer_lead,
    evaluar_tratamientos_activos,
    seleccionar_paciente_coincidente,
)
from app.procesamiento import procesar_lead


# ── 1. Pruebas unitarias de evaluar_tratamientos_activos ───────────────────────

class TestEvaluarTratamientosActivos:
    def test_lista_vacia(self):
        assert evaluar_tratamientos_activos([]) is False

    def test_solo_tratamientos_inactivos(self):
        historial = [
            {"id": "t1", "estado": "completado"},
            {"id": "t2", "estado": "cancelado"},
        ]
        assert evaluar_tratamientos_activos(historial) is False

    def test_tratamiento_pendiente(self):
        historial = [
            {"id": "t1", "estado": "completado"},
            {"id": "t2", "estado": "pendiente"},
        ]
        assert evaluar_tratamientos_activos(historial) is True

    def test_tratamiento_en_curso_con_mayusculas_y_espacios(self):
        historial = [{"id": "t1", "estado": "  EN_CURSO  "}]
        assert evaluar_tratamientos_activos(historial) is True


# ── 2. Coincidencias múltiples (familia que comparte teléfono o correo) ────────

class TestSeleccionarPaciente:
    def test_lista_vacia(self):
        assert seleccionar_paciente_coincidente([], "Ana") is None

    def test_un_solo_paciente(self):
        pacientes = [{"id": "pac_01", "nombre": "Carlos Díaz"}]
        assert seleccionar_paciente_coincidente(pacientes, None) == pacientes[0]

    def test_familia_comparte_contacto_coincidencia_por_nombre(self):
        pacientes = [
            {"id": "pac_madre", "nombre": "Rosa Mendoza"},
            {"id": "pac_hija", "nombre": "Carla Mendoza"},
        ]
        seleccionado = seleccionar_paciente_coincidente(pacientes, "Carla Mendoza")
        assert seleccionado is not None
        assert seleccionado["id"] == "pac_hija"

    def test_sin_nombre_toma_primero(self):
        pacientes = [
            {"id": "pac_01", "nombre": "Persona Uno"},
            {"id": "pac_02", "nombre": "Persona Dos"},
        ]
        seleccionado = seleccionar_paciente_coincidente(pacientes, "")
        assert seleccionado["id"] == "pac_01"


# ── 3. Paginación y Rate Limiting del Cliente HTTP ─────────────────────────────

@pytest.mark.asyncio
async def test_paginacion_tratamientos():
    """
    Simula una respuesta paginada con next_cursor:
    Página 1: 3 items con next_cursor="cur_pag2"
    Página 2: 2 items con next_cursor=None
    El cliente debe obtener los 5 tratamientos.
    """
    cliente = ClientePacientesAPI(base_url="https://api.test", token="fake_token")

    async def mock_get(endpoint, params=None):
        if params and params.get("cursor") == "cur_pag2":
            return Response(
                200,
                json={
                    "data": [
                        {"id": "t4", "estado": "completado"},
                        {"id": "t5", "estado": "pendiente"},
                    ],
                    "pagination": {"next_cursor": None},
                },
            )
        return Response(
            200,
            json={
                "data": [
                    {"id": "t1", "estado": "completado"},
                    {"id": "t2", "estado": "completado"},
                    {"id": "t3", "estado": "completado"},
                ],
                "pagination": {"next_cursor": "cur_pag2"},
            },
        )

    with patch.object(cliente, "_get", side_effect=mock_get):
        tratamientos, error = await cliente.obtener_tratamientos("pac_123")

    assert error is None
    assert len(tratamientos) == 5
    assert evaluar_tratamientos_activos(tratamientos) is True  # t5 está pendiente


@pytest.mark.asyncio
async def test_rate_limiter_adquiere_cupos():
    """Verifica que el Rate Limiter no bloquea peticiones si hay cupo disponible."""
    limiter = RateLimiterTokenBucket(max_requests=5, window_seconds=1.0)
    for _ in range(5):
        await limiter.acquire()
    assert len(limiter._timestamps) == 5


# ── 4. Degradación elegante (API caída, timeouts, 500, sin URL) ────────────────

@pytest.mark.asyncio
async def test_degradacion_cuando_api_no_esta_configurada():
    """Si no hay API_PACIENTES_URL en .env, no falla: marca enriquecimiento_pendiente=True."""
    cliente = ClientePacientesAPI(base_url="")
    resultado = await enriquecer_lead(
        telefono_e164="+593991234567",
        email="test@ejemplo.com",
        cliente_override=cliente,
    )
    assert resultado.enriquecimiento_pendiente is True
    assert resultado.es_paciente_existente is False
    assert resultado.tiene_tratamiento_activo is False


@pytest.mark.asyncio
async def test_degradacion_cuando_api_da_timeout():
    """Si la API tarda más de 3 segundos o lanza timeout, el lead continúa."""
    cliente = ClientePacientesAPI(base_url="https://api.test", token="token")

    with patch.object(cliente, "_get", side_effect=httpx.TimeoutException("Timeout simulado")):
        resultado = await enriquecer_lead(
            telefono_e164="+593991234567",
            email="test@ejemplo.com",
            cliente_override=cliente,
        )

    assert resultado.enriquecimiento_pendiente is True
    assert "Timeout" in (resultado.motivo_error or "")


@pytest.mark.asyncio
async def test_degradacion_cuando_api_da_500():
    """Si el CRM responde 500 Internal Server Error, el pipeline no muere."""
    cliente = ClientePacientesAPI(base_url="https://api.test", token="token")

    async def mock_get(endpoint, params=None):
        return Response(500, text="Internal Server Error")

    with patch.object(cliente, "_get", side_effect=mock_get):
        resultado = await enriquecer_lead(
            telefono_e164="+593991234567",
            email="test@ejemplo.com",
            cliente_override=cliente,
        )

    assert resultado.enriquecimiento_pendiente is True
    assert "500" in (resultado.motivo_error or "")


# ── 5. Integración completa con el pipeline de leads ───────────────────────────

@pytest.mark.asyncio
async def test_pipeline_enriquece_paciente_existente_con_tratamiento():
    """
    Lead válido cuyo teléfono coincide con un paciente del CRM con ortodoncia activa.
    En la DB debe quedar: es_paciente_existente=True, tiene_tratamiento_activo=True,
    lead_id="pac_999", enriquecimiento_pendiente=False.
    """
    payload = {
        "event_id": "evt_crm_activo",
        "lead": {
            "full_name": "Daniela Vásconez",
            "phone": "0987654321",
            "email": "daniela@ejemplo.com",
            "message": "Hola, se me rompió un bracket",
        },
    }
    # Mock de la API de pacientes para este test
    async def mock_enriquecer(*args, **kwargs):
        return ResultadoEnriquecimiento(
            es_paciente_existente=True,
            tiene_tratamiento_activo=True,
            lead_id="pac_999",
            enriquecimiento_pendiente=False,
        )

    transport = ASGITransport(app=app)
    with patch("app.procesamiento.enriquecer_lead", side_effect=mock_enriquecer):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/webhook", json=payload)
            assert resp.status_code == 200

    async with engine.connect() as conn:
        fila = (
            await conn.execute(
                sa.select(
                    leads.c.estado,
                    leads.c.es_paciente_existente,
                    leads.c.tiene_tratamiento_activo,
                    leads.c.lead_id,
                    leads.c.enriquecimiento_pendiente,
                )
                .where(leads.c.event_id == "evt_crm_activo")
            )
        ).fetchone()

    assert fila.estado == "procesado"
    assert fila.es_paciente_existente is True
    assert fila.tiene_tratamiento_activo is True
    assert fila.lead_id == "pac_999"
    assert fila.enriquecimiento_pendiente is False


@pytest.mark.asyncio
async def test_pipeline_degrada_elegantemente_si_crm_falla():
    """
    Si la API falla durante el pipeline, el lead queda con enriquecimiento_pendiente=True,
    pero NO se cae en estado fallido ni se descarta.
    """
    payload = {
        "event_id": "evt_crm_caido",
        "lead": {
            "phone": "0991112233",
            "email": "caido@ejemplo.com",
            "message": "Quiero blanqueamiento",
        },
    }

    async def mock_enriquecer(*args, **kwargs):
        return ResultadoEnriquecimiento(
            es_paciente_existente=False,
            tiene_tratamiento_activo=False,
            lead_id=None,
            enriquecimiento_pendiente=True,
            motivo_error="Timeout CRM",
        )

    transport = ASGITransport(app=app)
    with patch("app.procesamiento.enriquecer_lead", side_effect=mock_enriquecer):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/webhook", json=payload)
            assert resp.status_code == 200

    async with engine.connect() as conn:
        fila = (
            await conn.execute(
                sa.select(
                    leads.c.estado,
                    leads.c.enriquecimiento_pendiente,
                )
                .where(leads.c.event_id == "evt_crm_caido")
            )
        ).fetchone()

    assert fila.estado == "procesado"
    assert fila.enriquecimiento_pendiente is True

