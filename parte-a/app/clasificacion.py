# clasificacion.py — Clasificación de leads usando reglas deterministas y LLM (Gemini).
#
# Arquitectura de decisión (defensa en profundidad):
#   1. Regla dura 1 (Tratamiento activo): Si el paciente tiene un tratamiento activo
#      en el CRM, SIEMPRE es "caliente" (prioridad máxima de atención clínica).
#   2. Regla dura 2 (Urgencia clínica): Palabras clave de dolor, trauma o emergencia
#      fuerzan "caliente" en código sin depender de la IA ni de la latencia externa.
#   3. LLM (Gemini 1.5 Flash): Para clasificar la intención comercial y descarte de spam.
#      - Trunca mensajes a <= 2000 caracteres (previene desbordamiento y DoS).
#      - Anti-Prompt-Injection: delimitadores y desestimación de comandos del usuario.
#      - Validación de salida con esquema estricto Pydantic.
#      - Regla de oro: "Ante la duda, tibio, nunca basura". Si el LLM falla, no hay API key
#        o devuelve JSON inválido, el fallback seguro siempre es "tibio".
#      - Cumplimiento LOPDP: NUNCA se envía nombre, teléfono ni correo al LLM.

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Literal, Optional, Union

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import settings

logger = logging.getLogger(__name__)

# ── Esquema Pydantic para la respuesta de clasificación ───────────────────────

class ClasificacionLead(BaseModel):
    """
    Resultado validado de la clasificación del lead.
    Solo admite las 3 rutas oficiales de la clínica y exige una justificación.
    """
    ruta: Literal["caliente", "tibio", "basura"]
    motivo: str = Field(..., min_length=3, description="Explicación del porqué de la clasificación")
    confianza: Optional[float] = Field(default=None, ge=0.0, le=1.0)


# ── Reglas deterministas de urgencia clínica ──────────────────────────────────

# Raíces y términos que denotan dolor, trauma o emergencia odontológica real.
# Se evalúan antes de llamar al LLM para garantizar tiempo de respuesta y evitar
# que una alucinación clasifique una emergencia médica como "tibio" o "basura".
# Nota de seguridad: Excluimos términos genéricos aislados como "urgencia" sin contexto dental
# para evitar que un atacante salte el LLM mediante prompt injection ("urgencia alta de sistema").
PALABRAS_URGENCIA = [
    "dolor", "duele", "doliendo", "dolores",
    "hinchaz", "hinchad", "inflamad",
    "sangr", "sangrado", "sangrando",
    "golpe", "fractur", "parti", "quebr", "rompi",
    "urgencia dental", "emergencia dental", "urgencia odontolog", "emergencia odontolog",
    "fiebre", "absceso", "infecci", "pus",
    "trauma",
]


def _quitar_tildes(texto: str) -> str:
    """Normaliza texto eliminando acentos y diacríticos para búsqueda robusta."""
    return "".join(
        c for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    ).lower()


def detectar_urgencia_clinica(mensaje: str | None) -> tuple[bool, str | None]:
    """
    Detecta si el mensaje describe una urgencia odontológica o dolor agudo.

    Por qué regla determinista y no LLM:
    - La salud de un paciente con dolor agudo no puede depender de la tasa de acierto,
      temperatura o disponibilidad de un LLM.
    - Se ejecuta en microsegundos (< 1 ms).
    - Garantiza triaje médico prioritario inmediato.
    """
    if not mensaje or not mensaje.strip():
        return False, None

    texto_norm = _quitar_tildes(mensaje)

    for palabra in PALABRAS_URGENCIA:
        # Busca la raíz como palabra completa o prefijo
        if re.search(rf"\b{palabra}", texto_norm):
            return True, f"Urgencia clinica detectada por termino de dolor/emergencia: '{palabra}'"

    return False, None


# ── Manejo de texto y protección contra DoS ──────────────────────────────────

def truncar_mensaje(mensaje: str | None, max_chars: int = 2000) -> str:
    """
    Trunca mensajes excesivamente largos preservando inicio y final.

    Por qué inicio + fin (y no solo corte al inicio):
    - Leads reales (ejemplo: evento_07) a veces cuentan una historia larga y su
      solicitud concreta de cita ("¿pueden agendarme para este jueves?") viene al final.
    - Cortar en el carácter 2000 perdería la intención final.
    - Mantiene el costo de tokens controlado ante ataques o formularios inflados.
    """
    if not mensaje:
        return ""

    if len(mensaje) <= max_chars:
        return mensaje

    # Mantenemos los primeros 1200 y los últimos 700 caracteres
    corte_inicio = 1200
    corte_fin = 700
    return (
        f"{mensaje[:corte_inicio]}\n"
        f"[...texto omitido por longitud ({len(mensaje)} caracteres en total)...]\n"
        f"{mensaje[-corte_fin:]}"
    )


