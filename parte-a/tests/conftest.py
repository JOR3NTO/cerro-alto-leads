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

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine

import app.database as db_module
import app.webhook as webhook_module
import app.procesamiento as procesamiento_module


# ── Engine en memoria (se crea una sola vez por sesión) ──────────────────────
# Creamos el engine antes de las fixtures para que el módulo ya lo use.
_test_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    echo=False,
    connect_args={"check_same_thread": False},
)

# Reemplazamos el engine en todos los módulos que lo importan
db_module.engine = _test_engine
webhook_module.engine = _test_engine
procesamiento_module.engine = _test_engine


@pytest_asyncio.fixture(scope="session", autouse=True)
async def inicializar_db_test():
    """
    Crea las tablas en la base de datos en memoria.
    Se ejecuta UNA vez antes de todos los tests de la sesión.
    'autouse=True' hace que se aplique automáticamente sin que los tests
    la pidan explícitamente.
    """
    async with _test_engine.begin() as conn:
        await conn.run_sync(db_module.metadata.create_all, checkfirst=True)

    yield  # Los tests corren aquí

    # Limpieza al final de la sesión
    await _test_engine.dispose()


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
