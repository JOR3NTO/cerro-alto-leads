# enrutado.py — Enrutamiento operativo y avisos a recepción según la ruta del lead.
#
# Reglas de negocio (Etapa 6):
#   1. "caliente":
#      - SLA crítico: Notificar a recepción de inmediato (< 5 minutos).
#      - Canal: Telegram bot (prioritario) o Email (soporte fallback).
#      - La alerta incluye teléfono E.164 sin enmascarar (recepción debe llamar ya),
#        contexto del CRM (si es paciente y si tiene tratamiento activo) y motivo.
#   2. "tibio":
#      - Genera un borrador personalizado de mensaje de seguimiento en la base de datos.
#      - NO se envía automáticamente al paciente para evitar spam o respuestas insensibles.
#      - Queda listo para que el equipo comercial o recepción lo revise, ajuste y envíe.
#   3. "basura":
#      - Descarte silencioso. No envía alertas a recepción ni genera borradores molestos.
#      - Persiste el motivo en la base de datos para trazabilidad y auditoría.

import asyncio
import logging
import smtplib
from email.mime.text import MIMEText
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


# ── 1. Generador de borrador de seguimiento para leads tibios ─────────────────

def generar_borrador_seguimiento(
    nombre: str | None,
    servicio: str | None,
    mensaje_original: str | None,
) -> str:
    """
    Genera una propuesta de respuesta personalizada para que recepción contacte al lead tibio.

    Por qué un borrador y no envío automático:
    - Los leads tibios suelen tener dudas variadas (precios, tarjetas, horarios, ubicación).
    - Un bot enviando respuestas genéricas automáticas suele sentirse frío y poco profesional.
    - El borrador ahorra el 80 % del tiempo de redacción al asesor, pero mantiene el criterio humano.
    """
    nombre_limpio = nombre.strip().title() if nombre and nombre.strip() else None
    saludo = f"Hola {nombre_limpio}," if nombre_limpio else "Hola,"

    servicio_limpio = servicio.strip() if servicio and servicio.strip() else None
    mencion_servicio = (
        f"sobre nuestro servicio de {servicio_limpio}"
        if servicio_limpio
        else "sobre nuestros tratamientos odontológicos"
    )

    borrador = (
        f"{saludo} gracias por comunicarte con la Clínica Dental Cerro Alto.\n\n"
        f"Vimos tu consulta {mencion_servicio}. Con gusto podemos brindarte toda la "
        f"información sobre valores, facilidades de pago (incluyendo tarjetas de crédito) "
        f"y los horarios disponibles de nuestros especialistas.\n\n"
        f"¿Te gustaría que te agendemos una cita de valoración esta semana o tienes alguna "
        f"duda específica en la que podamos orientarte?\n\n"
        f"— Equipo de Atención al Paciente · Clínica Dental Cerro Alto"
    )
    return borrador


# ── 2. Formateador de alertas a recepción (< 5 min SLA) ───────────────────────

def formatear_alerta_recepcion(
    event_id: str,
    nombre: str | None,
    telefono_e164: str | None,
    email: str | None,
    servicio: str | None,
    campana: str | None,
    plataforma: str | None,
    mensaje: str | None,
    es_paciente_existente: bool | None,
    tiene_tratamiento_activo: bool | None,
    lead_id: str | None,
    motivo: str | None,
) -> str:
    """
    Formatea el mensaje de notificación para el equipo de recepción.

    LOPDP vs Operatividad:
    - En logs enmascaramos el teléfono (+593****4567).
    - Pero en la alerta interna para la recepcionista SÍ va el teléfono completo E.164,
      porque el personal de la clínica tiene autorización y el objetivo del SLA < 5 min
      es llamarlo al instante.
    """
    paciente_txt = "SÍ (Registrado)" if es_paciente_existente else "NO (Lead nuevo)"
    tratamiento_txt = "SÍ (¡Atención prioritaria!)" if tiene_tratamiento_activo else "Ninguno reportado"

    lineas = [
        "🚨 *NUEVO LEAD CALIENTE — CLÍNICA CERRO ALTO* 🚨",
        "⏱ *SLA: Llamar en menos de 5 minutos*",
        "",
        f"👤 *Paciente:* {nombre or 'No especificado'}",
        f"📞 *Teléfono:* {telefono_e164 or 'No disponible'}",
        f"✉️ *Correo:* {email or 'No disponible'}",
        f"🦷 *Interés:* {servicio or 'General'}",
        f"📢 *Campaña:* {campana or 'Directo'} ({plataforma or 'web'})",
        "",
        "💬 *Mensaje del paciente:*",
        f"> {mensaje or '(Sin mensaje de texto)'}",
        "",
        "📋 *Historial CRM:*",
        f"• ¿Es paciente?: {paciente_txt}",
        f"• Tratamiento activo: {tratamiento_txt}",
        f"• ID CRM: {lead_id or 'N/A'}",
        "",
        f"⚡ *Motivo de clasificación:* {motivo or 'Alta intención detectada'}",
        f"🆔 *Event ID:* `{event_id}`",
    ]
    return "\n".join(lineas)


# ── 3. Canales de notificación: Telegram y Email ─────────────────────────────

