# procesamiento.py — Lógica de procesamiento en segundo plano (stub de Etapa 1).
#
# En esta etapa solo registramos que el procesamiento fue "disparado".
# Las etapas 2-7 irán llenando esta función con la lógica real.
#
# Por qué un archivo separado y no todo en webhook.py:
#   - Separación de responsabilidades: el webhook solo recibe y guarda;
#     este módulo orquesta el pipeline completo.
#   - Más fácil de testear: podemos probar el procesamiento sin levantar FastAPI.
#   - El script reprocesar.py (Etapa 7) importará esta misma función.

import logging
from datetime import datetime, timezone

import sqlalchemy as sa

from app.database import engine, leads

# Logger con nombre del módulo para filtrar en los logs
logger = logging.getLogger(__name__)


async def procesar_lead(event_id: str) -> None:
    """
    Punto de entrada del pipeline de procesamiento de un lead.
    Se ejecuta en segundo plano después de que el webhook ya respondió 200.

    En Etapa 1: solo actualiza el timestamp de procesamiento como prueba
    de que el background task se ejecutó.
    Las etapas 2-7 agregarán: validación, deduplicación, enriquecimiento,
    clasificación LLM y enrutado con avisos.

    Args:
        event_id: El identificador del evento a procesar.
    """
    logger.info("Iniciando procesamiento del lead | event_id=%s", event_id)

    try:
        async with engine.begin() as conn:
            # Verificamos que el lead existe y está en estado pendiente.
            # Un lead podría haber cambiado de estado si llegó duplicado.
            fila = await conn.execute(
                sa.select(leads.c.id, leads.c.estado)
                .where(leads.c.event_id == event_id)
            )
            lead_row = fila.fetchone()

            if not lead_row:
                # No debería pasar: lo guardamos justo antes de llamar esto
                logger.error("Lead no encontrado en DB | event_id=%s", event_id)
                return

            if lead_row.estado != "pendiente":
                # Ya fue procesado por otro worker (carrera resuelta por UNIQUE)
                logger.info(
                    "Lead ya procesado, omitiendo | event_id=%s estado=%s",
                    event_id,
                    lead_row.estado,
                )
                return

            # ── Aquí irá la lógica real en etapas 2-7 ────────────────────────
            # Por ahora solo marcamos que el background task corrió.
            # En Etapa 2 esto se reemplaza con validación + normalización.
            logger.info(
                "Lead listo para pipeline completo (stub Etapa 1) | event_id=%s",
                event_id,
            )

    except Exception as exc:
        # En Etapa 7 habrá manejo centralizado de errores con avisos.
        # Por ahora solo logueamos para no perder el error.
        logger.exception(
            "Error inesperado en procesamiento | event_id=%s error=%s",
            event_id,
            str(exc),
        )
