# parte-a — Sistema de Leads Clínica Dental Cerro Alto

Módulo central de backend para la ingestión, validación, enriquecimiento, clasificación con IA y enrutamiento operativo de leads de Meta Ads para la **Clínica Dental Cerro Alto** (Quito, Ecuador).

---

## 🏗️ 1. Arquitectura y Componentes

El sistema implementa una arquitectura orientada a eventos, asíncrona y con persistencia inmediata (**Patrón "Guardar Primero"**), diseñada para garantizar **cero pérdida de datos**, tiempos de respuesta `< 20 ms` y tolerancia total ante fallas externas o picos de tráfico.

```
                           [ Meta Ads / Conector ]
                                      │
                                      ▼  (POST /webhook)
                      ┌────────────────────────────────┐
                      │    FastAPI Webhook Handler     │
                      │  - Extrae raw bytes            │
                      │  - Inserta en SQLite (UNIQUE)  │
                      │  - Responde 200 OK (< 20 ms)   │
                      └───────────────┬────────────────┘
                                      │ (BackgroundTasks)
                                      ▼
                      ┌────────────────────────────────┐
                      │     Pipeline Asíncrono         │
                      │         (app/procesamiento.py) │
                      └───────────────┬────────────────┘
                                      │
           ┌──────────────────────────┼──────────────────────────┐
           ▼                          ▼                          ▼
┌─────────────────────┐    ┌─────────────────────┐    ┌─────────────────────┐
│ 1. Normalización    │    │ 2. Deduplicación    │    │ 3. Enriquecimiento  │
│ (app/normalizacion) │    │ (app/deduplicacion) │    │ (app/pacientes.py)  │
│ - Teléfono E.164    │    │ - Ventana 30 días   │    │ - CRM Cerro Alto    │
│ - Email normalizado │    │ - Orden cronológico │    │ - Token Bucket (30) │
│ - Filtro rechazo    │    │ - Idempotencia DB   │    │ - Desempate familiar│
└─────────────────────┘    └─────────────────────┘    └─────────────────────┘
                                      │
                                      ▼
                      ┌────────────────────────────────┐
                      │ 4. Clasificación de Intención  │
                      │ (app/clasificacion.py)         │
                      │ ├─ Regla 1: Tratamiento activo │
                      │ ├─ Regla 2: Urgencia clínica   │
                      │ ├─ Regla 3: LLM Gemini Flash   │
                      │ └─ Fallback seguro: "tibio"    │
                      └───────────────┬────────────────┘
                                      │
           ┌──────────────────────────┼──────────────────────────┐
           ▼                          ▼                          ▼
┌─────────────────────┐    ┌─────────────────────┐    ┌─────────────────────┐
│ RUTA CALIENTE       │    │ RUTA TIBIO          │    │ RUTA BASURA         │
│ - Alerta Telegram   │    │ - Genera borrador de│    │ - Descarte          │
│ - SLA < 5 minutos   │    │   seguimiento en BD │    │   silencioso        │
│ - Teléfono visible  │    │ - Revisión humana   │    │ - Motivo auditable  │
└─────────────────────┘    └─────────────────────┘    └─────────────────────┘
```

---

## 📁 2. Estructura del Código

