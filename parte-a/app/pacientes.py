# pacientes.py — Cliente HTTP para la API de pacientes de Clínica Dental Cerro Alto.
#
# Por qué este módulo:
#   - Conecta con el CRM externo para verificar si el lead ya es paciente
#     y si tiene tratamientos activos o pendientes.
#   - Esto define la ruta CALIENTE (prioridad inmediata si tiene tratamiento activo).
#
# Reglas de robustez del enunciado:
#   1. Rate limit estricto: máximo 30 solicitudes por minuto por token.
#   2. Timeout corto (3 segundos) para no demorar el pipeline asíncrono.
#   3. En 429: respetar Retry-After si es razonable o degradar elegantemente.
#   4. Paginación de 3 elementos por página: seguir next_cursor hasta que sea null.
#   5. Coincidencias múltiples: una familia puede compartir teléfono o correo.
#   6. Degradación elegante: si la API falla o cae, el lead NUNCA se descarta;
#      sigue con enriquecimiento_pendiente = True y se alerta a recepción.

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class ResultadoEnriquecimiento:
    """Resultado del proceso de consulta a la API de pacientes."""
    es_paciente_existente: bool = False
    tiene_tratamiento_activo: bool = False
    lead_id: Optional[str] = None
    enriquecimiento_pendiente: bool = False
    motivo_error: Optional[str] = None


class RateLimiterTokenBucket:
    """
    Limitador de velocidad Token Bucket en memoria para respetar la cuota
    de 30 solicitudes por minuto hacia la API de pacientes.
    
    Por qué en memoria:
      Cumple el requisito de 'un solo comando para arrancar' sin necesidad
      de un servidor Redis externo.
    """

    def __init__(self, max_requests: int = 30, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Bloquea hasta que haya un cupo disponible dentro de la ventana de 60 segundos."""
        async with self._lock:
            while True:
                ahora = time.monotonic()
                # Eliminar marcas de tiempo fuera de la ventana de 60s
                self._timestamps = [
                    t for t in self._timestamps if ahora - t < self.window_seconds
                ]
                if len(self._timestamps) < self.max_requests:
                    self._timestamps.append(ahora)
                    return
                # Esperar hasta que el registro más antiguo expire
                tiempo_espera = self.window_seconds - (ahora - self._timestamps[0]) + 0.05
                logger.warning(
                    "Rate limit alcanzado (30 req/min). Esperando %.2f segundos...",
                    tiempo_espera,
                )
                await asyncio.sleep(tiempo_espera)


# Instancia global del rate limiter para compartir el límite entre todas las corrutinas
rate_limiter = RateLimiterTokenBucket(max_requests=30, window_seconds=60.0)


def evaluar_tratamientos_activos(tratamientos: list[dict]) -> bool:
    """
    Determina si el paciente tiene al menos un tratamiento activo.
    
    Estados considerados activos:
        - 'pendiente'
        - 'en_curso'
    
    Estados no activos:
        - 'completado'
        - 'cancelado'
    """
    if not tratamientos:
        return False

    estados_activos = {"pendiente", "en_curso"}

    for t in tratamientos:
        estado = str(t.get("estado", "")).strip().lower()
        if estado in estados_activos:
            return True

    return False


def seleccionar_paciente_coincidente(
    pacientes: list[dict],
    nombre_lead: Optional[str],
) -> Optional[dict]:
    """
    Si la búsqueda devuelve varios pacientes (ej. una familia que comparte
    teléfono o correo), intenta seleccionar el que mejor coincida por nombre
    contando palabras en común (nombre de pila y apellidos).
    Si no hay coincidencia clara o solo hay 1, devuelve el primero.
    """
    if not pacientes:
        return None

    if len(pacientes) == 1:
        return pacientes[0]

    # Varios pacientes comparten contacto: buscar la mayor coincidencia de palabras
    if nombre_lead and nombre_lead.strip():
        tokens_lead = set(w for w in nombre_lead.strip().lower().split() if len(w) > 1)
        mejor_paciente = None
        max_coincidencias = 0

        for p in pacientes:
            p_nombre = str(
                p.get("nombre_completo") or p.get("nombre") or p.get("full_name") or ""
            ).lower()
            tokens_p = set(w for w in p_nombre.split() if len(w) > 1)
            coincidencias = len(tokens_lead.intersection(tokens_p))
            if coincidencias > max_coincidencias:
                max_coincidencias = coincidencias
                mejor_paciente = p

        if mejor_paciente and max_coincidencias > 0:
            logger.info(
                "Coincidencia múltiple resuelta por nombre: '%s' -> paciente_id=%s (score=%s)",
                nombre_lead,
                mejor_paciente.get("id"),
                max_coincidencias,
            )
            return mejor_paciente

    # Si ninguno coincide o el lead no trajo nombre, tomar el primero
    return pacientes[0]


class ClientePacientesAPI:
    """
    Cliente HTTP asíncrono para interactuar con la API de pacientes de terceros.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout_seconds: float = 3.0,
    ):
        self.base_url = (
            base_url if base_url is not None else settings.API_PACIENTES_URL
        ).rstrip("/")
        self.token = token if token is not None else settings.API_PACIENTES_TOKEN
        self.timeout = timeout_seconds

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _get(self, endpoint: str, params: Optional[dict] = None) -> httpx.Response:
        """Realiza una petición GET pasando antes por el Rate Limiter."""
        await rate_limiter.acquire()
        url = f"{self.base_url}{endpoint}"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers(), params=params)
            # Manejo de 429 con Retry-After
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                espera = int(retry_after) if (retry_after and retry_after.isdigit()) else 2
                if espera <= 3:
                    logger.warning("Recibido 429 de API pacientes. Reintentando tras %ss...", espera)
                    await asyncio.sleep(espera)
                    return await client.get(url, headers=self._headers(), params=params)
            return resp

    async def buscar_paciente(
        self,
        telefono_e164: Optional[str],
        email: Optional[str],
        nombre_lead: Optional[str] = None,
    ) -> tuple[Optional[dict], Optional[str]]:
        """
        Consulta GET /v1/pacientes?telefono=...&email=...
        
        Returns:
            (paciente_dict_o_None, error_str_o_None)
        """
        params = {}
        if telefono_e164:
            params["telefono"] = telefono_e164
        if email:
            params["email"] = email

        if not params:
            return None, None

        try:
            resp = await self._get("/v1/pacientes", params=params)

            if resp.status_code == 200:
                data = resp.json().get("data", [])
                paciente = seleccionar_paciente_coincidente(data, nombre_lead)
                return paciente, None

            # Errores 4xx o 5xx del CRM
            error_msg = f"HTTP {resp.status_code}: {resp.text[:100]}"
            logger.warning("Error buscando paciente | %s", error_msg)
            return None, error_msg

        except httpx.TimeoutException:
            logger.warning("Timeout al consultar API pacientes (/v1/pacientes)")
            return None, "Timeout en API pacientes"
        except Exception as exc:
            logger.warning("Fallo de conexión con API pacientes: %s", exc)
            return None, f"Error de red: {exc}"

    async def obtener_tratamientos(self, paciente_id: str) -> tuple[list[dict], Optional[str]]:
        """
        Consulta GET /v1/pacientes/{id}/tratamientos siguiendo la paginación
        de 3 elementos por página con 'next_cursor' hasta que sea null.
        """
        todos_tratamientos: list[dict] = []
        cursor: Optional[str] = None
        max_paginas = 10  # Salvaguarda contra bucles infinitos de terceros

        for _ in range(max_paginas):
            params = {}
            if cursor:
                params["cursor"] = cursor

            try:
                resp = await self._get(f"/v1/pacientes/{paciente_id}/tratamientos", params=params)

                if resp.status_code != 200:
                    return todos_tratamientos, f"HTTP {resp.status_code}"

                body = resp.json()
                items = body.get("data", [])
                todos_tratamientos.extend(items)

                # Paginación: puede venir en pagination.next_cursor o next_cursor directo
                pag = body.get("pagination")
                if isinstance(pag, dict):
                    cursor = pag.get("next_cursor")
                else:
                    cursor = body.get("next_cursor")

                # Si no hay más páginas, terminamos
                if not cursor:
                    break

            except httpx.TimeoutException:
                logger.warning("Timeout al paginar tratamientos del paciente %s", paciente_id)
                return todos_tratamientos, "Timeout al paginar tratamientos"
            except Exception as exc:
                logger.warning("Error al paginar tratamientos: %s", exc)
                return todos_tratamientos, str(exc)

        return todos_tratamientos, None


