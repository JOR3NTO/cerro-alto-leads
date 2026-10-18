# deduplicacion.py — Lógica de deduplicación de leads por persona (ventana 30 días).
#
# Por qué este módulo existe:
#   - Idempotencia (mismo event_id) ya la resuelve el constraint UNIQUE en DB.
#   - Pero si un usuario llena dos formularios en Facebook Ads o vuelve a escribir
#     días después, Meta genera un NUEVO event_id (ejemplo real: evt_03 vs evt_01).
#   - Sin deduplicación por persona, la clínica llamaría 2 o 3 veces al mismo paciente
#     por la misma consulta, molestándolo y saturando a recepción.
#
# Regla de negocio:
#   - Si en los últimos 30 días ya existe un lead con el mismo teléfono E.164
#     O el mismo correo normalizado:
#     → estado = 'duplicado'
#     → duplicado_de = event_id del lead original
#     → motivo = detalle auditable
#   - Excepción: leads anteriores en estado 'rechazado' NO bloquean al nuevo lead
#     (si antes falló por teléfono inválido y ahora escribe bien, debe procesarse).

from datetime import datetime, timedelta, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from app.database import leads


def asegurar_utc(dt: datetime) -> datetime:
    """
    Garantiza que el datetime tenga zona horaria UTC.
    
    Por qué es necesario:
      SQLite a veces guarda o retorna datetimes sin tzinfo (naive).
      Comparar un datetime con zona (aware) contra uno sin zona (naive)
      provoca TypeError en Python. Esta función estandariza a UTC.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def evaluar_duplicado(
    telefono_e164: Optional[str],
    email: Optional[str],
    fecha_actual: datetime,
    historial: list[dict],
    dias_ventana: int = 30,
    id_actual: Optional[int] = None,
) -> tuple[bool, Optional[str], Optional[str]]:
    """
    Función pura que evalúa si un contacto coincide con algún lead previo
    dentro de la ventana de días indicada.

    Regla clave de orden cronológico:
        Un lead NUNCA puede ser duplicado de otro que haya llegado después.
        Solo se consideran leads creados antes en el tiempo (o mismo segundo con menor id).

    Args:
        telefono_e164: Teléfono normalizado en E.164 (o None).
        email: Correo normalizado en minúsculas (o None).
        fecha_actual: Fecha y hora del lead actual.
        historial: Lista de diccionarios de leads previos en DB.
        dias_ventana: Días hacia atrás para considerar duplicado (por defecto 30).
        id_actual: ID numérico del lead actual para desempatar eventos simultáneos.

    Returns:
        (es_duplicado, event_id_original, campo_coincidente)
        Si no hay duplicado, retorna (False, None, None).
    """
    # Si no hay ningún dato de contacto, no hay forma de cotejar duplicidad
    if not telefono_e164 and not email:
        return False, None, None

    fecha_ref_utc = asegurar_utc(fecha_actual)
    limite_tiempo = fecha_ref_utc - timedelta(days=dias_ventana)

    for registro in historial:
        # 1. Ignorar leads que fueron rechazados:
        #    Si un paciente tuvo un lead anterior rechazado por datos corruptos,
        #    no queremos que ese error le impida registrarse con datos válidos ahora.
        if registro.get("estado") == "rechazado":
            continue

        created_at = registro.get("created_at")
        if not created_at:
            continue

        created_at_utc = asegurar_utc(created_at)

        # 2. Descartar leads posteriores en el tiempo (un lead no se duplica de futuros)
        if created_at_utc > fecha_ref_utc:
            continue
        if created_at_utc == fecha_ref_utc and id_actual is not None:
            reg_id = registro.get("id")
            if reg_id is not None and reg_id >= id_actual:
                continue

        # 3. Descartar leads más antiguos que la ventana de 30 días
        if created_at_utc < limite_tiempo:
            continue

        event_id_previo = registro.get("event_id")

        # 4. Coincidencia por teléfono E.164 exacto
        tel_previo = registro.get("telefono_e164")
        if telefono_e164 and tel_previo and telefono_e164 == tel_previo:
            return True, event_id_previo, "telefono"

        # 5. Coincidencia por correo normalizado
        email_previo = registro.get("email_normalizado")
        if email and email_previo and email.lower() == email_previo.lower():
            return True, event_id_previo, "email"

    return False, None, None


async def buscar_duplicado_db(
    engine: AsyncEngine,
    event_id_actual: str,
    id_actual: int,
    created_at_actual: datetime,
    telefono_e164: Optional[str],
    email: Optional[str],
    dias_ventana: int = 30,
) -> tuple[bool, Optional[str], Optional[str]]:
    """
    Busca en SQLite si existe un lead previo con el mismo teléfono o correo
    en los últimos `dias_ventana` días.

    Por qué consultar en base de datos:
      Evitamos cargar todo el histórico a memoria; filtramos por fecha y campos
      usando SQL, y seleccionamos el lead original más antiguo (ORDER BY created_at ASC).
    """
    if not telefono_e164 and not email:
        return False, None, None

    created_at_utc = asegurar_utc(created_at_actual)
    fecha_limite = created_at_utc - timedelta(days=dias_ventana)

    # Construir condición OR para teléfono y correo
    condiciones_contacto = []
    if telefono_e164:
        condiciones_contacto.append(leads.c.telefono_e164 == telefono_e164)
    if email:
        condiciones_contacto.append(leads.c.email_normalizado == email)

    # Un lead solo puede ser duplicado de otro que haya llegado ANTES en el tiempo.
    # Si dos eventos tienen el mismo timestamp, desempatamos con id < id_actual.
    condicion_anterioridad = sa.or_(
        leads.c.created_at < created_at_actual,
        sa.and_(
            leads.c.created_at == created_at_actual,
            leads.c.id < id_actual,
        ),
    )

    query = (
        sa.select(
            leads.c.id,
            leads.c.event_id,
            leads.c.created_at,
            leads.c.telefono_e164,
            leads.c.email_normalizado,
            leads.c.estado,
        )
        .where(
            # Excluir el propio evento actual
            leads.c.event_id != event_id_actual,
            # No duplicar contra leads que fueron rechazados
            leads.c.estado != "rechazado",
            # Filtrar por ventana temporal de 30 días hacia atrás
            leads.c.created_at >= fecha_limite,
            # Estricta anterioridad temporal
            condicion_anterioridad,
            # Coincidencia con al menos uno de los medios de contacto
            sa.or_(*condiciones_contacto),
        )
        # Ordenamos por antigüedad para vincular siempre con el primer evento
        .order_by(leads.c.created_at.asc(), leads.c.id.asc())
        .limit(10)
    )

    async with engine.connect() as conn:
        resultado = await conn.execute(query)
        filas = resultado.fetchall()

    if not filas:
        return False, None, None

    historial = [
        {
            "id": fila.id,
            "event_id": fila.event_id,
            "created_at": fila.created_at,
            "telefono_e164": fila.telefono_e164,
            "email_normalizado": fila.email_normalizado,
            "estado": fila.estado,
        }
        for fila in filas
    ]

    return evaluar_duplicado(
        telefono_e164=telefono_e164,
        email=email,
        fecha_actual=created_at_utc,
        historial=historial,
        dias_ventana=dias_ventana,
        id_actual=id_actual,
    )