```
parte-a/
├── app/
│   ├── __init__.py
│   ├── main.py             # Entry-point FastAPI, ciclo de vida (lifespan) y logging
│   ├── config.py           # Variables de entorno validadas con pydantic-settings
│   ├── database.py         # Motor SQLAlchemy Core + aiosqlite, modo WAL y esquema
│   ├── modelos.py          # Esquemas Pydantic V2 de payloads entrantes con tolerancia de tipos
│   ├── webhook.py          # Endpoint POST /webhook con patrón guardar primero
│   ├── normalizacion.py    # Limpieza E.164 (phonenumbers "EC"), correos y descarte sin contacto
│   ├── deduplicacion.py    # Idempotencia y ventana móvil de 30 días con orden cronológico
│   ├── pacientes.py        # Cliente HTTP asíncrono para el CRM con Token Bucket rate limiter
│   ├── clasificacion.py    # Triaje determinista + Gemini 1.5 Flash + defensa anti-inyección
│   ├── enrutado.py         # Notificaciones Telegram/Email (<5 min SLA) y borradores editables
│   ├── estado.py           # Endpoint GET /leads/estado con filtros ?event_id= y ?desde=
│   ├── reproceso.py        # Detección y reejecución de leads fallidos o huérfanos (>10 min)
│   └── procesamiento.py    # Orquestador del pipeline completo de 6 fases
├── prompts/
│   └── v1_clasificacion.txt# Prompt versionado del LLM con delimitadores seguros
├── tests/                  # 101 tests automatizados con pytest-asyncio
│   ├── conftest.py         # Fixtures de BD aislada, cliente ASGI y mocks deterministas
│   ├── test_etapa1_webhook.py
│   ├── test_etapa2_normalizacion.py
│   ├── test_etapa3_deduplicacion.py
│   ├── test_etapa4_pacientes.py
│   ├── test_etapa5_clasificacion.py
│   ├── test_etapa6_enrutado.py
│   ├── test_etapa7_reproceso.py
│   ├── test_etapa8_estado.py
│   └── test_etapa9_payloads_e2e.py
├── reprocesar.py           # CLI operacional para auditar y recuperar leads (--dry-run, --todos)
├── requirements.txt        # Dependencias de producción y pruebas
├── pytest.ini              # Configuración de pytest (modo asyncio=auto)
├── LEARNING.md             # Bitácora de ingeniería, conceptos y preguntas de entrevista
└── DECISIONES.md           # Registro de 33 supuestos y decisiones arquitectónicas
```

---

## 🗄️ 3. Modelo de Datos y Motor SQLite

### ¿Por qué SQLite con aiosqlite?
1. **Un solo comando para arrancar**: No requiere levantar servidores pesados (PostgreSQL, Redis) para el volumen de la clínica (60 a 150 leads/día).
2. **Concurrencia limpia**: Se configura en arranque con:
   - `PRAGMA journal_mode = WAL;` (Write-Ahead Logging: lecturas y escrituras concurrentes sin bloqueo mutuo).
   - `PRAGMA busy_timeout = 30000;` (Espera de hasta 30 segundos antes de fallar por contención).

### Esquema de la Tabla `leads`
| Campo | Tipo | Restricción | Propósito |
|-------|------|-------------|-----------|
| `id` | Integer | PK, Autoincrement | Identificador numérico interno |
| `event_id` | String | UNIQUE, Not Null, Index | ID de Meta Ads o ID interno (`int_...`) para idempotencia atómica |
| `estado` | String | Not Null | `pendiente` \| `procesado` \| `duplicado` \| `rechazado` \| `fallido` |
| `ruta` | String | Nullable | `caliente` \| `tibio` \| `basura` (SOLO si `estado == 'procesado'`) |
| `raw_payload` | Text | Not Null | JSON original crudo antes de cualquier parseo |
| `telefono_e164` | String | Nullable, Index | Número limpio en formato internacional |
| `email_normalizado` | String | Nullable, Index | Correo en minúsculas y sin espacios |
| `telefono_invalido` | Boolean | Default 0 | Flag de auditoría si el teléfono venía corrupto |
| `email_invalido` | Boolean | Default 0 | Flag de auditoría si el correo venía corrupto |
| `es_paciente_existente` | Boolean | Nullable | `True` si el CRM lo tiene registrado |
| `tiene_tratamiento_activo` | Boolean | Nullable | `True` si tiene tratamientos `pendiente` o `en_curso` |
| `enriquecimiento_pendiente` | Boolean | Default 0 | `True` si el CRM externo falló o hubo timeout |
| `lead_id` | String | Nullable | ID del paciente en el CRM (`pac_...`) |
| `duplicado_de` | String | Nullable | `event_id` original del lead previo que causó la duplicidad |
| `motivo` | Text | Nullable | Explicación auditable (obligatoria en no procesados y basura) |
| `notificacion_enviada` | Boolean | Default 0 | `True` si recepción recibió la alerta exitosamente |
| `notificacion_canal` | String | Nullable | `telegram` o `email` |
| `borrador_seguimiento` | Text | Nullable | Propuesta de respuesta editable generada para leads tibios |
| `created_at` | DateTime | UTC, Default Now | Marca de tiempo de recepción en el webhook |
| `processed_at` | DateTime | UTC, Nullable | Marca de tiempo de finalización del pipeline |

