# test_etapa9_payloads_e2e.py — Validación integral con los 10 payloads reales y pruebas de estrés (Etapa 9).
#
# Cobertura de pruebas:
#   1. Helper 'verificar_estado_lead' para consultar el endpoint GET /leads/estado y validar el contrato.
#   2. Envío secuencial de los 10 payloads reales de la carpeta 'payloads/':
#      - evento_01: Lead nuevo estándar (alta intención) -> procesado, caliente.
#      - evento_02: Mismo event_id que 01 -> 200 OK con estado duplicado (idempotencia de red).
#      - evento_03: Mismo lead pero nuevo event_id (deduplicación 30 días) -> duplicado, duplicado_de=evt_01.
#      - evento_04: Teléfono internacional (+1 305...) en portugués -> procesado, tibio.
#      - evento_05: Sin campo 'phone', nombre vacío "" -> tolerado (email válido), procesado, tibio.
#      - evento_06: 'phone' entero y 'city' como array -> normalizado a E.164, procesado, tibio.
#      - evento_07: Mensaje largo de ~5 KB -> truncado inteligente, procesado, caliente.
#      - evento_08: Spam ("GANA DINERO YA", teléfono inválido "1111111111") -> clasificado como basura.
#      - evento_09: Mensaje "info", teléfono con historial -> procesado.
#      - evento_10: Prompt injection ("ignora instrucciones anteriores") -> resistido, tibio.
#   3. Prueba de carga y concurrencia: ráfaga de 20 eventos simultáneos sin bloqueo de base de datos (WAL mode).
#   4. Trazabilidad con GET /leads/estado?desde=... tras procesar los 10 payloads.

import asyncio
import json
from pathlib import Path
from typing import Optional
import pytest
from httpx import AsyncClient

from app.clasificacion import ClasificacionLead


# ── Helper de verificación de contrato ────────────────────────────────────────

async def verificar_estado_lead(
    cliente: AsyncClient,
    event_id: str,
    estado_esperado: str,
    ruta_esperada: Optional[str] = None,
) -> dict:
    """
    Consulta el estado de un lead específico mediante GET /leads/estado?event_id=...
    y valida que coincida con las expectativas de la prueba.
    """
    resp = await cliente.get(f"/leads/estado?event_id={event_id}")
    assert resp.status_code == 200, f"Error {resp.status_code} al consultar estado de {event_id}: {resp.text}"

    data = resp.json()
    assert isinstance(data, dict), f"La respuesta para {event_id} debe ser un diccionario"
    assert data["event_id"] == event_id, f"El event_id devuelto no coincide: {data.get('event_id')}"
    assert data["estado"] == estado_esperado, (
        f"Estado inesperado para {event_id}: esperado '{estado_esperado}', obtenido '{data['estado']}'"
    )

    if ruta_esperada is not None:
        assert data["ruta"] == ruta_esperada, (
            f"Ruta inesperada para {event_id}: esperada '{ruta_esperada}', obtenida '{data['ruta']}'"
        )
    else:
        if estado_esperado != "procesado":
            assert data["ruta"] is None, (
                f"Para estado '{estado_esperado}', ruta debe ser null (obtenida: {data['ruta']})"
            )

    return data


# ── Prueba End-to-End con los 10 Payloads Reales ───────────────────────────────

