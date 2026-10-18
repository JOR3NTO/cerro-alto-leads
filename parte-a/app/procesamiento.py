# procesamiento.py — Orquestador del pipeline de procesamiento de leads.
#
# Etapa 2: agrega validación, normalización y rechazo al pipeline.
# Las etapas 3-7 seguirán agregando lógica en el bloque marcado "PIPELINE".
#
# Patrón de este módulo:
#   1. Leer el raw_payload de la DB
#   2. Parsear para obtener los datos del lead
#   3. Validar/normalizar teléfono y correo
#   4. Si ambos inválidos → rechazar con motivo
#   5. Si al menos uno válido → marcar campos normalizados, estado=procesado (por ahora)
#   6. En etapas siguientes: deduplicar, enriquecer, clasificar, enrutar

import json
import logging
from datetime import datetime, timezone

import sqlalchemy as sa

from app.database import engine, leads
from app.deduplicacion import buscar_duplicado_db
from app.modelos import EventoWebhook
from app.normalizacion import validar_y_normalizar
from app.clasificacion import clasificar_lead
from app.pacientes import enriquecer_lead

# Logger con nombre del módulo para filtrar en los logs
logger = logging.getLogger(__name__)


async def procesar_lead(event_id: str) -> None:
    """
    Punto de entrada del pipeline de procesamiento de un lead.
    Se ejecuta en segundo plano después de que el webhook ya respondió 200.

    Etapa 2 agrega: validación y normalización de contacto, rechazo si
    no hay ningún medio de contacto válido.

    Args:
        event_id: El identificador del evento a procesar.
    """
    logger.info("Iniciando procesamiento | event_id=%s", event_id)

    try:
        # ── Leer el lead de la base de datos ─────────────────────────────────
        async with engine.connect() as conn:
            fila = await conn.execute(
                sa.select(
                    leads.c.id,
                    leads.c.estado,
                    leads.c.raw_payload,
                    leads.c.created_at,
                )
                .where(leads.c.event_id == event_id)
            )
            lead_row = fila.fetchone()

        if not lead_row:
            logger.error("Lead no encontrado en DB | event_id=%s", event_id)
            return

        if lead_row.estado != "pendiente":
            # Ya procesado (race condition resuelta por UNIQUE en Etapa 1)
            logger.info(
                "Lead ya procesado, omitiendo | event_id=%s estado=%s",
                event_id, lead_row.estado,
            )
            return

        # ── Parsear el payload original ───────────────────────────────────────
        # Usamos el raw guardado en Etapa 1, no lo que llegó por HTTP,
        # porque el background task no tiene acceso al request original.
        try:
            data = json.loads(lead_row.raw_payload)
            evento = EventoWebhook.model_validate(data)
        except Exception as exc:
            # Si el payload es irrecuperable, marcamos como fallido
            logger.warning("No se pudo reparsear payload | event_id=%s: %s", event_id, exc)
            await _marcar_fallido(event_id, f"Payload irrecuperable: {exc}")
            return

        # Extraer datos del lead (con defaults seguros si vienen None)
        lead_data = evento.lead
        telefono_raw = lead_data.phone if lead_data else None
        email_raw = lead_data.email if lead_data else None

        # ── ETAPA 2: Validación y normalización ───────────────────────────────
        resultado = validar_y_normalizar(telefono_raw, email_raw)

        if resultado.debe_rechazarse:
            # Sin ningún medio de contacto válido → rechazado (no se puede contactar)
            logger.info(
                "Lead rechazado sin contacto válido | event_id=%s", event_id
            )
            await _actualizar_lead(event_id, {
                "estado": "rechazado",
                "motivo": resultado.motivo_rechazo,
                "telefono_e164": None,
                "email_normalizado": None,
                "telefono_invalido": True,
                "email_invalido": True,
                "processed_at": datetime.now(timezone.utc),
            })
            return

        # ── ETAPA 2: Guardar campos normalizados ──────────────────────────────
        logger.info(
            "Lead normalizado | event_id=%s tel=%s email=%s",
            event_id,
            # Enmascaramos el teléfono en logs (cumplimiento LOPDP)
            _enmascarar_tel(resultado.telefono_e164),
            resultado.email_normalizado,
        )

        # Guardamos teléfono y email normalizados en la DB de inmediato.
        # Por qué aquí: si entra otro lead en paralelo de la misma persona,
        # ambos tendrán ya los datos normalizados visibles en DB para cotejarse.
        await _actualizar_lead(event_id, {
            "telefono_e164": resultado.telefono_e164,
            "email_normalizado": resultado.email_normalizado,
            "telefono_invalido": resultado.telefono_invalido,
            "email_invalido": resultado.email_invalido,
        })

        # ── ETAPA 3: Deduplicación por persona (ventana 30 días) ───────────────
        # Si la persona ya registró un lead con el mismo teléfono o correo
        # en los últimos 30 días, se marca como duplicado y se detiene el flujo.
        es_duplicado, event_id_original, campo_match = await buscar_duplicado_db(
            engine=engine,
            event_id_actual=event_id,
            id_actual=lead_row.id,
            created_at_actual=lead_row.created_at,
            telefono_e164=resultado.telefono_e164,
            email=resultado.email_normalizado,
            dias_ventana=30,
        )

        if es_duplicado:
            motivo = (
                f"Lead duplicado por {campo_match} dentro de ventana de 30 dias "
                f"(original: {event_id_original})"
            )
            logger.info(
                "Lead duplicado detectado | event_id=%s duplicado_de=%s motivo=%s",
                event_id, event_id_original, motivo,
            )
            await _actualizar_lead(event_id, {
                "estado": "duplicado",
                "duplicado_de": event_id_original,
                "motivo": motivo,
                "processed_at": datetime.now(timezone.utc),
            })
            return  # Detenemos pipeline: no llamamos a API pacientes, ni LLM, ni avisos

        # ── ETAPA 4: Enriquecimiento con API de Pacientes ──────────────────────
        # Consultamos el CRM externo para saber si ya es paciente registrado y si
        # tiene tratamientos activos o pendientes (clave para clasificarlo como CALIENTE).
        nombre_lead = lead_data.full_name if lead_data else None
        resultado_enriq = await enriquecer_lead(
            telefono_e164=resultado.telefono_e164,
            email=resultado.email_normalizado,
            nombre_lead=nombre_lead,
        )

        logger.info(
            "Enriquecimiento completado | event_id=%s es_paciente=%s activo=%s pendiente=%s lead_id=%s",
            event_id,
            resultado_enriq.es_paciente_existente,
            resultado_enriq.tiene_tratamiento_activo,
            resultado_enriq.enriquecimiento_pendiente,
            resultado_enriq.lead_id,
        )

        await _actualizar_lead(event_id, {
            "es_paciente_existente": resultado_enriq.es_paciente_existente,
            "tiene_tratamiento_activo": resultado_enriq.tiene_tratamiento_activo,
            "lead_id": resultado_enriq.lead_id,
            "enriquecimiento_pendiente": resultado_enriq.enriquecimiento_pendiente,
        })

        # ── ETAPA 5: Clasificación de intención con LLM y reglas duras ────────
        # 1. Tratamiento activo en CRM → caliente
        # 2. Urgencia clínica / dolor dental → caliente
        # 3. LLM (Gemini 1.5 Flash) → caliente / tibio / basura
        # Regla de oro: si el LLM falla o no hay API key, degrada a tibio, nunca a basura.
        mensaje_lead = lead_data.message if lead_data else None
        servicio_interes = lead_data.service_interest if lead_data else None
        campana = evento.source.campaign if evento.source else None

        clasificacion = await clasificar_lead(
            mensaje=mensaje_lead,
            service_interest=servicio_interes,
            campaign=campana,
            tiene_tratamiento_activo=resultado_enriq.tiene_tratamiento_activo,
        )

        logger.info(
            "Clasificacion final | event_id=%s ruta=%s motivo=%s",
            event_id, clasificacion.ruta, clasificacion.motivo,
        )

        # ── Guardar clasificación en base de datos ────────────────────────────
        await _actualizar_lead(event_id, {
            "estado": "procesado",
            "ruta": clasificacion.ruta,
            "motivo": clasificacion.motivo,
            "processed_at": datetime.now(timezone.utc),
        })

    except Exception as exc:
        # Captura cualquier error no esperado y lo registra
        # En Etapa 7 habrá avisos automáticos cuando esto ocurra
        logger.exception(
            "Error inesperado en procesamiento | event_id=%s: %s",
            event_id, str(exc),
        )
        await _marcar_fallido(event_id, f"Error interno: {exc}")


# ── Helpers privados ──────────────────────────────────────────────────────────

async def _actualizar_lead(event_id: str, campos: dict) -> None:
    """Actualiza campos del lead en la base de datos."""
    async with engine.begin() as conn:
        await conn.execute(
            sa.update(leads)
            .where(leads.c.event_id == event_id)
            .values(**campos)
        )


async def _marcar_fallido(event_id: str, motivo: str) -> None:
    """
    Marca el lead como fallido para que el script reprocesar.py lo retome.
    Estado 'fallido' = se aceptó el lead pero algo salió mal en el pipeline.
    """
    await _actualizar_lead(event_id, {
        "estado": "fallido",
        "motivo": motivo,
        "processed_at": datetime.now(timezone.utc),
    })


def _enmascarar_tel(telefono: str | None) -> str:
    """
    Enmascara el teléfono para logs: +593****4567
    Cumple con la regla de privacidad LOPDP del enunciado:
    'en los logs, enmascara el teléfono'.
    """
    if not telefono or len(telefono) < 7:
        return "****"
    # Muestra código de país y últimos 4 dígitos
    return f"{telefono[:4]}****{telefono[-4:]}"
