# reproceso.py — Lógica de rescate y reprocesamiento de leads fallidos o huérfanos.
#
# Principio de Resiliencia (Etapa 7):
#   - El conector de Meta Ads recibió 200 OK inmediatamente al inicio del webhook.
#   - Meta NUNCA volverá a enviar ese lead si el worker en background se interrumpe
#     por caídas del proceso, reinicios o fallas imprevistas.
#   - Este módulo identifica los leads que quedaron en 'fallido' o 'pendiente' huérfano,
#     restablece su estado de forma segura y los reinyecta en el pipeline procesar_lead.

import logging
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa

from app.database import leads
from app.deduplicacion import asegurar_utc
from app.procesamiento import procesar_lead

logger = logging.getLogger(__name__)


# ── 1. Regla pura de elegibilidad de reproceso ───────────────────────────────

def es_elegible_para_reproceso(
    estado: str,
    minutos_antiguedad: float,
    min_minutos_huerfano: float = 10.0,
) -> tuple[bool, str]:
    """
    Evalúa si un lead específico califica para ser reprocesado.

    Criterios:
      1. 'fallido': Falló explícitamente durante una corrida anterior. Debe reprocesarse.
      2. 'pendiente' antiguo (>= 10 min): Tarea en background que murió antes de terminar.
         (Lead huérfano).
      3. 'pendiente' reciente (< 10 min): Todavía se está ejecutando o encolado; NO tocar.
      4. 'procesado', 'duplicado', 'rechazado': Estados terminales completados; NO tocar.
    """
    estado_limpio = (estado or "").strip().lower()

    if estado_limpio == "fallido":
        return True, "Lead marcado como fallido previamente"

    if estado_limpio == "pendiente":
        if minutos_antiguedad >= min_minutos_huerfano:
            return True, f"Lead huerfano atascado en pendiente ({minutos_antiguedad:.1f} min)"
        return False, f"Lead pendiente reciente ({minutos_antiguedad:.1f} min), en ejecucion"

    return False, f"Estado '{estado}' no requiere reproceso"


# ── 2. Consulta y búsqueda de leads reprocesables en la base de datos ─────────

async def buscar_leads_reprocesables(
    engine,
    event_id: Optional[str] = None,
    incluir_huerfanos: bool = True,
    min_minutos_huerfano: float = 10.0,
    limite: int = 50,
) -> list[dict]:
    """
    Busca en SQLite los leads que requieren reprocesamiento.

    Args:
        engine: AsyncEngine de SQLAlchemy.
        event_id: Si se especifica, busca únicamente ese lead puntual.
        incluir_huerfanos: Si True, incluye leads en 'pendiente' con más de 10 min.
        min_minutos_huerfano: Umbral de minutos para considerar un 'pendiente' como huérfano.
        limite: Máximo de registros a retornar para no sobrecargar el sistema.
    """
    ahora = datetime.now(timezone.utc)

    async with engine.connect() as conn:
        if event_id:
            query = sa.select(
                leads.c.event_id,
                leads.c.estado,
                leads.c.created_at,
                leads.c.processed_at,
                leads.c.motivo,
                leads.c.ruta,
            ).where(leads.c.event_id == event_id)
        else:
            query = (
                sa.select(
                    leads.c.event_id,
                    leads.c.estado,
                    leads.c.created_at,
                    leads.c.processed_at,
                    leads.c.motivo,
                    leads.c.ruta,
                )
                .where(leads.c.estado.in_(["fallido", "pendiente"] if incluir_huerfanos else ["fallido"]))
                .order_by(leads.c.created_at.asc())
                .limit(limite)
            )

        filas = (await conn.execute(query)).fetchall()

    candidatos = []
    for fila in filas:
        dt_creado = asegurar_utc(fila.created_at)
        antiguedad_min = max(0.0, (ahora - dt_creado).total_seconds() / 60.0)

        elegible, razon = es_elegible_para_reproceso(
            estado=fila.estado,
            minutos_antiguedad=antiguedad_min,
            min_minutos_huerfano=min_minutos_huerfano,
        )

        # Si se buscó un event_id específico, lo devolvemos aunque esté procesado para que el CLI informe
        if event_id or elegible:
            candidatos.append({
                "event_id": fila.event_id,
                "estado_actual": fila.estado,
                "ruta_actual": fila.ruta,
                "motivo_actual": fila.motivo,
                "created_at": fila.created_at,
                "antiguedad_min": antiguedad_min,
                "elegible": elegible,
                "razon_elegibilidad": razon,
            })

    return candidatos


# ── 3. Ejecución individual y en lote ─────────────────────────────────────────

async def reprocesar_lead_por_id(engine, event_id: str) -> tuple[bool, str, dict]:
    """
    Reprocesa un lead específico:
      1. Restablece su estado a 'pendiente' en la base de datos.
      2. Invoca la función principal procesar_lead(event_id).
      3. Lee el estado resultante y devuelve el reporte.
    """
    logger.info("Iniciando reproceso manual para event_id=%s", event_id)

    # 1. Restablecemos a pendiente para permitir que procesar_lead lo retome
    async with engine.begin() as conn:
        await conn.execute(
            sa.update(leads)
            .where(leads.c.event_id == event_id)
            .values(
                estado="pendiente",
                motivo="En reproceso manual",
            )
        )

    # 2. Ejecutamos el pipeline estándar
    try:
        await procesar_lead(event_id)
    except Exception as exc:
        logger.exception("Error crítico durante reproceso de event_id=%s: %s", event_id, exc)

    # 3. Consultamos el estado final en DB
    async with engine.connect() as conn:
        res = (await conn.execute(
            sa.select(
                leads.c.estado,
                leads.c.ruta,
                leads.c.motivo,
                leads.c.notificacion_enviada,
                leads.c.borrador_seguimiento,
            )
            .where(leads.c.event_id == event_id)
        )).fetchone()

    if not res:
        return False, "Lead no encontrado tras reproceso", {}

    datos_finales = {
        "estado": res.estado,
        "ruta": res.ruta,
        "motivo": res.motivo,
        "notificacion_enviada": res.notificacion_enviada,
        "tiene_borrador": bool(res.borrador_seguimiento),
    }

    if res.estado in ("procesado", "duplicado", "rechazado"):
        return True, f"Reprocesado exitosamente a estado '{res.estado}'", datos_finales
    else:
        return False, f"Reproceso termino en estado '{res.estado}': {res.motivo}", datos_finales


async def reprocesar_lote(engine, lista_event_ids: list[str]) -> dict:
    """Reprocesa secuencialmente una lista de event_ids y resume los resultados."""
    exitosos = 0
    fallidos = 0
    detalles = []

    for event_id in lista_event_ids:
        exito, mensaje, datos = await reprocesar_lead_por_id(engine, event_id)
        if exito:
            exitosos += 1
        else:
            fallidos += 1
        detalles.append({
            "event_id": event_id,
            "exito": exito,
            "mensaje": mensaje,
            "datos": datos,
        })

    return {
        "total": len(lista_event_ids),
        "exitosos": exitosos,
        "fallidos": fallidos,
        "detalles": detalles,
    }