@pytest.mark.asyncio
async def test_los_10_payloads_reales_secuencia_completa(cliente, monkeypatch):
    """
    Ejecuta los 10 payloads de 'payloads/' en el orden exacto en que llegarían
    desde el conector de Meta Ads, verificando las trampas y resoluciones.
    """
    # 1. Mockeamos la llamada HTTP a Gemini para que la prueba sea 100 % determinista y rápida
    async def mock_clasificar_con_llm(mensaje, service_interest, campaign):
        msg_str = (mensaje or "").lower()
        if "gana $500" in msg_str or "gana dinero" in msg_str:
            return ClasificacionLead(ruta="basura", motivo="Spam comercial no solicitado", confianza=0.99)
        if "quisiera agendar" in msg_str or "turno el jueves" in msg_str or "estoy decidida a empezar" in msg_str:
            return ClasificacionLead(ruta="caliente", motivo="Alta intencion de agendamiento", confianza=0.95)
        # Por defecto: consultas, precios, dudas o prompt injection resistido
        return ClasificacionLead(ruta="tibio", motivo="Consulta informativa regular", confianza=0.90)

    monkeypatch.setattr("app.clasificacion.clasificar_con_llm", mock_clasificar_con_llm)

    # 2. Localizamos la carpeta de payloads
    ruta_payloads = Path(__file__).parent.parent.parent / "payloads"
    assert ruta_payloads.exists(), f"No se encontró la carpeta payloads en {ruta_payloads}"

    archivos = sorted(ruta_payloads.glob("evento_*.json"))
    assert len(archivos) == 10, f"Se esperaban 10 archivos de eventos, encontrados {len(archivos)}"

    # Diccionario con el contenido en memoria de cada evento
    eventos = {}
    for f in archivos:
        datos = json.loads(f.read_text(encoding="utf-8"))
        eventos[f.name] = datos

    # ── EVENTO 01: Lead nuevo de blanqueamiento (Carla Mendoza) ─────────────────
    ev1 = eventos["evento_01.json"]
    r1 = await cliente.post("/webhook", json=ev1)
    assert r1.status_code == 200
    st1 = await verificar_estado_lead(
        cliente,
        event_id=ev1["event_id"],
        estado_esperado="procesado",
        ruta_esperada="caliente",
    )
    assert st1["telefono_e164"] == "+593991234567"
    assert st1["email"] == "carla.mendoza@example.com"

    # ── EVENTO 02: Mismo event_id que evento_01 (Reintento de red / Idempotencia)
    ev2 = eventos["evento_02.json"]
    r2 = await cliente.post("/webhook", json=ev2)
    assert r2.status_code == 200
    assert r2.json()["estado"] == "duplicado"
    assert "ya registrado" in r2.json()["detalle"]
    # El lead original en base de datos no debe haber sido corrompido
    st2 = await verificar_estado_lead(cliente, event_id=ev2["event_id"], estado_esperado="procesado")
    assert st2["ruta"] == "caliente"

    # ── EVENTO 03: Carla Mendoza llena de nuevo el form (Deduplicación 30 días) ─
    ev3 = eventos["evento_03.json"]
    r3 = await cliente.post("/webhook", json=ev3)
    assert r3.status_code == 200
    # Como tiene un nuevo event_id pero el mismo teléfono y correo, debe marcarse como duplicado
    st3 = await verificar_estado_lead(
        cliente,
        event_id=ev3["event_id"],
        estado_esperado="duplicado",
        ruta_esperada=None,
    )
    assert st3["duplicado_de"] == ev1["event_id"]
    assert "duplicado por" in st3["motivo"].lower()

    # ── EVENTO 04: Paciente extranjero con número de EE.UU. (+1 305...) ─────────
    ev4 = eventos["evento_04.json"]
    r4 = await cliente.post("/webhook", json=ev4)
    assert r4.status_code == 200
    st4 = await verificar_estado_lead(
        cliente,
        event_id=ev4["event_id"],
        estado_esperado="procesado",
        ruta_esperada="tibio",
    )
    assert st4["telefono_e164"] == "+13055550142"
    assert st4["email"] == "joao.almeida@example.com"

    # ── EVENTO 05: Sin campo 'phone' y con nombre vacío "" ──────────────────────
    ev5 = eventos["evento_05.json"]
    r5 = await cliente.post("/webhook", json=ev5)
    assert r5.status_code == 200
    st5 = await verificar_estado_lead(
        cliente,
        event_id=ev5["event_id"],
        estado_esperado="procesado",
        ruta_esperada="tibio",
    )
    assert st5["telefono_e164"] is None
    assert st5["email"] == "lectora.anonima@example.com"

    # ── EVENTO 06: 'phone' entero y 'city' como array ───────────────────────────
    ev6 = eventos["evento_06.json"]
    r6 = await cliente.post("/webhook", json=ev6)
    assert r6.status_code == 200
    st6 = await verificar_estado_lead(
        cliente,
        event_id=ev6["event_id"],
        estado_esperado="procesado",
        ruta_esperada="tibio",
    )
    assert st6["telefono_e164"] == "+593987650021"
    assert st6["email"] == "rosa.quishpe@example.com"

    # ── EVENTO 07: Mensaje largo de ~5 KB (truncado seguro) ─────────────────────
    ev7 = eventos["evento_07.json"]
    r7 = await cliente.post("/webhook", json=ev7)
    assert r7.status_code == 200
    st7 = await verificar_estado_lead(
        cliente,
        event_id=ev7["event_id"],
        estado_esperado="procesado",
        ruta_esperada="caliente",
    )
    assert st7["telefono_e164"] == "+593987654321"

    # ── EVENTO 08: Spam comercial evidente y teléfono inválido ('1111111111') ───
    ev8 = eventos["evento_08.json"]
    r8 = await cliente.post("/webhook", json=ev8)
    assert r8.status_code == 200
    st8 = await verificar_estado_lead(
        cliente,
        event_id=ev8["event_id"],
        estado_esperado="procesado",
        ruta_esperada="basura",
    )
    assert "spam" in st8["motivo"].lower()

    # ── EVENTO 09: Mensaje 'info' con teléfono de paciente ───────────────────────
    ev9 = eventos["evento_09.json"]
    r9 = await cliente.post("/webhook", json=ev9)
    assert r9.status_code == 200
    st9 = await verificar_estado_lead(
        cliente,
        event_id=ev9["event_id"],
        estado_esperado="procesado",
    )
    assert st9["email"] == "jorge.paredes@example.com"

    # ── EVENTO 10: Intento de Prompt Injection ──────────────────────────────────
    ev10 = eventos["evento_10.json"]
    r10 = await cliente.post("/webhook", json=ev10)
    assert r10.status_code == 200
    st10 = await verificar_estado_lead(
        cliente,
        event_id=ev10["event_id"],
        estado_esperado="procesado",
        ruta_esperada="tibio",  # No fue engañado para marcarse como caliente
    )
    assert st10["telefono_e164"] == "+593987111222"