---

## ⚙️ 4. Flujo Detallado por Módulo

### 1. Webhook (`app/webhook.py`)
- Lee los bytes crudos del request sin asumir codificación para garantizar persistencia ante payloads corruptos.
- Asigna un ID interno con prefijo `int_` mediante `uuid4` si el payload no trae `event_id`.
- Inserta con `estado = 'pendiente'`. Si SQLite arroja `IntegrityError`, detecta reintento de red (idempotencia) y responde `200 OK` con `estado: "duplicado"` sin alterar el lead en curso.
- Encola la tarea asíncrona con `BackgroundTasks` y retorna `200 OK` inmediatamente.

### 2. Normalización (`app/normalizacion.py`)
- **Teléfonos**: Utiliza `phonenumbers` de Google con región por defecto `"EC"`. Transforma `0991234567` en `+593991234567`. Respeta números internacionales como `+1 (305)...`.
- **Correos**: Limpia espacios, pasa a minúsculas (`Carla.Mendoza@...` → `carla.mendoza@...`) y valida estructura con regex.
- **Tolerancia a tipos**: Acepta enteros en teléfonos (`593987650021`) y arrays en ciudades (`["Quito"]`).
- **Regla de contacto mínimo**: Si al menos un canal es válido, el lead continúa. Si ambos son inválidos o ausentes, pasa a `rechazado`.

### 3. Deduplicación por Persona (`app/deduplicacion.py`)
- Busca coincidencias de teléfono E.164 o correo normalizado en una ventana móvil de **30 días**.
- **Orden cronológico estricto**: Un lead solo puede ser duplicado de otro que haya llegado estrictamente antes (`created_at < actual` o mismo timestamp con `id < actual`).
- **Excepción de rechazados**: Un lead previo rechazado por datos corruptos NO bloquea a un lead nuevo con datos válidos.
- Si coincide, marca `estado = 'duplicado'`, enlaza `duplicado_de` y detiene el flujo para ahorrar costos de IA y llamadas repetidas de recepción.

### 4. Enriquecimiento CRM (`app/pacientes.py`)
- Conecta con la API de pacientes de Cerro Alto.
- **Rate Limiting**: Algoritmo Token Bucket en memoria limitando el tráfico a 30 req/min por token.
- **Paginación segura**: Consulta tratamientos siguiendo `next_cursor` hasta `null`, con tope de seguridad de 10 páginas.
- **Desempate familiar**: Si varios pacientes comparten teléfono o correo, desempata por coincidencia de palabras del nombre propio.
- **Degradación elegante**: Si el CRM arroja 500, 429 o timeout (3 s), el lead continúa con `enriquecimiento_pendiente = True`.

### 5. Clasificación con IA y Reglas Duras (`app/clasificacion.py`)
1. **Regla de Tratamiento Activo**: Si el CRM reporta tratamientos activos, se clasifica como `caliente` sin llamar a la IA.
2. **Regla de Urgencia Clínica**: Detecta palabras clave de dolor, trauma o absceso dental y fuerza `caliente` en `< 1 ms` (triaje médico prioritario determinista).
3. **LLM (Gemini 1.5 Flash)**: Para determinar intención comercial y descarte de spam:
   - Trunca mensajes a `<= 2000` caracteres preservando los primeros 1200 y los últimos 700 chars.
   - Protección Anti-Prompt-Injection con delimitadores XML `<mensaje_lead>` e instrucciones explícitas de desestimación de comandos.
   - Validación estricta con Pydantic (`ClasificacionLead`).
   - **Regla de Oro**: Fallback seguro a `tibio` ante cualquier error de red, cuota o JSON inválido ("Ante la duda, tibio, nunca basura").
   - **LOPDP estricta**: Jamás se envía nombre, teléfono ni correo a Google.