async def enviar_telegram(texto: str) -> bool:
    """
    Envía la alerta al canal de recepción en Telegram.
    Si el bot token o chat ID no están configurados, registra una simulación limpia
    sin fallar el pipeline.
    """
    token = settings.TELEGRAM_BOT_TOKEN.strip() if settings.TELEGRAM_BOT_TOKEN else ""
    chat_id = settings.TELEGRAM_CHAT_ID.strip() if settings.TELEGRAM_CHAT_ID else ""

    if not token or not chat_id:
        logger.info(
            "Aviso Telegram simulado (TELEGRAM_BOT_TOKEN o CHAT_ID no configurados):\n%s",
            texto,
        )
        return True

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": "Markdown",
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            logger.info("Notificación Telegram enviada exitosamente a chat_id=%s", chat_id)
            return True
    except Exception as exc:
        logger.error("Error al enviar alerta por Telegram: %s", exc)
        return False


def _enviar_email_sync(asunto: str, cuerpo: str) -> bool:
    """Envío de correo SMTP usando la librería estándar (ejecutado en un hilo aparte)."""
    user = settings.EMAIL_USER.strip() if settings.EMAIL_USER else ""
    password = settings.EMAIL_PASSWORD.strip() if settings.EMAIL_PASSWORD else ""
    destino = settings.EMAIL_DESTINO.strip() if settings.EMAIL_DESTINO else ""

    if not user or not password or not destino:
        logger.info("Aviso Email simulado (credenciales no configuradas) | Asunto: %s", asunto)
        return True

    try:
        msg = MIMEText(cuerpo, "plain", "utf-8")
        msg["Subject"] = asunto
        msg["From"] = user
        msg["To"] = destino

        with smtplib.SMTP(settings.EMAIL_HOST, settings.EMAIL_PORT, timeout=5.0) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)

        logger.info("Notificación Email enviada exitosamente a %s", destino)
        return True
    except Exception as exc:
        logger.error("Error al enviar alerta por Email: %s", exc)
        return False


async def enviar_email(asunto: str, cuerpo: str) -> bool:
    """Envía correo SMTP de forma asíncrona usando asyncio.to_thread."""
    return await asyncio.to_thread(_enviar_email_sync, asunto, cuerpo)


async def notificar_recepcion(texto: str, asunto: str = "🚨 LEAD CALIENTE — Cerro Alto") -> tuple[bool, str]:
    """
    Despacha la alerta según el canal configurado en NOTIF_CANAL ('telegram' o 'email').
    Retorna (exito, canal_utilizado).
    """
    canal = (settings.NOTIF_CANAL or "telegram").lower().strip()

    if canal == "email":
        exito = await enviar_email(asunto, texto)
        return exito, "email"

    # Default a telegram
    exito = await enviar_telegram(texto)
    return exito, "telegram"


# ── 4. Orquestador de enrutado por ruta ────────────────────────────────────────

async def enrutar_lead(
    ruta: str,
    event_id: str,
    nombre: str | None,
    telefono_e164: str | None,
    email: str | None,
    servicio: str | None,
    campana: str | None,
    plataforma: str | None,
    mensaje: str | None,
    es_paciente_existente: bool | None,
    tiene_tratamiento_activo: bool | None,
    lead_id: str | None,
    motivo: str | None,
) -> dict:
    """
    Ejecuta la acción operativa según la ruta asignada:
      - caliente: dispara aviso a recepción (< 5 min SLA).
      - tibio: redacta borrador de seguimiento personalizado.
      - basura: descarte silencioso con motivo.

    Retorna un diccionario con los campos a persistir en la DB.
    """
    if ruta == "caliente":
        alerta_texto = formatear_alerta_recepcion(
            event_id=event_id,
            nombre=nombre,
            telefono_e164=telefono_e164,
            email=email,
            servicio=servicio,
            campana=campana,
            plataforma=plataforma,
            mensaje=mensaje,
            es_paciente_existente=es_paciente_existente,
            tiene_tratamiento_activo=tiene_tratamiento_activo,
            lead_id=lead_id,
            motivo=motivo,
        )
        exito, canal = await notificar_recepcion(alerta_texto)
        logger.info(
            "Enrutado lead CALIENTE completado | event_id=%s notificado=%s canal=%s",
            event_id, exito, canal,
        )
        return {
            "notificacion_enviada": exito,
            "notificacion_canal": canal,
            "borrador_seguimiento": None,
        }

    elif ruta == "tibio":
        borrador = generar_borrador_seguimiento(
            nombre=nombre,
            servicio=servicio,
            mensaje_original=mensaje,
        )
        logger.info(
            "Enrutado lead TIBIO completado | event_id=%s borrador generado (%d chars)",
            event_id, len(borrador),
        )
        return {
            "notificacion_enviada": False,
            "notificacion_canal": None,
            "borrador_seguimiento": borrador,
        }

    elif ruta == "basura":
        logger.info(
            "Enrutado lead BASURA completado | event_id=%s descartado silenciosamente motivo=%s",
            event_id, motivo,
        )
        return {
            "notificacion_enviada": False,
            "notificacion_canal": None,
            "borrador_seguimiento": None,
        }

    # Ruta desconocida por seguridad
    return {
        "notificacion_enviada": False,
        "notificacion_canal": None,
        "borrador_seguimiento": None,
    }
