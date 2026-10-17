# normalizacion.py — Validación y normalización de teléfono y correo.
#
# Responsabilidad única de este módulo:
#   Dado un teléfono y/o correo "sucios" del conector, devuelve versiones
#   limpias y estandarizadas, y reporta cuáles son inválidos.
#
# Por qué separar en su propio módulo:
#   - Las reglas de negocio de validación pueden cambiar sin tocar el webhook.
#   - Es el módulo más fácil de testear de forma aislada (pura función → entrada/salida).
#   - El script reprocesar.py también llamará a estas mismas funciones.

import re
import logging
from dataclasses import dataclass
from typing import Optional

import phonenumbers
from phonenumbers import NumberParseException

logger = logging.getLogger(__name__)


# ── Resultado de la normalización ─────────────────────────────────────────────
@dataclass
class ResultadoNormalizacion:
    """
    Empaqueta todo lo que necesita el pipeline después de normalizar.
    Usamos dataclass en lugar de dict para tener autocompletar y type hints.
    """
    # Contacto válido en formato final (o None si es inválido)
    telefono_e164: Optional[str]
    email_normalizado: Optional[str]

    # Flags de auditoría: True si el valor original era inválido
    telefono_invalido: bool
    email_invalido: bool

    # Si ambos son inválidos, el lead debe ser rechazado
    debe_rechazarse: bool
    motivo_rechazo: Optional[str]


# ── Validación de correo ──────────────────────────────────────────────────────
# Usamos lógica propia (no librería) porque la validación RFC completa
# es innecesaria para nuestro caso: solo necesitamos detectar errores obvios
# como "sin @", "sin dominio" o strings vacíos.
_PATRON_EMAIL = re.compile(
    r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
)


def normalizar_email(email_raw: Optional[str]) -> tuple[Optional[str], bool]:
    """
    Limpia y valida un correo electrónico.

    Pasos:
      1. Si es None o vacío → inválido.
      2. strip() elimina espacios y saltos de línea al inicio y al final.
      3. lower() normaliza mayúsculas (evento_03 trae 'Carla.Mendoza@...').
      4. Valida con regex básica (tiene @, tiene dominio con punto).

    Returns:
        (email_limpio, es_invalido)
        - email_limpio: string normalizado o None si era inválido
        - es_invalido: True si el valor original no era utilizable
    """
    if not email_raw or not email_raw.strip():
        return None, True

    limpio = email_raw.strip().lower()

    if not _PATRON_EMAIL.match(limpio):
        logger.debug("Correo inválido descartado: %s", limpio)
        return None, True

    return limpio, False


# ── Normalización de teléfono — TU PIEZA ─────────────────────────────────────
# Esta función la escribes TÚ. Mira la firma, el comportamiento esperado
# y los casos de prueba al final del archivo.