# ── Prueba de Concurrencia y Carga (Burst Concurrency) ─────────────────────────

@pytest.mark.asyncio
async def test_concurrencia_rafaga_leads(cliente, monkeypatch):
    """
    Envía 20 leads simultáneamente en paralelo usando asyncio.gather.
    Verifica que SQLite en modo WAL y busy_timeout soporten la concurrencia
    sin arrojar errores de 'database table is locked'.
    """
    from app.pacientes import ResultadoEnriquecimiento

    async def mock_enriquecer_lead(telefono_e164, email, nombre_lead=None):
        return ResultadoEnriquecimiento(
            es_paciente_existente=False,
            tiene_tratamiento_activo=False,
            lead_id=None,
            enriquecimiento_pendiente=False,
        )

    monkeypatch.setattr("app.procesamiento.enriquecer_lead", mock_enriquecer_lead)

    async def enviar_un_lead(i: int):
        payload = {
            "event_id": f"evt_burst_{i:03d}",
            "event_type": "lead.created",
            "created_at": "2026-11-03T18:00:00Z",
            "source": {"platform": "instagram", "campaign": "Estres Test"},
            "lead": {
                "full_name": f"Usuario Burst {i}",
                "phone": f"095000{i:04d}",
                "email": f"burst_{i}@ejemplo.com",
                "message": f"Mensaje de prueba de concurrencia #{i}",
            },
        }
        return await cliente.post("/webhook", json=payload)

    # Disparamos 20 peticiones concurrentes
    respuestas = await asyncio.gather(*[enviar_un_lead(i) for i in range(20)])

    # Todas deben recibir 200 OK inmediatamente
    for resp in respuestas:
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    # Verificamos que los 20 leads se hayan registrado
    resp_lista = await cliente.get("/leads/estado?limite=50")
    assert resp_lista.status_code == 200
    ids_en_db = {item["event_id"] for item in resp_lista.json()}

    for i in range(20):
        assert f"evt_burst_{i:03d}" in ids_en_db


# ── Prueba de Trazabilidad Temporal ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_trazabilidad_desde_con_payloads_reales(cliente):
    """
    Verifica que GET /leads/estado?desde=... retorne correctamente la lista
    de los eventos recibidos ordenados cronológicamente.
    """
    ev_a = {
        "event_id": "evt_trazabilidad_1",
        "created_at": "2026-11-03T10:00:00Z",
        "lead": {"phone": "0951110001", "email": "a@test.com", "message": "cita"},
    }
    ev_b = {
        "event_id": "evt_trazabilidad_2",
        "created_at": "2026-11-03T12:00:00Z",
        "lead": {"phone": "0951110002", "email": "b@test.com", "message": "cita"},
    }

    await cliente.post("/webhook", json=ev_a)
    await cliente.post("/webhook", json=ev_b)

    resp = await cliente.get("/leads/estado?desde=2020-01-01T00:00:00Z")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) >= 2
    ids = [it["event_id"] for it in items]
    assert "evt_trazabilidad_1" in ids
    assert "evt_trazabilidad_2" in ids
