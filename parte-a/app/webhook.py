# webhook.py — Punto de entrada HTTP para los eventos del conector de Meta Ads.
#
# REGLA MÁS IMPORTANTE DE ESTA ETAPA:
#   1. Guardar el raw payload en SQLite (estado=pendiente)
#   2. Responder 200 OK INMEDIATAMENTE
#   3. Disparar el procesamiento en SEGUNDO PLANO
#
# Si hacemos 3 → 2 (procesar antes de responder), el conector ve un timeout
# de 10 s y reintenta 3 veces con el mismo event_id. Con nuestra restricción
# UNIQUE, solo el primero se guarda, pero generamos tráfico y logs sucios.
#
# Por qué BackgroundTasks y no asyncio.create_task:
#   - BackgroundTasks es parte de FastAPI/Starlette: espera a que la tarea
#     termine antes de cerrar la respuesta del worker (evita tareas huérfanas).
#   - asyncio.create_task puede perderse si el servidor se reinicia entre el
#     200 y el inicio real de la tarea.

import json
import logging
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.database import engine, leads
from app.modelos import EventoWebhook
from app.procesamiento import procesar_lead

# Router separado del main.py para poder montarlo en cualquier prefijo
router = APIRouter()

logger = logging.getLogger(__name__)


@router.post("/webhook")
async def recibir_lead(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    """
    Recibe un evento del conector de Meta Ads.

    Flujo:
    ① Leer el body crudo (nunca falla, aunque el JSON esté roto)
    ② Intentar parsear con Pydantic (si falla, guardamos el raw igual)
    ③ Insertar en SQLite con estado=pendiente y UNIQUE en event_id
       - Si el event_id ya existe → IntegrityError → respondemos 200 con
         estado=duplicado (el conector no lo sabe; no reintenta)
    ④ Responder 200 OK con el event_id y estado
    ⑤ Disparar el procesamiento en background (sin esperar)
    """

    # ── ① Leer body crudo ─────────────────────────────────────────────────────
    # Leemos bytes primero para garantizar que SIEMPRE tenemos el dato original,
    # aunque el JSON venga malformado (el conector puede enviar UTF-8 roto).
    try:
        raw_bytes = await request.body()
        raw_str = raw_bytes.decode("utf-8", errors="replace")
    except Exception as exc:
        logger.error("No se pudo leer el body de la petición: %s", exc)
        # Aun así respondemos 200 para no generar reintentos
        return JSONResponse(
            status_code=200,
            content={"ok": False, "detalle": "body ilegible, descartado"},
        )

    # ── ② Parsear con Pydantic ────────────────────────────────────────────────
    evento: EventoWebhook | None = None
    try:
        data = json.loads(raw_str)
        evento = EventoWebhook.model_validate(data)
    except (json.JSONDecodeError, Exception) as exc:
        # JSON roto o estructura inesperada: guardamos el raw con un id interno
        logger.warning("Payload no parseable: %s", exc)

    # Determinar el event_id definitivo
    if evento and evento.event_id:
        event_id = evento.event_id
    else:
        # Asignamos un id interno prefijado para distinguirlos de los del conector
        event_id = f"int_{uuid.uuid4().hex}"
        logger.info(
            "event_id ausente o no parseable; asignado id interno: %s", event_id
        )

    # ── ③ Insertar en SQLite ──────────────────────────────────────────────────
    # La restricción UNIQUE en event_id garantiza atomicidad:
    # si dos peticiones llegan en paralelo con el mismo event_id,
    # solo una INSERT tiene éxito; la otra recibe IntegrityError.
    # No necesitamos un SELECT previo (eso tendría condición de carrera).

    insert_data = {
        "event_id": event_id,
        "estado": "pendiente",
        "raw_payload": raw_str,
        "created_at": datetime.now(timezone.utc),
        "event_created_at": (evento.created_at if evento else None),
        "source_platform": (evento.source.platform if evento and evento.source else None),
        "source_campaign": (evento.source.campaign if evento and evento.source else None),
        "source_form_id": (evento.source.form_id if evento and evento.source else None),
    }

    try:
        async with engine.begin() as conn:
            resultado = await conn.execute(sa.insert(leads).values(**insert_data))
            lead_db_id = resultado.inserted_primary_key[0]

        logger.info(
            "Lead guardado | event_id=%s db_id=%s", event_id, lead_db_id
        )

    except IntegrityError:
        # event_id ya existe → es un reintento del conector (idempotencia)
        # Respondemos 200 para que el conector no vuelva a intentar
        logger.info("event_id duplicado recibido, ignorando | event_id=%s", event_id)
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "event_id": event_id,
                "estado": "duplicado",
                "detalle": "event_id ya registrado",
            },
        )

    except Exception as exc:
        # Error de base de datos inesperado
        logger.exception("Error al guardar en DB | event_id=%s: %s", event_id, exc)
        # Respondemos 200 de todas formas (regla del enunciado)
        return JSONResponse(
            status_code=200,
            content={"ok": False, "event_id": event_id, "detalle": "error interno al guardar"},
        )

    # ── ④ Disparar procesamiento en background ────────────────────────────────
    # El 200 se envía al conector ANTES de que empiece procesar_lead.
    # Esto garantiza que el conector siempre recibe respuesta rápida.
    background_tasks.add_task(procesar_lead, event_id)

    # ── ⑤ Responder 200 OK ────────────────────────────────────────────────────
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "event_id": event_id,
            "estado": "pendiente",
            "detalle": "lead recibido y en proceso",
        },
    )
