# config.py — Centraliza la lectura de variables de entorno.
# Usamos pydantic-settings para que cualquier variable faltante lance un error claro
# al arrancar, en lugar de fallar misteriosamente a mitad del procesamiento.

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Lee el archivo .env del directorio raíz del proyecto.
    Cada campo corresponde a una variable de entorno.
    Si una variable marcada como requerida (sin default) no existe, la app
    no arranca y muestra exactamente qué falta — mucho mejor que un KeyError.
    """

    # ── Base de datos ─────────────────────────────────────────────────────────
    # La URL usa el formato de SQLAlchemy/aiosqlite:
    # "sqlite+aiosqlite:///ruta/al/archivo.db"
    DATABASE_URL: str = "sqlite+aiosqlite:///parte-a/leads.db"

    # ── API de Pacientes ──────────────────────────────────────────────────────
    API_PACIENTES_URL: str = ""
    API_PACIENTES_TOKEN: str = ""

    # ── LLM ───────────────────────────────────────────────────────────────────
    LLM_PROVIDER: str = "gemini"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "gemini-1.5-flash"

    # ── Notificaciones ────────────────────────────────────────────────────────
    NOTIF_CANAL: str = "telegram"
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    EMAIL_HOST: str = "smtp.gmail.com"
    EMAIL_PORT: int = 587
    EMAIL_USER: str = ""
    EMAIL_PASSWORD: str = ""
    EMAIL_DESTINO: str = ""

    # ── Servidor ──────────────────────────────────────────────────────────────
    APP_PORT: int = 8000

    model_config = SettingsConfigDict(
        # Busca el .env dos niveles arriba de parte-a/app/ → raíz del repo
        env_file="../../.env",
        env_file_encoding="utf-8",
        # Permite variables extra en el .env sin que lancen error
        extra="ignore",
    )


# Instancia única (singleton) que toda la app importa.
# Al importar este módulo se carga el .env una sola vez.
settings = Settings()
