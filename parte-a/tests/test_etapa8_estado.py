# test_etapa8_estado.py — Pruebas para el endpoint GET /leads/estado (Etapa 8).
#
# Cobertura de pruebas:
#   1. Formateador puro (formatear_respuesta_lead) y cumplimiento estricto del contrato.
#   2. Regla clave: 'ruta' SOLO tiene valor cuando estado == 'procesado'; null en el resto.
#   3. Mapeo de campos: columna 'email_normalizado' se expone como 'email'.
#   4. Consulta puntual por ?event_id=...: 200 OK con objeto dict.
#   5. Consulta puntual de id inexistente: 404 Not Found.
#   6. Consulta por lista con filtro ?desde=...: 200 OK con lista cronológica.
#   7. Inclusión de eventos con ids internos ('int_...') en filtro 'desde'.
#   8. Validación de fecha 'desde' inválida: 400 Bad Request.
#   9. Consulta sin parámetros: 200 OK con lista de leads recientes.

from datetime import datetime, timezone
import pytest
import sqlalchemy as sa

import app.database as db_module
from app.database import leads
from app.estado import formatear_respuesta_lead, parsear_fecha_iso


# ── 1. Pruebas de la función pura de formateo ─────────────────────────────────

def test_formatear_respuesta_lead_estado_procesado():
    """Cuando estado == 'procesado', la ruta debe mantenerse."""
    fila = {
        "event_id": "evt_100",
        "estado": "procesado",
        "ruta": "caliente",
        "lead_id": "pac_001",
        "duplicado_de": None,
        "telefono_e164": "+593987654321",
        "email_normalizado": "ana@ejemplo.com",
        "motivo": "Urgencia dental detectada",
    }
    resultado = formatear_respuesta_lead(fila)

    assert resultado["event_id"] == "evt_100"
    assert resultado["estado"] == "procesado"
    assert resultado["ruta"] == "caliente"
    assert resultado["lead_id"] == "pac_001"
    assert resultado["email"] == "ana@ejemplo.com"
    assert resultado["telefono_e164"] == "+593987654321"


def test_formatear_respuesta_lead_ruta_debe_ser_null_si_no_es_procesado():
    """
    Regla estricta del contrato:
    Si estado no es 'procesado' (ej: duplicado, rechazado, fallido, pendiente),
    'ruta' DEBE ser None (null en JSON), incluso si la columna en BD tuviese datos residuales.
    """
    for estado_no_procesado in ["duplicado", "rechazado", "fallido", "pendiente"]:
        fila = {
            "event_id": f"evt_{estado_no_procesado}",
            "estado": estado_no_procesado,
            "ruta": "caliente",  # Simula dato residual en BD
            "lead_id": None,
            "duplicado_de": "evt_origen" if estado_no_procesado == "duplicado" else None,
            "telefono_e164": "+593987654321",
            "email_normalizado": "test@ejemplo.com",
            "motivo": "Causa cualquiera",
        }
        res = formatear_respuesta_lead(fila)
        assert res["ruta"] is None, f"Para estado '{estado_no_procesado}', ruta debe ser None"


def test_formatear_respuesta_lead_garantiza_motivo_en_estados_obligatorios():
    """
    El contrato exige motivo obligatorio en 'duplicado', 'rechazado', 'fallido' y ruta 'basura'.
    Si por anomalía viniera vacío en DB, la función debe proveer un fallback informativo.
    """
    for estado in ["duplicado", "rechazado", "fallido"]:
        fila = {"event_id": "evt_x", "estado": estado, "ruta": None, "motivo": None}
        res = formatear_respuesta_lead(fila)
        assert res["motivo"] is not None and len(res["motivo"]) > 0


def test_parsear_fecha_iso():
    """Valida el parseo de diferentes formatos ISO 8601 a datetime UTC."""
    dt_z = parsear_fecha_iso("2026-11-03T15:30:00Z")
    assert dt_z.tzinfo == timezone.utc
    assert dt_z.year == 2026 and dt_z.day == 3 and dt_z.hour == 15

    dt_offset = parsear_fecha_iso("2026-11-03T17:30:00+02:00")
    assert dt_offset.tzinfo == timezone.utc
    assert dt_offset.hour == 15  # 17:30 +02:00 == 15:30 UTC

    with pytest.raises(ValueError):
        parsear_fecha_iso("fecha-no-valida")


# ── 2. Pruebas de integración HTTP con FastAPI ────────────────────────────────

@pytest.mark.asyncio
async def test_get_estado_por_event_id_existente(cliente):
    """GET /leads/estado?event_id=... retorna 200 con el JSON exacto del contrato."""
    # 1. Insertamos un lead de prueba directamente en DB
    async with db_module.engine.begin() as conn:
        await conn.execute(
            sa.insert(leads).values(
                event_id="evt_test_exitoso",
                estado="procesado",
                ruta="caliente",
                lead_id="pac_invers_123",
                telefono_e164="+593987654321",
                email_normalizado="carlos@correo.com",
                motivo="Paciente con tratamiento activo",
                raw_payload="{}",
                created_at=datetime.now(timezone.utc),
            )
        )

    # 2. Consultamos el endpoint
    resp = await cliente.get("/leads/estado?event_id=evt_test_exitoso")
    assert resp.status_code == 200

    data = resp.json()
    assert isinstance(data, dict)
    assert data["event_id"] == "evt_test_exitoso"
    assert data["estado"] == "procesado"
    assert data["ruta"] == "caliente"
    assert data["lead_id"] == "pac_invers_123"
    assert data["telefono_e164"] == "+593987654321"
    assert data["email"] == "carlos@correo.com"
    assert data["motivo"] == "Paciente con tratamiento activo"
    assert data["duplicado_de"] is None


