# database.py — Esquema SQLite y ciclo de vida de la base de datos.
#
# Por qué SQLite y no PostgreSQL:
#   - El enunciado pide "un solo comando para arrancar" → sin servidor externo.
#   - SQLite con WAL (Write-Ahead Log) soporta múltiples lectores simultáneos
#     y un escritor; suficiente para 60 leads/día y 150 los lunes pico.
#
# Por qué aiosqlite (async):
#   - FastAPI es 100 % asíncrono. Si usáramos sqlite3 síncrono bloquearíamos
#     el event loop → el servidor dejaría de responder durante cada consulta.
#
# Por qué SQLAlchemy Core (no ORM):
#   - Con tablas simples, el ORM agrega complejidad sin beneficio visible.
#   - SQLAlchemy Core nos da type hints, migrations futuras y protección
#     contra SQL injection de forma natural.

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import settings

# ── Motor de base de datos ────────────────────────────────────────────────────
# echo=False en producción para no llenar los logs con SQL.
# connect_args: WAL hace que lecturas no bloqueen escrituras.
engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False, "timeout": 30},
)

metadata = sa.MetaData()

# ── Tabla principal de leads ──────────────────────────────────────────────────
# Cada fila representa un evento recibido del conector de Meta Ads.
# Un lead "duplicado por persona" sigue siendo una fila, con estado=duplicado.
leads = sa.Table(
    "leads",
    metadata,
    # ── Identificadores ───────────────────────────────────────────────────────
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),

    # event_id viene del conector. UNIQUE + NOT NULL previenen duplicados
    # exactos (idempotencia). El INSERT fallará con IntegrityError si ya existe,
    # sin necesidad de un SELECT previo — elimina la condición de carrera.
    sa.Column("event_id", sa.String, nullable=False, unique=True),

    # ── Estado del pipeline ───────────────────────────────────────────────────
    # pendiente  → recibido, aún no procesado
    # procesado  → clasificado y enrutado con éxito
    # duplicado  → mismo event_id (idempotencia) o misma persona (30 días)
    # rechazado  → sin ningún medio de contacto válido
    # fallido    → se intentó procesar pero hubo error; pendiente de reproceso
    sa.Column(
        "estado",
        sa.String,
        nullable=False,
        default="pendiente",
        server_default="pendiente",
    ),

    # ── Ruta de clasificación ─────────────────────────────────────────────────
    # caliente | tibio | basura — solo cuando estado=procesado
    sa.Column("ruta", sa.String, nullable=True),

    # ── Payload crudo ─────────────────────────────────────────────────────────
    # Guardamos el JSON original ANTES de cualquier procesamiento.
    # Si la validación falla, el dato sigue aquí para reprocesar manualmente.
    sa.Column("raw_payload", sa.Text, nullable=False),

    # ── Campos normalizados (se rellenan en la etapa de validación) ───────────
    sa.Column("telefono_e164", sa.String, nullable=True, index=True),
    sa.Column("email_normalizado", sa.String, nullable=True, index=True),

    # Indica si el teléfono o correo original era inválido pero el otro sí valió
    sa.Column("telefono_invalido", sa.Boolean, nullable=False, server_default="0"),
    sa.Column("email_invalido", sa.Boolean, nullable=False, server_default="0"),

    # ── Enriquecimiento con API de pacientes ──────────────────────────────────
    # True si la API falló o hubo timeout; el lead sigue, pero sin datos del CRM
    sa.Column("enriquecimiento_pendiente", sa.Boolean, nullable=False, server_default="0"),
    sa.Column("es_paciente_existente", sa.Boolean, nullable=True),
    sa.Column("tiene_tratamiento_activo", sa.Boolean, nullable=True),

    # ── Deduplicación por persona ─────────────────────────────────────────────
    # Si es duplicado de otro lead, guardamos el event_id del original
    sa.Column("duplicado_de", sa.String, nullable=True),

    # ── Auditoría y motivo ────────────────────────────────────────────────────
    # motivo es obligatorio para duplicado, rechazado, fallido y basura
    sa.Column("motivo", sa.Text, nullable=True),

    # ── Timestamps ───────────────────────────────────────────────────────────
    sa.Column(
        "created_at",
        sa.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=sa.func.current_timestamp(),
    ),
    sa.Column("processed_at", sa.DateTime, nullable=True),
    sa.Column("event_created_at", sa.String, nullable=True),  # ISO del conector

    # ── Extras del conector ───────────────────────────────────────────────────
    sa.Column("source_platform", sa.String, nullable=True),
    sa.Column("source_campaign", sa.String, nullable=True),
    sa.Column("source_form_id", sa.String, nullable=True),

    # lead_id es el id en la API de pacientes (si existe)
    sa.Column("lead_id", sa.String, nullable=True),
)


async def init_db() -> None:
    """
    Crea las tablas si no existen. Se llama una vez al arrancar FastAPI.
    Configura WAL (Write-Ahead Logging) y busy_timeout para permitir lecturas
    concurrentes y serialización segura de escrituras bajo carga.
    """
    async with engine.begin() as conn:
        await conn.execute(sa.text("PRAGMA journal_mode=WAL;"))
        await conn.execute(sa.text("PRAGMA busy_timeout=30000;"))
        await conn.run_sync(metadata.create_all, checkfirst=True)


async def close_db() -> None:
    """Cierra el pool de conexiones al apagar FastAPI (buenas prácticas)."""
    await engine.dispose()