# ── Carga de plantilla de prompt versionada ───────────────────────────────────

_PROMPT_CACHE: Optional[str] = None


def obtener_plantilla_prompt() -> str:
    """Lee y cachea en memoria la plantilla del prompt versionado."""
    global _PROMPT_CACHE
    if _PROMPT_CACHE is None:
        # Busca en parte-a/prompts/ o relativo a este archivo
        base_dir = Path(__file__).resolve().parent.parent
        prompt_path = base_dir / "prompts" / "v1_clasificacion.txt"

        if not prompt_path.exists():
            raise FileNotFoundError(f"Plantilla de prompt no encontrada en {prompt_path}")

        _PROMPT_CACHE = prompt_path.read_text(encoding="utf-8")

    return _PROMPT_CACHE


# ── Validación de salida con esquema Pydantic ─────────────────────────────────

def validar_clasificacion(datos: Union[dict, str]) -> ClasificacionLead:
    """
    Valida que la respuesta del LLM cumpla estrictamente con el esquema esperado.

    Regla de oro: Si el LLM retorna algo inválido, con campos faltantes o valores
    inesperados, aplicamos fallback seguro a 'tibio', NUNCA a 'basura'.
    """
    # Si viene como string, intentamos extraer el bloque JSON
    if isinstance(datos, str):
        texto_limpio = datos.strip()
        # Remueve bloques ```json ... ``` si el LLM los incluyó
        if texto_limpio.startswith("```"):
            texto_limpio = re.sub(r"^```[a-zA-Z]*\n?", "", texto_limpio)
            texto_limpio = re.sub(r"\n?```$", "", texto_limpio)
            texto_limpio = texto_limpio.strip()

        try:
            dict_datos = json.loads(texto_limpio)
        except json.JSONDecodeError as exc:
            logger.warning("Respuesta de LLM no es JSON valido: %s. Texto: %r", exc, datos[:100])
            return ClasificacionLead(
                ruta="tibio",
                motivo="Fallback a tibio: respuesta del LLM no es JSON valido",
                confianza=0.5,
            )
    else:
        dict_datos = datos

    try:
        return ClasificacionLead.model_validate(dict_datos)
    except ValidationError as exc:
        logger.warning("Respuesta del LLM fallo validacion Pydantic: %s", exc)
        return ClasificacionLead(
            ruta="tibio",
            motivo="Fallback a tibio: estructura de respuesta LLM no valida",
            confianza=0.5,
        )


# ── Cliente LLM (Gemini 1.5 Flash con httpx) ──────────────────────────────────