async def enriquecer_lead(
    telefono_e164: Optional[str],
    email: Optional[str],
    nombre_lead: Optional[str] = None,
    cliente_override: Optional[ClientePacientesAPI] = None,
) -> ResultadoEnriquecimiento:
    """
    Función principal de enriquecimiento llamada por el pipeline.
    
    Aplica degradación elegante:
      Si la API no está configurada, o está caída, o da timeout,
      nunca tumba el proceso; retorna enriquecimiento_pendiente=True.
    """
    cliente = cliente_override or ClientePacientesAPI()

    # Si la URL no está configurada (ej. en desarrollo local sin mock)
    if not cliente.base_url:
        logger.info("API de pacientes no configurada en .env; marcando enriquecimiento_pendiente=True")
        return ResultadoEnriquecimiento(
            es_paciente_existente=False,
            tiene_tratamiento_activo=False,
            lead_id=None,
            enriquecimiento_pendiente=True,
            motivo_error="API_PACIENTES_URL no configurada",
        )

    # 1. Buscar si el paciente ya existe en el CRM
    paciente, error = await cliente.buscar_paciente(telefono_e164, email, nombre_lead)

    if error:
        # Falló la comunicación: degradación elegante
        return ResultadoEnriquecimiento(
            es_paciente_existente=False,
            tiene_tratamiento_activo=False,
            lead_id=None,
            enriquecimiento_pendiente=True,
            motivo_error=error,
        )

    if not paciente:
        # Consulta exitosa pero el paciente es nuevo
        return ResultadoEnriquecimiento(
            es_paciente_existente=False,
            tiene_tratamiento_activo=False,
            lead_id=None,
            enriquecimiento_pendiente=False,
        )

    # 2. Paciente encontrado: obtener su historial de tratamientos con paginación
    paciente_id = str(paciente.get("id"))
    tratamientos, error_tratamientos = await cliente.obtener_tratamientos(paciente_id)

    tiene_activo = evaluar_tratamientos_activos(tratamientos)

    return ResultadoEnriquecimiento(
        es_paciente_existente=True,
        tiene_tratamiento_activo=tiene_activo,
        lead_id=paciente_id,
        enriquecimiento_pendiente=bool(error_tratamientos),
        motivo_error=error_tratamientos,
    )
