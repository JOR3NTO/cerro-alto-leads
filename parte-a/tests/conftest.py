# conftest.py — Configuración global de pytest para la Parte A.
#
# Por qué este archivo:
#   - pytest lo carga automáticamente antes de cualquier test.
#   - Aquí configuramos la base de datos en memoria para tests limpios.
#   - Centralizamos el cliente HTTP reutilizable.
#
# Por qué SQLite :memory: para tests:
#   - Cada corrida de pytest empieza desde cero, sin residuos de corridas anteriores.
#   - No hay riesgo de corromper la base de datos de desarrollo.
#   - Es más rápida que escribir a disco.
#
# Estrategia de patch:
#   Reemplazamos `app.database.engine` directamente ANTES de que cualquier
#   test importe los módulos que lo usan. Python cachea los módulos, así que
#   el patch persiste para toda la sesión de pytest.

import os
import tempfile
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

import app.database as db_module
import app.webhook as webhook_module
import app.procesamiento as procesamiento_module

# ── Base de datos temporal con WAL mode para tests ───────────────────────────
# Usamos un archivo temporal en disco (en vez de :memory: puro) porque SQLite
# en modo WAL (Write-Ahead Logging) requiere archivo en disco para soportar
# transacciones concurrentes reales sin bloquearse ("database table is locked").
_test_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_test_db_path = _test_db_file.name
_test_db_file.close()

_test_engine = create_async_engine(
    f"sqlite+aiosqlite:///{_test_db_path}",
    echo=False,
    connect_args={"check_same_thread": False, "timeout": 30},
)

# Reemplazamos el engine en todos los módulos que lo importan
db_module.engine = _test_engine
webhook_module.engine = _test_engine
procesamiento_module.engine = _test_engine


@pytest_asyncio.fixture(scope="session", autouse=True)
async def inicializar_db_test():
    """
    Crea las tablas en la base de datos de test con WAL habilitado.
    Se ejecuta UNA vez antes de todos los tests de la sesión.
    """
    async with _test_engine.begin() as conn:
        await conn.execute(sa.text("PRAGMA journal_mode=WAL;"))
        await conn.execute(sa.text("PRAGMA busy_timeout=30000;"))
        await conn.run_sync(db_module.metadata.create_all, checkfirst=True)

    yield

    # Limpieza al final de la sesión: cerrar conexiones y borrar archivos temporales
    await _test_engine.dispose()
    try:
        if os.path.exists(_test_db_path):
            os.remove(_test_db_path)
        for ext in ["-shm", "-wal"]:
            wal_file = _test_db_path + ext
            if os.path.exists(wal_file):
                os.remove(wal_file)
    except Exception:
        pass


@pytest_asyncio.fixture(autouse=True)
async def limpiar_db_por_test():
    """
    Limpia los registros de las tablas antes de cada test para garantizar
    que las pruebas estén 100 % aisladas y no haya contaminación cruzada.
    """
    async with _test_engine.begin() as conn:
        await conn.execute(db_module.leads.delete())
    yield


@pytest_asyncio.fixture
async def cliente(inicializar_db_test):
    """
    Cliente HTTP que habla directamente con la app ASGI.
    No levanta un servidor de red real: usa el transport ASGI de httpx.
    La fixture depende de 'inicializar_db_test' para garantizar que las
    tablas existen antes de cualquier petición.
    """
    from app.main import app
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client