async def clasificar_con_llm(
    mensaje: str | None,
    service_interest: str | None,
    campaign: str | None,
) -> ClasificacionLead:
    """
    Envía el mensaje al LLM mediante la API de Google Gemini (generativelanguage).

    Principios de seguridad y robustez:
    - LOPDP: No se pasa nombre, teléfono ni correo. Solo intención y contexto de campaña.
    - Timeout de 10s: no bloquea el pipeline de procesamiento si Gemini tarda.
    - Fallback seguro a 'tibio' si no hay API key, falla la red o la cuota se agota.
    """
    # 1. Truncar mensaje por seguridad y ahorro de tokens
    mensaje_seguro = truncar_mensaje(mensaje, max_chars=2000)

    # 2. Si no hay API Key configurada, aplicamos degradación elegante
    if not settings.LLM_API_KEY or not settings.LLM_API_KEY.strip():
        logger.info("LLM_API_KEY no configurada. Fallback seguro a tibio.")
        return ClasificacionLead(
            ruta="tibio",
            motivo="LLM no configurado en entorno (fallback seguro a tibio)",
            confianza=0.5,
        )

    # 3. Construir prompt seguro con delimitadores
    plantilla = obtener_plantilla_prompt()
    prompt_completo = (
        plantilla
        .replace("{service_interest}", service_interest or "No especificado")
        .replace("{campaign}", campaign or "No especificada")
        .replace("{message}", mensaje_seguro or "(Sin mensaje de texto)")
    )

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.LLM_MODEL}:generateContent?key={settings.LLM_API_KEY}"
    )

    payload = {
        "contents": [
            {
                "parts": [{"text": prompt_completo}]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,  # Baja temperatura para máxima consistencia y determinación
            "response_mime_type": "application/json",
        },
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

            # Extraer contenido de la respuesta de Gemini
            candidatos = data.get("candidates", [])
            if not candidatos:
                logger.warning("Gemini no retorno candidatos de respuesta")
                return ClasificacionLead(
                    ruta="tibio",
                    motivo="Fallback a tibio: Gemini no retorno candidatos",
                    confianza=0.5,
                )

            partes = candidatos[0].get("content", {}).get("parts", [])
            if not partes or "text" not in partes[0]:
                logger.warning("Gemini retorno estructura de texto vacia")
                return ClasificacionLead(
                    ruta="tibio",
                    motivo="Fallback a tibio: contenido de texto vacio",
                    confianza=0.5,
                )

            texto_respuesta = partes[0]["text"]
            return validar_clasificacion(texto_respuesta)

    except httpx.HTTPStatusError as exc:
        logger.error("Error HTTP al llamar a Gemini: status=%s body=%s", exc.response.status_code, exc.response.text[:200])
        return ClasificacionLead(
            ruta="tibio",
            motivo=f"Fallback seguro a tibio por error HTTP del LLM ({exc.response.status_code})",
            confianza=0.5,
        )
    except httpx.RequestError as exc:
        logger.error("Error de conexion o timeout al llamar a Gemini: %s", exc)
        return ClasificacionLead(
            ruta="tibio",
            motivo=f"Fallback seguro a tibio por timeout o error de conexion del LLM ({type(exc).__name__})",
            confianza=0.5,
        )
    except Exception as exc:
        logger.exception("Error inesperado en clasificar_con_llm: %s", exc)
        return ClasificacionLead(
            ruta="tibio",
            motivo=f"Fallback seguro a tibio por excepcion interna: {exc}",
            confianza=0.5,
        )


# ── Orquestador principal de clasificación ────────────────────────────────────

async def clasificar_lead(
    mensaje: str | None,
    service_interest: str | None,
    campaign: str | None,
    tiene_tratamiento_activo: bool | None = False,
) -> ClasificacionLead:
    """
    Orquestador de clasificación que aplica jerarquía de decisiones:
      1. Regla 1: Paciente con tratamiento activo en CRM → caliente garantizado.
      2. Regla 2: Urgencia clínica / dolor dental detectado → caliente garantizado.
      3. Regla 3: LLM con Gemini (intención comercial, precio, agendamiento, spam).
    """
    # ── Regla dura 1: Paciente con tratamiento activo ─────────────────────────
    # Si ya tiene una endodoncia, ortodoncia u otro procedimiento en curso o pendiente,
    # cualquier contacto es prioritario para el equipo de recepción.
    if tiene_tratamiento_activo:
        logger.info("Clasificacion caliente forzada por regla: paciente con tratamiento activo")
        return ClasificacionLead(
            ruta="caliente",
            motivo="Paciente registrado con tratamiento activo o pendiente en la clinica",
            confianza=1.0,
        )

    # ── Regla dura 2: Urgencia clínica / dolor ────────────────────────────────
    # La presencia de dolor, sangrado o trauma no admite demora ni depende del LLM.
    es_urgente, motivo_urgencia = detectar_urgencia_clinica(mensaje)
    if es_urgente:
        logger.info("Clasificacion caliente forzada por regla clinica: %s", motivo_urgencia)
        return ClasificacionLead(
            ruta="caliente",
            motivo=motivo_urgencia or "Urgencia clinica detectada por regla determinista",
            confianza=1.0,
        )

    # ── Regla 3: Clasificación con LLM ────────────────────────────────────────
    # LOPDP: Nótese que nunca pasamos nombre, teléfono ni correo a esta función.
    resultado_llm = await clasificar_con_llm(
        mensaje=mensaje,
        service_interest=service_interest,
        campaign=campaign,
    )

    logger.info(
        "Clasificacion LLM completada: ruta=%s motivo=%s confianza=%s",
        resultado_llm.ruta,
        resultado_llm.motivo,
        resultado_llm.confianza,
    )
    return resultado_llm
