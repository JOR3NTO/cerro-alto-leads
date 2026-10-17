# modelos.py — Esquemas Pydantic para validar el JSON entrante del conector.
#
# Por qué Pydantic aquí:
#   - FastAPI lo usa internamente para parsear el body de la petición.
#   - Pydantic V2 es estricto por default: si el tipo no coincide, lanza error.
#   - IMPORTANTE: aunque usamos Pydantic para parsear, si hay error de parseo
#     aún guardamos el raw body y respondemos 200 (regla del enunciado).
#
# Decisión de diseño: los campos son Optional con default None porque el
# conector "reenvía lo que escribió la persona" y puede omitir campos.
# Nunca retornamos 4xx por datos de lead malformados.

from typing import Any, Optional, Union
from pydantic import BaseModel, Field, field_validator


class Source(BaseModel):
    """Origen del lead: plataforma, campaña y formulario de Meta Ads."""
    platform: Optional[str] = None
    campaign: Optional[str] = None
    form_id: Optional[str] = None


class LeadData(BaseModel):
    """
    Datos del lead enviados por el conector.
    Todos los campos son opcionales porque el conector no valida nada.
    La validación real (teléfono E.164, correo RFC) ocurre en la Etapa 2.
    """
    full_name: Optional[str] = None
    # phone puede venir como string o como entero (evento_06: 593987650021)
    # Lo convertimos a string para procesarlo uniformemente.
    phone: Optional[Union[str, int]] = None
    email: Optional[str] = None
    # city puede venir como array (evento_06: ["Quito"]) o como string
    city: Optional[Union[str, list]] = None
    service_interest: Optional[str] = None
    message: Optional[str] = None

    @field_validator("phone", mode="before")
    @classmethod
    def phone_a_string(cls, v: Any) -> Optional[str]:
        """
        Convierte el teléfono a string si llega como número entero.
        Registrado como supuesto S-01 en DECISIONES.md.
        """
        if v is None:
            return None
        return str(v)

    @field_validator("city", mode="before")
    @classmethod
    def city_a_string(cls, v: Any) -> Optional[str]:
        """
        Si city es una lista, toma el primer elemento.
        Registrado como supuesto S-02 en DECISIONES.md.
        """
        if isinstance(v, list):
            return v[0] if v else None
        return v


class EventoWebhook(BaseModel):
    """
    Estructura completa de un evento enviado por el conector de Meta Ads.
    event_id es opcional aquí porque el enunciado pide asignar un id interno
    si falta (GET /leads/estado?desde=... los debe incluir).
    """
    event_id: Optional[str] = None
    event_type: Optional[str] = None
    created_at: Optional[str] = None  # ISO UTC como string; no parseamos a datetime aquí
    source: Optional[Source] = None
    lead: Optional[LeadData] = None