@pytest.mark.asyncio
async def test_get_estado_por_event_id_no_existente_retorna_404(cliente):
    """GET /leads/estado?event_id=... inexistente retorna 404 Not Found."""
    resp = await cliente.get("/leads/estado?event_id=evt_no_existe_999")
    assert resp.status_code == 404
    data = resp.json()
    assert "no encontrado" in data["detail"].lower()


@pytest.mark.asyncio
async def test_get_estado_lead_duplicado_contrato(cliente):
    """GET /leads/estado para lead duplicado incluye duplicado_de y ruta null."""
    async with db_module.engine.begin() as conn:
        await conn.execute(
            sa.insert(leads).values(
                event_id="evt_duplicado_test",
                estado="duplicado",
                ruta=None,
                duplicado_de="evt_original_01",
                telefono_e164="+593987654321",
                email_normalizado="ana@ejemplo.com",
                motivo="Lead duplicado por telefono en ventana de 30 dias",
                raw_payload="{}",
                created_at=datetime.now(timezone.utc),
            )
        )

    resp = await cliente.get("/leads/estado?event_id=evt_duplicado_test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["estado"] == "duplicado"
    assert data["ruta"] is None
    assert data["duplicado_de"] == "evt_original_01"
    assert "duplicado por telefono" in data["motivo"]


@pytest.mark.asyncio
async def test_get_estado_filtro_desde_valido(cliente):
    """
    GET /leads/estado?desde=... retorna una lista de leads creados a partir de esa fecha,
    ordenados cronológicamente.
    """
    # Insertamos 3 leads con distintas fechas
    async with db_module.engine.begin() as conn:
        await conn.execute(
            sa.insert(leads).values([
                {
                    "event_id": "evt_antiguo",
                    "estado": "procesado",
                    "ruta": "tibio",
                    "created_at": datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc),
                    "raw_payload": "{}",
                },
                {
                    "event_id": "evt_medio",
                    "estado": "procesado",
                    "ruta": "caliente",
                    "created_at": datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc),
                    "raw_payload": "{}",
                },
                {
                    "event_id": "evt_nuevo",
                    "estado": "procesado",
                    "ruta": "basura",
                    "created_at": datetime(2026, 11, 4, 8, 0, 0, tzinfo=timezone.utc),
                    "raw_payload": "{}",
                },
            ])
        )

    # Consultamos desde el 3 de noviembre
    resp = await cliente.get("/leads/estado?desde=2026-11-03T00:00:00Z")
    assert resp.status_code == 200

    lista = resp.json()
    assert isinstance(lista, list)
    assert len(lista) == 2
    event_ids = [item["event_id"] for item in lista]
    assert event_ids == ["evt_medio", "evt_nuevo"]


@pytest.mark.asyncio
async def test_get_estado_filtro_desde_incluye_ids_internos(cliente):
    """
    El requerimiento explícito indica que ?desde=... debe incluir eventos a los que se
    les asignó un id interno prefijado con 'int_' (aquellos que llegaron sin event_id).
    """
    async with db_module.engine.begin() as conn:
        await conn.execute(
            sa.insert(leads).values(
                event_id="int_a1b2c3d4e5f67890",
                estado="rechazado",
                motivo="Sin telefono ni correo validos",
                created_at=datetime(2026, 11, 5, 14, 0, 0, tzinfo=timezone.utc),
                raw_payload="{}",
            )
        )
        await conn.execute(
            sa.insert(leads).values(
                event_id="evt_con_id_normal",
                estado="procesado",
                ruta="tibio",
                created_at=datetime(2026, 11, 5, 15, 0, 0, tzinfo=timezone.utc),
                raw_payload="{}",
            )
        )

    resp = await cliente.get("/leads/estado?desde=2026-11-05T00:00:00Z")
    assert resp.status_code == 200

    lista = resp.json()
    assert len(lista) == 2
    ids = [item["event_id"] for item in lista]
    assert "int_a1b2c3d4e5f67890" in ids
    assert "evt_con_id_normal" in ids


@pytest.mark.asyncio
async def test_get_estado_filtro_desde_invalido_retorna_400(cliente):
    """GET /leads/estado con 'desde' no parseable retorna 400 Bad Request."""
    resp = await cliente.get("/leads/estado?desde=cadena_de_texto_invalida")
    assert resp.status_code == 400
    data = resp.json()
    assert "inválido" in data["detail"].lower() or "iso 8601" in data["detail"].lower()


@pytest.mark.asyncio
async def test_get_estado_sin_parametros_retorna_lista(cliente):
    """GET /leads/estado sin query params retorna 200 con la lista de leads existentes."""
    async with db_module.engine.begin() as conn:
        await conn.execute(
            sa.insert(leads).values({
                "event_id": "evt_prueba_sin_params",
                "estado": "pendiente",
                "raw_payload": "{}",
                "created_at": datetime.now(timezone.utc),
            })
        )

    resp = await cliente.get("/leads/estado")
    assert resp.status_code == 200
    lista = resp.json()
    assert isinstance(lista, list)
    assert len(lista) >= 1
    assert any(item["event_id"] == "evt_prueba_sin_params" for item in lista)
