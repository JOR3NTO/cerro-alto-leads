# test_etapa6_enrutado.py — Tests para la Etapa 6 (Enrutado y avisos a recepción).
#
# Casos cubiertos:
#   1. Generación de borradores para leads tibios (con nombre, sin nombre, con/sin servicio).
#   2. Formato de alerta a recepción para leads calientes (SLA < 5 min, teléfono visible, contexto CRM).
#   3. Despacho a Telegram (simulado cuando no hay credenciales, mock HTTP cuando hay token).
#   4. Despacho a Email (configuración por NOTIF_CANAL).
#   5. Tolerancia a fallos: caídas en Telegram no crashean el pipeline.
#   6. Descarte silencioso para leads clasificados como basura.
#   7. Integración completa en el pipeline procesar_lead (verificación en DB).

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import sqlalchemy as sa

from app.config import settings
from app.database import engine, leads
from app.enrutado import (
    enrutar_lead,
    enviar_email,
    enviar_telegram,
    formatear_alerta_recepcion,
    generar_borrador_seguimiento,
    notificar_recepcion,
)
from app.procesamiento import procesar_lead


# ── 1. Pruebas unitarias del generador de borradores (Leads Tibios) ───────────

class TestGenerarBorradorSeguimiento:
    def test_borrador_con_nombre_y_servicio(self):
        """Borrador personalizado con nombre de la persona y servicio consultado."""
        borrador = generar_borrador_seguimiento(
            nombre="João Pedro Almeida",
            servicio="Ortodoncia",
            mensaje_original="Quanto custa o aparelho?",
        )
        assert "Hola Joao Pedro Almeida," in borrador or "Hola João Pedro Almeida," in borrador
        assert "Ortodoncia" in borrador
        assert "Clínica Dental Cerro Alto" in borrador
        assert "¿Te gustaría que te agendemos una cita" in borrador

    def test_borrador_sin_nombre(self):
        """Si el nombre es None o vacío, usa un saludo cortés 'Hola,'."""
        borrador = generar_borrador_seguimiento(
            nombre="",
            servicio="Limpieza",
            mensaje_original="cuánto cuesta la limpieza?",
        )
        assert borrador.startswith("Hola, gracias por comunicarte")
        assert "Limpieza" in borrador

    def test_borrador_sin_servicio(self):
        """Si no especifica servicio, usa 'tratamientos odontológicos' por defecto."""
        borrador = generar_borrador_seguimiento(
            nombre="Carlos",
            servicio=None,
            mensaje_original="info por favor",
        )
        assert "Hola Carlos," in borrador
        assert "tratamientos odontológicos" in borrador


# ── 2. Pruebas de formateo de alerta a recepción (Leads Calientes) ─────────────

class TestFormatearAlertaRecepcion:
    def test_alerta_incluye_telefono_completo_sla_y_crm(self):
        """
        En la alerta interna de recepción el teléfono SÍ debe estar completo
        para que puedan llamar al instante (cumplimiento SLA < 5 min).
        """
        alerta = formatear_alerta_recepcion(
            event_id="evt_01JB7Q3M2X4K8R9T5W6Y",
            nombre="Carla Mendoza",
            telefono_e164="+593991234567",
            email="carla.mendoza@example.com",
            servicio="Blanqueamiento",
            campana="Sonrisa Blanca",
            plataforma="instagram",
            mensaje="Hola, tienen turno para el jueves?",
            es_paciente_existente=True,
            tiene_tratamiento_activo=True,
            lead_id="pac_7K2M1",
            motivo="Paciente con tratamiento activo en clínica",
        )
        # SLA y urgencia
        assert "SLA: Llamar en menos de 5 minutos" in alerta
        # Datos sin enmascarar para operatividad
        assert "+593991234567" in alerta
        assert "Carla Mendoza" in alerta
        # Contexto del CRM
        assert "pac_7K2M1" in alerta
        assert "Atención prioritaria" in alerta
        assert "evt_01JB7Q3M2X4K8R9T5W6Y" in alerta


# ── 3. Canales de notificación: Telegram y Email ─────────────────────────────

