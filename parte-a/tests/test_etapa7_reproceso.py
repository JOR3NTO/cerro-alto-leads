# test_etapa7_reproceso.py — Tests para la Etapa 7 (Manejo de errores, estado fallido y reproceso).
#
# Casos cubiertos:
#   1. Regla pura es_elegible_para_reproceso (fallido, huérfano, reciente, estados terminales).
#   2. Consulta en DB buscar_leads_reprocesables (detección de huérfanos y filtros).
#   3. Ejecución de reprocesar_lead_por_id (reseteo a pendiente y re-ejecución del pipeline).
#   4. Reprocesamiento en lote (reprocesar_lote).
#   5. Interfaz CLI de reprocesar.py (--dry-run, --event-id, --todos).

import argparse
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa

from app.database import engine, leads
from app.reproceso import (
    buscar_leads_reprocesables,
    es_elegible_para_reproceso,
    reprocesar_lead_por_id,
    reprocesar_lote,
)
from reprocesar import main_async


# ── 1. Pruebas unitarias de la regla pura de elegibilidad ─────────────────────

class TestEsElegibleParaReproceso:
    def test_lead_fallido_siempre_elegible(self):
        """Un lead con estado 'fallido' siempre califica para reintento."""
        elegible, motivo = es_elegible_para_reproceso(
            estado="fallido",
            minutos_antiguedad=1.0,
        )
        assert elegible is True
        assert "fallido" in motivo.lower()

    def test_lead_pendiente_antiguo_es_huerfano(self):
        """Un lead en 'pendiente' con más de 10 min se considera huérfano y se rescata."""
        elegible, motivo = es_elegible_para_reproceso(
            estado="pendiente",
            minutos_antiguedad=15.0,
            min_minutos_huerfano=10.0,
        )
        assert elegible is True
        assert "huerfano" in motivo.lower()

    def test_lead_pendiente_reciente_no_se_toca(self):
        """Un lead recién recibido (< 10 min) aún se está procesando; no se interrumpe."""
        elegible, motivo = es_elegible_para_reproceso(
            estado="pendiente",
            minutos_antiguedad=2.0,
            min_minutos_huerfano=10.0,
        )
        assert elegible is False
        assert "reciente" in motivo.lower()

    def test_estados_terminales_no_se_reprocesan(self):
        """Leads en 'procesado', 'duplicado' o 'rechazado' ya culminaron su ciclo."""
        for est in ("procesado", "duplicado", "rechazado"):
            elegible, motivo = es_elegible_para_reproceso(
                estado=est,
                minutos_antiguedad=60.0,
            )
            assert elegible is False
            assert est in motivo.lower()


# ── 2. Búsqueda y filtrado de leads reprocesables en SQLite ───────────────────

@pytest.mark.asyncio
async def test_buscar_leads_reprocesables_detecta_fallidos_y_huerfanos():
    """Verifica que la consulta en DB clasifique correctamente los candidatos."""
    ahora = datetime.now(timezone.utc)

    payload_base = {
        "event_id": "",
        "event_type": "lead.created",
        "created_at": ahora.isoformat(),
        "lead": {"phone": "0991234567", "email": "a@b.com"},
    }

    async with engine.begin() as conn:
        # 1. Lead fallido
        payload_base["event_id"] = "evt_f1"
        await conn.execute(leads.insert().values(
            event_id="evt_f1",
            estado="fallido",
            motivo="Fallo previo",
            created_at=ahora - timedelta(minutes=5),
            raw_payload=json.dumps(payload_base),
        ))
        # 2. Lead huérfano (pendiente hace 25 min)
        payload_base["event_id"] = "evt_h1"
        await conn.execute(leads.insert().values(
            event_id="evt_h1",
            estado="pendiente",
            created_at=ahora - timedelta(minutes=25),
            raw_payload=json.dumps(payload_base),
        ))
        # 3. Lead pendiente reciente (hace 1 min)
        payload_base["event_id"] = "evt_p1"
        await conn.execute(leads.insert().values(
            event_id="evt_p1",
            estado="pendiente",
            created_at=ahora - timedelta(minutes=1),
            raw_payload=json.dumps(payload_base),
        ))
        # 4. Lead ya procesado
        payload_base["event_id"] = "evt_proc"
        await conn.execute(leads.insert().values(
            event_id="evt_proc",
            estado="procesado",
            created_at=ahora - timedelta(minutes=30),
            raw_payload=json.dumps(payload_base),
        ))

    candidatos = await buscar_leads_reprocesables(
        engine=engine,
        incluir_huerfanos=True,
        min_minutos_huerfano=10.0,
    )

    ids_elegibles = [c["event_id"] for c in candidatos if c["elegible"]]
    assert "evt_f1" in ids_elegibles
    assert "evt_h1" in ids_elegibles
    assert "evt_p1" not in ids_elegibles
    assert "evt_proc" not in ids_elegibles


