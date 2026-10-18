# test_etapa3_deduplicacion.py — Pruebas de deduplicación e idempotencia.
#
# Casos a probar:
#   1. Función pura evaluar_duplicado (ventana de 30 días, exclusion de rechazados).
#   2. Deduplicación por teléfono E.164 idéntico con diferente event_id.
#   3. Deduplicación por email normalizado idéntico con diferente event_id.
#   4. Lead fuera de la ventana de 30 días NO se marca como duplicado.
#   5. Lead anterior en estado 'rechazado' NO bloquea al nuevo lead.
#   6. Eventos en paralelo: dos peticiones concurrentes con la misma persona.

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

from app.database import engine, leads
from app.deduplicacion import asegurar_utc, evaluar_duplicado
from app.main import app
from app.procesamiento import procesar_lead


# ── 1. Pruebas unitarias de evaluar_duplicado ──────────────────────────────────

class TestEvaluarDuplicadoPuro:
    def test_mismo_telefono_dentro_de_ventana(self):
        ahora = datetime.now(timezone.utc)
        historial = [
            {
                "event_id": "evt_001",
                "created_at": ahora - timedelta(days=5),
                "telefono_e164": "+593991234567",
                "email_normalizado": "otro@ejemplo.com",
                "estado": "procesado",
            }
        ]
        es_dup, id_orig, campo = evaluar_duplicado(
            telefono_e164="+593991234567",
            email="nuevo@ejemplo.com",
            fecha_actual=ahora,
            historial=historial,
            dias_ventana=30,
        )
        assert es_dup is True
        assert id_orig == "evt_001"
        assert campo == "telefono"

    def test_mismo_email_dentro_de_ventana(self):
        ahora = datetime.now(timezone.utc)
        historial = [
            {
                "event_id": "evt_001",
                "created_at": ahora - timedelta(days=12),
                "telefono_e164": "+593980000000",
                "email_normalizado": "paciente@ejemplo.com",
                "estado": "procesado",
            }
        ]
        es_dup, id_orig, campo = evaluar_duplicado(
            telefono_e164="+593999999999",
            email="PACIENTE@ejemplo.com",
            fecha_actual=ahora,
            historial=historial,
            dias_ventana=30,
        )
        assert es_dup is True
        assert id_orig == "evt_001"
        assert campo == "email"

    def test_fuera_de_ventana_no_es_duplicado(self):
        ahora = datetime.now(timezone.utc)
        # 35 días atrás (ventana es 30 días)
        historial = [
            {
                "event_id": "evt_antiguo",
                "created_at": ahora - timedelta(days=35),
                "telefono_e164": "+593991234567",
                "email_normalizado": "paciente@ejemplo.com",
                "estado": "procesado",
            }
        ]
        es_dup, id_orig, campo = evaluar_duplicado(
            telefono_e164="+593991234567",
            email="paciente@ejemplo.com",
            fecha_actual=ahora,
            historial=historial,
            dias_ventana=30,
        )
        assert es_dup is False
        assert id_orig is None
        assert campo is None

    def test_lead_rechazado_no_bloquea_nuevo_lead(self):
        ahora = datetime.now(timezone.utc)
        historial = [
            {
                "event_id": "evt_rechazado",
                "created_at": ahora - timedelta(days=2),
                "telefono_e164": "+593991234567",
                "email_normalizado": "paciente@ejemplo.com",
                "estado": "rechazado",
            }
        ]
        es_dup, id_orig, campo = evaluar_duplicado(
            telefono_e164="+593991234567",
            email="paciente@ejemplo.com",
            fecha_actual=ahora,
            historial=historial,
            dias_ventana=30,
        )
        assert es_dup is False
        assert id_orig is None

    def test_telefono_o_email_none_no_coinciden_falsamente(self):
        ahora = datetime.now(timezone.utc)
        historial = [
            {
                "event_id": "evt_sin_contacto",
                "created_at": ahora - timedelta(days=1),
                "telefono_e164": None,
                "email_normalizado": None,
                "estado": "procesado",
            }
        ]
        es_dup, id_orig, campo = evaluar_duplicado(
            telefono_e164=None,
            email=None,
            fecha_actual=ahora,
            historial=historial,
            dias_ventana=30,
        )
        assert es_dup is False


# ── 2. Pruebas de integración con base de datos y pipeline ─────────────────────