def normalizar_telefono(telefono_raw: Optional[str]) -> tuple[Optional[str], bool]:
    """
    [FIRMA — esta función la escribe el estudiante]

    Convierte un teléfono "sucio" al formato E.164, asumiendo región Ecuador (EC).

    Comportamiento esperado:
      1. Si telefono_raw es None o vacío → devuelve (None, True).
      2. Intenta parsear con phonenumbers.parse(telefono_raw, "EC").
         - "EC" como región default significa que "0991234567" se lee como
           número ecuatoriano: +593991234567.
         - Si ya tiene código de país (+1, +593, etc.) lo respeta.
      3. Valida que sea un número posible (phonenumbers.is_valid_number).
         - "1111111111" es parseble pero NO válido.
      4. Si es válido → devuelve (e164, False) donde e164 es la forma
         phonenumbers.format_number(..., PhoneNumberFormat.E164).
      5. Si falla el parseo o no es válido → devuelve (None, True).
      6. No lanza excepciones: captura NumberParseException.

    Args:
        telefono_raw: Teléfono como string, en cualquier formato.
                      Puede ser None, vacío, o venir con espacios/guiones.

    Returns:
        (telefono_e164, es_invalido)
        - telefono_e164: string E.164 (ej: "+593991234567") o None
        - es_invalido: True si no se pudo normalizar

    Casos de prueba (los verificamos con pytest):
        normalizar_telefono("0991234567")       → ("+593991234567", False)
        normalizar_telefono("+593 99 123 4567") → ("+593991234567", False)
        normalizar_telefono("098 765 4321")     → ("+593987654321", False)
        normalizar_telefono("+1 (305) 555-0142")→ ("+13055550142",  False)  # válido, extranjero
        normalizar_telefono("1111111111")       → (None, True)              # parseble pero inválido
        normalizar_telefono(None)               → (None, True)
        normalizar_telefono("")                 → (None, True)
        normalizar_telefono("abc")              → (None, True)
    """
    # ── Paso 1: Guardia de entrada ────────────────────────────────────────────
    # Si no llega nada (None) o el string está vacío después de limpiar
    # espacios, no hay nada que procesar → inválido de inmediato.
    if not telefono_raw or not telefono_raw.strip():
        return None, True

    # ── Paso 2: Intentar parsear el número ────────────────────────────────────
    # phonenumbers.parse() entiende formatos con espacios, guiones, paréntesis.
    # "EC" es el código de región por defecto: si el número empieza con 0
    # (formato local Ecuador, ej: "0991234567"), lo convierte a +593991234567.
    # Si ya trae código de país ("+1...", "+593..."), lo respeta tal cual.
    try:
        numero = phonenumbers.parse(telefono_raw.strip(), "EC")
    except NumberParseException:
        # El string no tiene ningún parecido a un número de teléfono (ej: "abc")
        logger.debug("Teléfono no parseable: %s", telefono_raw)
        return None, True

    # ── Paso 3: Validar que sea un número real ────────────────────────────────
    # is_valid_number() va más allá de la sintaxis: comprueba que la longitud
    # y el prefijo existan de verdad en algún país del mundo.
    # Ejemplo: "1111111111" se parsea sin error pero NO es un número válido.
    if not phonenumbers.is_valid_number(numero):
        logger.debug("Teléfono parseado pero inválido: %s", telefono_raw)
        return None, True

    # ── Paso 4: Formatear a E.164 ─────────────────────────────────────────────
    # E.164 es el estándar internacional: "+" + código de país + número, sin
    # espacios ni guiones. Ejemplo: "+593991234567".
    # La API de pacientes exige EXACTAMENTE este formato para buscar.
    e164 = phonenumbers.format_number(numero, phonenumbers.PhoneNumberFormat.E164)
    return e164, False
    # ── FIN DE LA FUNCIÓN ─────────────────────────────────────────────────────


# ── Orquestador principal ─────────────────────────────────────────────────────

def validar_y_normalizar(
    telefono_raw: Optional[str],
    email_raw: Optional[str],
) -> ResultadoNormalizacion:
    """
    Punto de entrada para el pipeline.
    Llama a normalizar_telefono y normalizar_email y aplica las reglas de negocio:

    Reglas (del enunciado):
      - Si teléfono inválido pero correo válido → procesa con correo, marca telefono_invalido=True.
      - Si correo inválido pero teléfono válido → procesa con teléfono, marca email_invalido=True.
      - Si ambos inválidos → debe_rechazarse=True con motivo.
      - Nombre vacío → NO descarta el lead (regla explícita del enunciado).
    """
    tel_e164, tel_invalido = normalizar_telefono(telefono_raw)
    email_norm, email_invalido = normalizar_email(email_raw)

    # Determinar si el lead debe ser rechazado
    sin_contacto = tel_invalido and email_invalido
    motivo = None

    if sin_contacto:
        motivo = (
            f"Sin medio de contacto válido. "
            f"Teléfono recibido: '{telefono_raw}'. "
            f"Correo recibido: '{email_raw}'."
        )
        logger.info("Lead rechazado por falta de contacto | tel=%s email=%s", telefono_raw, email_raw)

    return ResultadoNormalizacion(
        telefono_e164=tel_e164,
        email_normalizado=email_norm,
        telefono_invalido=tel_invalido,
        email_invalido=email_invalido,
        debe_rechazarse=sin_contacto,
        motivo_rechazo=motivo,
    )
