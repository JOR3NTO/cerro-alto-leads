# estado.py — Endpoint de consulta de estado de leads (GET /leads/estado).
#
# Por qué este endpoint es crítico:
#   - El conector de Meta Ads es asíncrono: recibe 200 OK de inmediato en /webhook
#     y no sabe qué pasó con el lead después.
#   - El equipo de operaciones, auditoría o un dashboard consultan este endpoint
#     para saber si el lead fue procesado, duplicado, rechazado o falló.
#
# Contrato estricto del enunciado:
#   - GET /leads/estado?event_id=evt_...
#       → 200 OK con objeto JSON individual
#       → 404 Not Found si el event_id no existe
#   - GET /leads/estado?desde=2026-11-03T00:00:00Z
#       → 200 OK con lista JSON de eventos recibidos desde esa fecha
#       → 400 Bad Request si 'desde' no es una fecha ISO 8601 válida
#       → Debe incluir eventos con id interno ("int_...") asignados en el webhook
#
# Reglas de campos en la respuesta:
#   - 'ruta': SOLO puede ser 'caliente', 'tibio' o 'basura' cuando estado == 'procesado'.
#     Para cualquier otro estado DEBE ser null (None en Python).
#   - 'email': mapeado desde la columna 'email_normalizado' de la BD.
#   - 'motivo': obligatorio (no null) si estado es duplicado, rechazado, fallido o ruta basura.

import logging
from datetime import datetime, timezone
from typing import Any, Optional, Union

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query

from app.database import engine, leads
from app.deduplicacion import asegurar_utc

logger = logging.getLogger(__name__)

router = APIRouter()


def parsear_fecha_iso(fecha_str: str) -> datetime:
    """
    Parsea una cadena de fecha ISO 8601 y garantiza zona horaria UTC.
    Soporta sufijo 'Z', offsets numéricos (+00:00) y formato ISO estándar.
    Lanza ValueError si el formato no es parseable.
    """
    s = fecha_str.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def formatear_respuesta_lead(fila: Any) -> dict:
    """
    Formatea una fila de la base de datos (dict o SQLAlchemy Row) al contrato
    estricto especificado en las instrucciones técnicas:

    Campos garantizados:
      - event_id: ID original del conector o interno 'int_...'
      - estado: 'procesado' | 'duplicado' | 'rechazado' | 'fallido' | 'pendiente'
      - ruta: 'caliente' | 'tibio' | 'basura' | null (SOLO si estado == 'procesado')
      - lead_id: ID en API de pacientes ('pac_...') o null
      - duplicado_de: event_id original si es duplicado, o null
      - telefono_e164: string en formato E.164 o null
      - email: string normalizado o null (mapeado desde email_normalizado)
      - motivo: string explicativo o null (obligatorio si no es procesado exitoso)
    """
    datos = dict(fila._mapping) if hasattr(fila, "_mapping") else dict(fila)

    estado = datos.get("estado")
    ruta_guardada = datos.get("ruta")

    # Regla del contrato: ruta SOLO existe cuando el lead fue efectivamente procesado
    ruta = ruta_guardada if estado == "procesado" else None

    # Motivo: garantizar que nunca sea nulo en los estados donde es obligatorio
    motivo = datos.get("motivo")
    if not motivo:
        if estado == "duplicado":
            motivo = "Lead duplicado en ventana de 30 dias"
        elif estado == "rechazado":
            motivo = "Sin medios de contacto validos (telefono ni email)"
        elif estado == "fallido":
            motivo = "Error durante el procesamiento del lead"
        elif ruta == "basura":
            motivo = "Clasificado como spam o irrelevante"

    return {
        "event_id": datos.get("event_id"),
        "estado": estado,
        "ruta": ruta,
        "lead_id": datos.get("lead_id"),
        "duplicado_de": datos.get("duplicado_de"),
        "telefono_e164": datos.get("telefono_e164"),
        "email": datos.get("email_normalizado"),
        "motivo": motivo,
    }


@router.get("/leads/estado")
async def consultar_estado_leads(
    event_id: Optional[str] = Query(
        None,
        description="ID del evento para consultar un lead puntual (ej: evt_01)",
    ),
    desde: Optional[str] = Query(
        None,
        description="Filtro ISO 8601 UTC para listar eventos recibidos desde esa fecha",
    ),
    limite: int = Query(
        100,
        ge=1,
        le=500,
        description="Cantidad máxima de registros a retornar en consultas por lista",
    ),
) -> Union[dict, list[dict]]:
    """
    Endpoint para consultar el estado actual del procesamiento de leads.

    Comportamiento según parámetros:
      - ?event_id=... : Retorna el objeto individual o 404 si no existe.
      - ?desde=...    : Retorna lista con los leads creados a partir de esa fecha.
      - Sin filtros   : Retorna lista con los leads más recientes (hasta 'limite').
    """
    # ── 1. Búsqueda individual por event_id ────────────────────────────────────
    if event_id:
        async with engine.connect() as conn:
            query = sa.select(leads).where(leads.c.event_id == event_id)
            fila = (await conn.execute(query)).fetchone()

        if not fila:
            raise HTTPException(
                status_code=404,
                detail=f"Evento '{event_id}' no encontrado",
            )

        return formatear_respuesta_lead(fila)

    # ── 2. Búsqueda por lista con filtro 'desde' ──────────────────────────────
    if desde is not None:
        try:
            fecha_desde_utc = parsear_fecha_iso(desde)
        except Exception as exc:
            logger.warning("Parámetro 'desde' inválido recibido: '%s' (%s)", desde, exc)
            raise HTTPException(
                status_code=400,
                detail="Parámetro 'desde' inválido. Debe ser formato ISO 8601 (ej: 2026-11-03T00:00:00Z)",
            )

        async with engine.connect() as conn:
            query = (
                sa.select(leads)
                .where(leads.c.created_at >= fecha_desde_utc)
                .order_by(leads.c.created_at.asc(), leads.c.id.asc())
                .limit(limite)
            )
            filas = (await conn.execute(query)).fetchall()

        return [formatear_respuesta_lead(f) for f in filas]

    # ── 3. Listar leads recientes sin filtro temporal ─────────────────────────
    async with engine.connect() as conn:
        query = (
            sa.select(leads)
            .order_by(leads.c.created_at.desc(), leads.c.id.desc())
            .limit(limite)
        )
        filas = (await conn.execute(query)).fetchall()

    return [formatear_respuesta_lead(f) for f in filas]