@pytest.mark.asyncio
async def test_deduplicacion_mismo_telefono_distinto_event_id():
    """
    Simula el caso real evt_01 y evt_03:
    Misma persona con diferente event_id en días distintos.
    El segundo debe quedar en estado 'duplicado' y apuntar al primero.
    """
    # 1. Insertar y procesar primer lead
    payload_1 = {
        "event_id": "evt_persona_01",
        "event_type": "lead.created",
        "lead": {
            "full_name": "Ana García",
            "phone": "0991234567",
            "email": "ana@ejemplo.com",
            "message": "Quiero ortodoncia",
        },
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp1 = await client.post("/webhook", json=payload_1)
        assert resp1.status_code == 200

    # Ejecutar pipeline del primer lead
    await procesar_lead("evt_persona_01")

    # 2. Insertar segundo lead con mismo teléfono pero event_id diferente
    payload_2 = {
        "event_id": "evt_persona_02",
        "event_type": "lead.created",
        "lead": {
            "full_name": "Ana García",
            "phone": "+593 99 123 4567",  # Mismo teléfono con formato distinto
            "email": "otro_correo@ejemplo.com",
            "message": "Sigo interesada en ortodoncia",
        },
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp2 = await client.post("/webhook", json=payload_2)
        assert resp2.status_code == 200

    # Ejecutar pipeline del segundo lead
    await procesar_lead("evt_persona_02")

    # 3. Verificar estado en DB
    async with engine.connect() as conn:
        fila1 = (
            await conn.execute(
                sa.select(leads.c.estado, leads.c.duplicado_de)
                .where(leads.c.event_id == "evt_persona_01")
            )
        ).fetchone()

        fila2 = (
            await conn.execute(
                sa.select(leads.c.estado, leads.c.duplicado_de, leads.c.motivo)
                .where(leads.c.event_id == "evt_persona_02")
            )
        ).fetchone()

    assert fila1.estado == "procesado"
    assert fila2.estado == "duplicado"
    assert fila2.duplicado_de == "evt_persona_01"
    assert "telefono" in fila2.motivo.lower()
    assert "30 dias" in fila2.motivo.lower()


@pytest.mark.asyncio
async def test_deduplicacion_mismo_email_distinto_event_id():
    """
    Lead con distinto teléfono (o sin teléfono) pero con el mismo email normalizado.
    """
    payload_1 = {
        "event_id": "evt_correo_01",
        "lead": {
            "phone": "0991112222",
            "email": "carlos.m@ejemplo.com",
            "message": "Consulta blanqueamiento",
        },
    }
    payload_2 = {
        "event_id": "evt_correo_02",
        "lead": {
            "phone": "0987778888",
            "email": "CARLOS.M@EJEMPLO.COM ",  # Con mayúsculas y espacio
            "message": "Información por favor",
        },
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/webhook", json=payload_1)
        await client.post("/webhook", json=payload_2)

    await procesar_lead("evt_correo_01")
    await procesar_lead("evt_correo_02")

    async with engine.connect() as conn:
        fila2 = (
            await conn.execute(
                sa.select(leads.c.estado, leads.c.duplicado_de, leads.c.motivo)
                .where(leads.c.event_id == "evt_correo_02")
            )
        ).fetchone()

    assert fila2.estado == "duplicado"
    assert fila2.duplicado_de == "evt_correo_01"
    assert "email" in fila2.motivo.lower()


@pytest.mark.asyncio
async def test_lead_fuera_de_30_dias_no_se_duplica_en_db():
    """
    Si el lead original llegó hace 40 días, la misma persona NO debe considerarse
    duplicada porque ya pasó la ventana de seguimiento.
    """
    hace_40_dias = datetime.now(timezone.utc) - timedelta(days=40)

    # Insertamos directamente un lead histórico de hace 40 días
    async with engine.begin() as conn:
        await conn.execute(
            leads.insert().values(
                event_id="evt_historico_antiguo",
                raw_payload="{}",
                telefono_e164="+593998887776",
                email_normalizado="antiguo@ejemplo.com",
                estado="procesado",
                created_at=hace_40_dias,
            )
        )

    # Nuevo lead hoy con el mismo teléfono
    payload_nuevo = {
        "event_id": "evt_hoy_nuevo",
        "lead": {
            "phone": "0998887776",
            "email": "nuevo_email@ejemplo.com",
            "message": "Hola de nuevo, quiero cita",
        },
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/webhook", json=payload_nuevo)

    await procesar_lead("evt_hoy_nuevo")

    async with engine.connect() as conn:
        fila = (
            await conn.execute(
                sa.select(leads.c.estado, leads.c.duplicado_de)
                .where(leads.c.event_id == "evt_hoy_nuevo")
            )
        ).fetchone()

    assert fila.estado == "procesado"
    assert fila.duplicado_de is None


@pytest.mark.asyncio
async def test_carreras_en_paralelo_mismo_contacto():
    """
    Simula 5 peticiones enviadas casi simultáneamente por la misma persona.
    El primero debe procesarse; los demás deben marcarse como duplicados
    o el conector rechazarse si usan el mismo event_id.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 5 eventos concurrentes de la misma persona con distintos event_id
        tareas = [
            client.post(
                "/webhook",
                json={
                    "event_id": f"evt_concurrente_{i}",
                    "lead": {
                        "phone": "0995554443",
                        "email": "concurrente@ejemplo.com",
                        "message": f"Mensaje simultaneo {i}",
                    },
                },
            )
            for i in range(5)
        ]
        respuestas = await asyncio.gather(*tareas)

    # Todas deben recibir 200 sin excepción
    for r in respuestas:
        assert r.status_code == 200

    # Procesar todos los leads
    for i in range(5):
        await procesar_lead(f"evt_concurrente_{i}")

    # Verificar que exactamente 1 sea procesado y los otros 4 sean duplicados
    async with engine.connect() as conn:
        filas = (
            await conn.execute(
                sa.select(leads.c.event_id, leads.c.estado, leads.c.duplicado_de)
                .where(leads.c.event_id.like("evt_concurrente_%"))
            )
        ).fetchall()

    procesados = [f for f in filas if f.estado == "procesado"]
    duplicados = [f for f in filas if f.estado == "duplicado"]

    assert len(procesados) == 1
    assert len(duplicados) == 4
    for dup in duplicados:
        assert dup.duplicado_de == procesados[0].event_id
