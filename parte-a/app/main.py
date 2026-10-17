# main.py — Punto de arranque de la aplicación FastAPI.
#
# Aquí viven SOLO:
#   - La creación de la app FastAPI
#   - El ciclo de vida (startup/shutdown)
#   - El registro de routers
#   - La configuración de logging
#
# NO ponemos lógica de negocio aquí para que sea fácil de testear y extender.

import logging
import sys

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.database import close_db, init_db
from app.webhook import router as webhook_router


# ── Configuración de logging ─────────────────────────────────────────────────
# Formato: timestamp nivel módulo mensaje
# Escribimos a stdout para que sea fácil capturar con Docker/systemd/etc.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)


# ── Ciclo de vida de la aplicación ───────────────────────────────────────────
# El @asynccontextmanager reemplaza los eventos on_startup/on_shutdown
# deprecados en FastAPI >= 0.93.
# Todo lo que está ANTES del yield corre al arrancar.
# Todo lo que está DESPUÉS del yield corre al apagar.
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ARRANQUE
    logger.info("Iniciando Cerro Alto Leads — Parte A")
    await init_db()
    logger.info("Base de datos lista | url=%s", settings.DATABASE_URL)
    yield
    # APAGADO
    await close_db()
    logger.info("Conexiones de base de datos cerradas")


# ── Aplicación FastAPI ────────────────────────────────────────────────────────
app = FastAPI(
    title="Cerro Alto Leads — Webhook",
    description="Recibe leads de Meta Ads y los procesa para la clínica dental Cerro Alto.",
    version="0.1.0",
    lifespan=lifespan,
)

# ── Registro de routers ───────────────────────────────────────────────────────
# El webhook vive en /webhook; en Etapa 8 agregaremos /leads/estado
app.include_router(webhook_router)


# ── Ruta de salud ─────────────────────────────────────────────────────────────
# Útil para verificar que el servidor está vivo sin tocar la base de datos.
@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "0.1.0"}