@pytest.mark.asyncio
async def test_telegram_sin_credenciales_simula_sin_fallar():
    """Si no hay token o chat_id, registra en log y retorna True sin crashear."""
    with patch.object(settings, "TELEGRAM_BOT_TOKEN", ""), patch.object(settings, "TELEGRAM_CHAT_ID", ""):
        exito = await enviar_telegram("Mensaje de prueba")
        assert exito is True


@pytest.mark.asyncio
async def test_telegram_con_credenciales_envia_post():
    """Con token y chat_id configurados, hace POST a la API de Telegram."""
    mock_resp = AsyncMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()

    with patch.object(settings, "TELEGRAM_BOT_TOKEN", "123456:ABC-DEF"):
        with patch.object(settings, "TELEGRAM_CHAT_ID", "-100123456"):
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = mock_resp
                exito = await enviar_telegram("Alerta a recepción")
                assert exito is True

                llamada = mock_post.call_args
                url = llamada[0][0]
                assert "api.telegram.org/bot123456:ABC-DEF/sendMessage" in url
                assert llamada.kwargs["json"]["chat_id"] == "-100123456"


@pytest.mark.asyncio
async def test_telegram_error_red_no_rompe_flujo():
    """Si la API de Telegram falla o da timeout, retorna False pero no lanza excepción."""
    mock_req = httpx.Request("POST", "https://api.telegram.org")
    error_http = httpx.HTTPStatusError("400 Bad Request", request=mock_req, response=httpx.Response(400))

    with patch.object(settings, "TELEGRAM_BOT_TOKEN", "token"), patch.object(settings, "TELEGRAM_CHAT_ID", "chat"):
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = error_http
            exito = await enviar_telegram("Alerta")
            assert exito is False


@pytest.mark.asyncio
async def test_despacho_por_canal_email():
    """Si NOTIF_CANAL está configurado como 'email', despacha por email."""
    with patch.object(settings, "NOTIF_CANAL", "email"):
        with patch("app.enrutado.enviar_email", new_callable=AsyncMock) as mock_email:
            mock_email.return_value = True
            exito, canal = await notificar_recepcion("Texto de alerta")
            assert exito is True
            assert canal == "email"
            mock_email.assert_called_once()


# ── 4. Pruebas de la función de enrutado según la ruta ────────────────────────

@pytest.mark.asyncio
async def test_enrutar_lead_caliente_dispara_aviso():
    """Lead caliente despacha aviso a recepción y no genera borrador de mensaje."""
    with patch("app.enrutado.notificar_recepcion", new_callable=AsyncMock) as mock_notif:
        mock_notif.return_value = (True, "telegram")
        res = await enrutar_lead(
            ruta="caliente",
            event_id="evt_caliente_1",
            nombre="Rosa Elena",
            telefono_e164="+593987650021",
            email="rosa@example.com",
            servicio="Valoración",
            campana="Chequeo",
            plataforma="facebook",
            mensaje="Deseo turno urgente",
            es_paciente_existente=True,
            tiene_tratamiento_activo=True,
            lead_id="pac_123",
            motivo="Tratamiento activo",
        )
        assert res["notificacion_enviada"] is True
        assert res["notificacion_canal"] == "telegram"
        assert res["borrador_seguimiento"] is None
        mock_notif.assert_called_once()


@pytest.mark.asyncio
async def test_enrutar_lead_tibio_crea_borrador():
    """Lead tibio no notifica a recepción pero genera borrador en DB."""
    with patch("app.enrutado.notificar_recepcion", new_callable=AsyncMock) as mock_notif:
        res = await enrutar_lead(
            ruta="tibio",
            event_id="evt_tibio_1",
            nombre="João Pedro",
            telefono_e164="+13055550142",
            email="joao@example.com",
            servicio="Ortodoncia",
            campana="Ortodoncia sin excusas",
            plataforma="instagram",
            mensaje="Quanto custa?",
            es_paciente_existente=False,
            tiene_tratamiento_activo=False,
            lead_id=None,
            motivo="Pregunta informativa de precios",
        )
        assert res["notificacion_enviada"] is False
        assert res["notificacion_canal"] is None
        assert res["borrador_seguimiento"] is not None
        assert "Ortodoncia" in res["borrador_seguimiento"]
        mock_notif.assert_not_called()


