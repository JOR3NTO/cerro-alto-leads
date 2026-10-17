# LEARNING.md — Bitácora de aprendizajes

> Úsalo para preparar tu video y la entrevista. Cada etapa tiene: qué aprendiste, por qué importa, y qué podrías explicar en 30 segundos.

---

## Etapa 0 — Preparación y análisis de payloads

### Conceptos nuevos
- **Payload**: el JSON que envía el conector de Meta Ads a tu servidor. Es como el "sobre" que trae la información del lead.
- **E.164**: formato internacional de teléfono. Ejemplo: `+593991234567`. Es el único formato que la API de pacientes acepta exactamente.
- **Idempotencia**: si el mismo evento llega dos veces, el sistema produce el mismo resultado (no crea duplicados). Fundamental porque el conector reintenta hasta 3 veces.
- **event_id**: identificador único del evento. Funciona como llave para detectar reintentos del conector vs. duplicados reales.

### Decisiones de esta etapa
- Ver DECISIONES.md para la lista completa.
- El `.gitignore` ya existía pero incompleto: le faltaban `*.db`, `*.db-shm`, `*.db-wal` (la base SQLite con datos reales) y los artefactos de pytest.

### Trampas detectadas en los payloads
| Evento | Trampa | Por qué importa |
|--------|--------|-----------------|
| 01 y 02 | Mismo `event_id` | Prueba de idempotencia: el conector reintentó |
| 03 | Mismo lead que 01/02 pero `event_id` distinto, teléfono con espacios, correo con mayúscula y espacio al final | Prueba de deduplicación por persona (ventana 30 días) |
| 04 | Teléfono americano (+1 305…), mensaje en portugués | ¿Se acepta? Sí si hay contacto válido; la ruta la decide el LLM |
| 05 | Sin campo `phone` (clave ausente), nombre vacío `""` | Un medio de contacto basta; nombre vacío no descarta |
| 06 | `phone` es **número entero** (no string), `city` es **array** | El esquema Pydantic debe tolerar coerción o marcar inválido |
| 07 | Mensaje de ~5 KB (repetido muchas veces) | Riesgo de prompt gigante al LLM; hay que truncar antes de enviar |
| 08 | Spam evidente: "GANA DINERO YA", link en city, teléfono 1111111111 | Ruta BASURA con motivo auditable |
| 09 | Mensaje de una sola palabra: "info" | Tibio por defecto (no hay urgencia, pero interés real) |
| 10 | **Prompt injection** en el mensaje del usuario | La ruta la decide el código, no el texto del usuario |

### Preguntas de repaso (respondidas después de la etapa 0)
*(se llenan en la siguiente sección)*

---

## Etapa 1 — Webhook que guarda primero

### Conceptos nuevos

| Concepto | Qué es | Por qué importa aquí |
|----------|--------|----------------------|
| **Patrón "guardar primero"** | Guardas el dato en DB *antes* de procesarlo y respondes 200 inmediatamente | Si procesas antes de responder, el conector ve timeout y reintenta 3 veces innecesariamente |
| **BackgroundTasks (FastAPI)** | Función que se ejecuta *después* de enviar la respuesta HTTP, en el mismo proceso | Más simple que Celery/Redis; el proceso vive mientras corre la tarea |
| **IntegrityError** | Error de SQLAlchemy cuando violas una restricción UNIQUE | Lo capturamos para detectar el mismo `event_id` dos veces sin hacer SELECT previo |
| **Condición de carrera (race condition)** | Cuando dos procesos leen "no existe" al mismo tiempo y los dos intentan insertar | La restricción UNIQUE en la DB lo resuelve atómicamente; un "si no existe, inserto" en Python no |
| **ASGI** | Interfaz estándar entre Python y servidores web asíncronos | FastAPI es ASGI; `httpx.AsyncClient(transport=ASGITransport(app))` prueba sin levantar red |
| **WAL (Write-Ahead Log)** | Modo de SQLite donde las escrituras van a un log temporal | Permite lecturas simultáneas mientras hay una escritura; necesario con varios workers |
| **pydantic-settings** | Librería que lee variables de entorno con validación de tipos | Si falta una variable crítica, la app falla al arrancar con un mensaje claro |

### Decisiones de esta etapa
- El engine SQLite usa `aiosqlite` (async) para no bloquear el event loop de FastAPI.
- Los modelos Pydantic en `modelos.py` son **permisivos** (todo `Optional`) porque la validación real es en Etapa 2. Aquí solo queremos no crashear.
- El `conftest.py` reemplaza el engine de producción por uno en memoria (`:memory:`) para tests limpios.

### Trampas de esta etapa
| Trampa | Solución |
|--------|----------|
| `tail` no existe en PowerShell | Usar `Select-Object -Last 5` o simplemente ver el log completo |
| `scope='session'` en fixtures async requiere `pytest-asyncio >= 0.21` | Verifica la versión instalada |
| El `BackgroundTasks` de FastAPI **no** garantiza que la tarea termine si el servidor se apaga a mitad | En Etapa 7 agregamos el estado `fallido` y el script de reproceso para cubrir esto |

### Para explicar en 30 segundos
> *"El webhook hace tres cosas en orden: guarda el JSON crudo en SQLite, responde 200 al conector, y solo entonces dispara el procesamiento en background. La clave es el UNIQUE en event_id: si el conector reintenta con el mismo ID, SQLite lanza IntegrityError y nosotros respondemos 200 sin crear duplicados. No hay condición de carrera porque la base de datos lo resuelve atómicamente."*