# ── 3. Ejecución de reprocesamiento individual y en lote ──────────────────────

@pytest.mark.asyncio
async def test_reprocesar_lead_por_id_exitoso():
    """Un lead previamente en estado 'fallido' se reintenta y termina procesado."""
    event_id = "evt_reproceso_001"
    payload = {
        "event_id": event_id,
        "event_type": "lead.created",
        "created_at": "2026-11-03T14:22:05Z",
        "source": {"platform": "instagram", "campaign": "Ortodoncia"},
        "lead": {
            "full_name": "Diana Palacios",
            "phone": "0950001111",
            "email": "diana.p@example.com",
            "service_interest": "Ortodoncia",
            "message": "Quisiera información de costos",
        },
    }

    # Insertamos lead en estado fallido
    async with engine.begin() as conn:
        await conn.execute(
            leads.insert().values(
                event_id=event_id,
                estado="fallido",
                motivo="Error simulado previo",
                raw_payload=json.dumps(payload),
            )
        )

    # Reprocesamos
    exito, msg, datos = await reprocesar_lead_por_id(engine, event_id)

    assert exito is True
    assert datos["estado"] == "procesado"
    assert datos["ruta"] == "tibio"
    assert datos["tiene_borrador"] is True

    # Comprobamos en DB
    async with engine.connect() as conn:
        fila = (await conn.execute(
            sa.select(leads.c.estado, leads.c.ruta).where(leads.c.event_id == event_id)
        )).fetchone()
    assert fila.estado == "procesado"
    assert fila.ruta == "tibio"


@pytest.mark.asyncio
async def test_reprocesar_lote_cuenta_resultados():
    """reprocesar_lote resume correctamente totales, exitosos y fallidos."""
    # Lead válido
    p1 = {
        "event_id": "evt_lote_1",
        "lead": {"phone": "0950002222", "email": "l1@example.com", "message": "info"},
    }
    # Lead con JSON irrecuperable (seguirá fallando)
    async with engine.begin() as conn:
        await conn.execute(leads.insert().values(
            event_id="evt_lote_1",
            estado="fallido",
            raw_payload=json.dumps(p1),
        ))
        await conn.execute(leads.insert().values(
            event_id="evt_lote_2",
            estado="fallido",
            raw_payload="JSON CORRUPTO QUE NUNCA PARSEARA {{{{",
        ))

    res = await reprocesar_lote(engine, ["evt_lote_1", "evt_lote_2"])
    assert res["total"] == 2
    assert res["exitosos"] == 1
    assert res["fallidos"] == 1


# ── 4. Pruebas de la interfaz CLI de reprocesar.py ─────────────────────────────

@pytest.mark.asyncio
async def test_cli_dry_run_no_modifica_datos():
    """El flag --dry-run muestra los leads pero no cambia su estado 'fallido'."""
    event_id = "evt_dry_1"
    payload = {"event_id": event_id, "lead": {"phone": "0991234567", "email": "a@b.com"}}

    async with engine.begin() as conn:
        await conn.execute(leads.insert().values(
            event_id=event_id,
            estado="fallido",
            motivo="Fallo original",
            raw_payload=json.dumps(payload),
        ))

    args = argparse.Namespace(
        event_id=None,
        todos=True,
        limite=10,
        solo_fallidos=False,
        minutos_huerfano=10.0,
        dry_run=True,
    )

    exit_code = await main_async(args)
    assert exit_code == 0

    # Verificamos que sigue en estado fallido intacto
    async with engine.connect() as conn:
        fila = (await conn.execute(
            sa.select(leads.c.estado).where(leads.c.event_id == event_id)
        )).fetchone()
    assert fila.estado == "fallido"


@pytest.mark.asyncio
async def test_cli_reprocesar_event_id_especifico():
    """El flag --event-id reprocesa puntualmente el lead indicado."""
    event_id = "evt_cli_single"
    payload = {
        "event_id": event_id,
        "lead": {"phone": "0950003333", "email": "cli@example.com", "message": "cita"},
    }

    async with engine.begin() as conn:
        await conn.execute(leads.insert().values(
            event_id=event_id,
            estado="fallido",
            motivo="Fallo previo",
            raw_payload=json.dumps(payload),
        ))

    args = argparse.Namespace(
        event_id=event_id,
        todos=False,
        limite=10,
        solo_fallidos=False,
        minutos_huerfano=10.0,
        dry_run=False,
    )

    exit_code = await main_async(args)
    assert exit_code == 0

    async with engine.connect() as conn:
        fila = (await conn.execute(
            sa.select(leads.c.estado).where(leads.c.event_id == event_id)
        )).fetchone()
    assert fila.estado == "procesado"