@pytest.mark.asyncio
async def test_enrutar_lead_basura_descarte_silencioso():
    """Lead basura se descarta silenciosamente sin avisos ni borradores."""
    with patch("app.enrutado.notificar_recepcion", new_callable=AsyncMock) as mock_notif:
        res = await enrutar_lead(
            ruta="basura",
            event_id="evt_basura_1",
            nombre="GANA DINERO",
            telefono_e164="+593991234567",
            email="spam@ganadinero.example",
            servicio="Otro",
            campana="Spam",
            plataforma="facebook",
            mensaje="GANA $500 DIARIOS",
            es_paciente_existente=False,
            tiene_tratamiento_activo=False,
            lead_id=None,
            motivo="Spam comercial evidente",
        )
        assert res["notificacion_enviada"] is False
        assert res["notificacion_canal"] is None
        assert res["borrador_seguimiento"] is None
        mock_notif.assert_not_called()


# ── 5. Integración en el pipeline completo (DB leads) ────────────────────────

@pytest.mark.asyncio
async def test_pipeline_lead_caliente_guarda_enrutado_en_db():
    """
    Lead con dolor dental (caliente):
    - Al procesarse debe quedar con notificacion_enviada=True
    - borrador_seguimiento=None
    """
    event_id = "evt_etapa6_caliente"
    payload = {
        "event_id": event_id,
        "event_type": "lead.created",
        "created_at": "2026-11-03T14:22:05Z",
        "source": {"platform": "instagram", "campaign": "Urgencias"},
        "lead": {
            "full_name": "Valeria Castro",
            "phone": "0991234567",
            "email": "valeria@example.com",
            "service_interest": "Extracción",
            "message": "Me duele mucho una muela por favor necesito cita hoy",
        },
    }

    async with engine.begin() as conn:
        await conn.execute(
            leads.insert().values(
                event_id=event_id,
                estado="pendiente",
                raw_payload=json.dumps(payload),
            )
        )

    await procesar_lead(event_id)

    async with engine.connect() as conn:
        fila = (await conn.execute(
            sa.select(
                leads.c.estado,
                leads.c.ruta,
                leads.c.notificacion_enviada,
                leads.c.notificacion_canal,
                leads.c.borrador_seguimiento,
            )
            .where(leads.c.event_id == event_id)
        )).fetchone()

    assert fila is not None
    assert fila.estado == "procesado"
    assert fila.ruta == "caliente"
    assert fila.notificacion_enviada is True
    assert fila.notificacion_canal in ("telegram", "email")
    assert fila.borrador_seguimiento is None


@pytest.mark.asyncio
async def test_pipeline_lead_tibio_guarda_borrador_en_db():
    """
    Lead informativo (tibio):
    - Al procesarse debe quedar con notificacion_enviada=False
    - borrador_seguimiento con texto listo para revisión
    """
    event_id = "evt_etapa6_tibio"
    payload = {
        "event_id": event_id,
        "event_type": "lead.created",
        "created_at": "2026-11-03T14:22:05Z",
        "source": {"platform": "facebook", "campaign": "Blanqueamiento"},
        "lead": {
            "full_name": "Mateo Morales",
            "phone": "0950009999",
            "email": "mateo.morales.nuevo@example.com",
            "service_interest": "Blanqueamiento",
            "message": "info",
        },
    }

    async with engine.begin() as conn:
        await conn.execute(
            leads.insert().values(
                event_id=event_id,
                estado="pendiente",
                raw_payload=json.dumps(payload),
            )
        )

    await procesar_lead(event_id)

    async with engine.connect() as conn:
        fila = (await conn.execute(
            sa.select(
                leads.c.estado,
                leads.c.ruta,
                leads.c.motivo,
                leads.c.notificacion_enviada,
                leads.c.borrador_seguimiento,
            )
            .where(leads.c.event_id == event_id)
        )).fetchone()

    assert fila is not None
    assert fila.estado == "procesado"
    assert fila.ruta == "tibio", f"Ruta fue {fila.ruta}, motivo: {fila.motivo}"
    assert fila.notificacion_enviada is False
    assert fila.borrador_seguimiento is not None
    assert "Mateo Morales" in fila.borrador_seguimiento
    assert "Blanqueamiento" in fila.borrador_seguimiento