### 6. Enrutamiento Operativo (`app/enrutado.py`)
- **Caliente**: Envía alerta inmediata por Telegram o Email. Teléfono visible en formato E.164 para que recepción cumpla el **SLA de contacto en < 5 minutos**.
- **Tibio**: Genera un borrador de mensaje personalizado en la base de datos (con saludo por nombre y mención del tratamiento solicitado). No se auto-envía para proteger la reputación del remitente y mantener toque humano.
- **Basura**: Descarte silencioso con motivo registrado en BD. Evita fatiga de alertas.

### 7. Auditoría y Estado (`app/estado.py`)
- `GET /leads/estado?event_id=evt_...`: Devuelve 200 con el objeto o 404 si no existe.
- `GET /leads/estado?desde=2026-11-03T00:00:00Z`: Devuelve lista cronológica de leads recibidos desde esa fecha UTC (400 si la fecha no es ISO 8601).
- **Contrato de campos**: El campo `ruta` SOLO se expone cuando `estado == 'procesado'`. En cualquier otro caso es `null`. `email_normalizado` se expone como `email`.

### 8. Rescate y Reproceso (`app/reproceso.py` y `reprocesar.py`)
- Recupera leads en estado `fallido` o leads en `pendiente` que llevan más de 10 minutos atascados (**huérfanos** por reinicio del servidor).
- CLI profesional con flags `--dry-run`, `--event-id`, `--todos` y `--limite`.

---

## 🔒 5. Privacidad y Seguridad (LOPDP Ecuador)

1. **Enmascaramiento en Logs**: Todos los logs del sistema muestran los teléfonos enmascarados (ej. `+593****4567`).
2. **Zero-PII hacia el LLM**: Al prompt de Gemini únicamente viajan `message`, `service_interest` y `campaign`. No viajan nombres, identificaciones, correos ni teléfonos.
3. **Defensa Anti-Inyección**: Se aíslan los datos de usuario mediante etiquetas cerradas y el código toma las decisiones de enrutamiento independientemente de lo que ordene el usuario.
4. **Protección SQL Injection**: SQLAlchemy Core parametriza todas las consultas de forma nativa.
5. **Aislamiento de Secretos**: Ninguna credencial en duro. `.env` ignorado por git.

---

## 🧪 6. Suite de Pruebas Automatizadas

El proyecto cuenta con **101 pruebas automatizadas** que cubren unit testing, integración con SQLite en memoria y pruebas de estrés E2E:

```bash
# Ejecutar toda la suite
pytest

# Ejecutar con reporte detallado
pytest -v
```

### Distribución de Pruebas por Módulo
- `test_etapa1_webhook.py`: 5 tests (ingestión, raw payload, idempotencia atómica, ID interno).
- `test_etapa2_normalizacion.py`: 21 tests (phonenumbers, E.164, casos Ecuador/EEUU, números falsos, emails, rechazo sin contacto).
- `test_etapa3_deduplicacion.py`: 9 tests (ventana 30 días, orden cronológico estricto, excepción de rechazados, concurrencia).
- `test_etapa4_pacientes.py`: 15 tests (Token Bucket, paginación cursor, desempate familiar, degradación elegante 429/500/timeout).
- `test_etapa5_clasificacion.py`: 15 tests (triaje clínico de dolor, tratamientos activos, prompt injection, fallback a tibio, validación Pydantic).
- `test_etapa6_enrutado.py`: 13 tests (SLA < 5 min, Telegram/Email mock, borradores de seguimiento, descarte silencioso).
- `test_etapa7_reproceso.py`: 9 tests (cálculo de huérfanos > 10 min, restablecimiento atómico a pendiente, script reprocesar.py).
- `test_etapa8_estado.py`: 11 tests (búsqueda por event_id, filtro desde ISO UTC, ruta nula en no procesados, mapeo de email).
- `test_etapa9_payloads_e2e.py`: 3 tests integrales:
  - Ejecución secuencial fiel de los 10 archivos `.json` de `payloads/`.
  - Prueba de ráfaga publicitaria concurrente (20 leads simultáneos con `asyncio.gather`).
  - Verificación estricta de base de datos e invariantes de negocio.
