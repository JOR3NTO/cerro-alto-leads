# DECISIONES.md — Supuestos, recortes y correcciones

> Cada vez que tomamos una decisión de diseño, asumimos algo, o recortamos alcance, lo anotamos aquí con su justificación. Sirve para la entrevista y para no olvidar por qué hicimos las cosas así.

---

## Etapa 0 — Preparación

### Supuestos tomados

| # | Supuesto | Justificación |
|---|----------|---------------|
| S-01 | El `phone` de evento_06 viene como entero JSON (`593987650021`). Lo tratamos como inválido porque el conector lo documentó como string; si llega entero, pydantic intentará coercionarlo a string y lo procesamos. | El enunciado dice "reenvía lo que escribió la persona"; un entero no es lo que escribió, es un bug del form. Aun así no descartamos el lead (tiene email válido). |
| S-02 | El campo `city` de evento_06 viene como array `["Quito"]`. Extraemos el primer elemento y lo usamos como string; no es un campo crítico para las reglas de negocio. | `city` no afecta ninguna regla de negocio; no vale rechazar el lead por esto. |
| S-03 | El teléfono de evento_04 es americano (+1 305…). Es técnicamente válido en E.164. Lo normalizamos y procesamos; no es "inválido", solo no es ecuatoriano. | El enunciado solo pide E.164 válido, no que sea ecuatoriano. |
| S-04 | El mensaje de evento_07 (~5 KB, texto repetido) se trunca a 2 000 caracteres antes de enviarlo al LLM, con una nota en el prompt. | Evitar facturas enormes y timeouts. El contenido relevante está al principio y al final. |
| S-05 | Para evento_05 (sin campo `phone`), campo ausente ≠ campo inválido; se procesa igual que phone = null. | El enunciado dice "Si uno es inválido, se procesa con el otro". Ausente equivale a no válido. |
| S-06 | `enviar_payloads.py` manda los archivos en orden alfabético (evento_01 → evento_10). El sistema debe soportar que evento_01 y evento_02 lleguen casi al mismo tiempo (el script espera 1 s entre ellos). | Confirma que la restricción UNIQUE en event_id + la ventana de 30 días deben funcionar bajo concurrencia ligera. |

### Recortes de alcance (orden de corte definido en el enunciado)

| # | Recorte | Razón |
|---|---------|-------|
| R-01 | La alarma de silencio (detectar que llevan X horas sin lead) se implementa SOLO si sobra tiempo. | Es el primero en la lista de corte del enunciado. |
| R-02 | El canal de respaldo para notificaciones (si falla Telegram, usar correo) se implementa SOLO si sobra tiempo. | Es el segundo en la lista de corte. |

### Análisis de rarezas por payload (tabla completa)

| Evento | event_id | Teléfono | Correo | Nombre | Mensaje | Campos extra | Rareza principal |
|--------|----------|----------|--------|--------|---------|-------------|-----------------|
| 01 | `evt_01JB7Q3M2X4K8R9T5W6Y` | `0991234567` (EC, sin código) | válido | Carla Mendoza | Quiere agendar jueves | — | Lead normal, referencia base |
| 02 | **mismo** `evt_01JB7Q3M2X4K8R9T5W6Y` | idem | idem | idem | idem | — | **Reintento del conector** (idempotencia) |
| 03 | distinto | `+593 99 123 4567` (con espacios) | `Carla.Mendoza@example.com ` (mayúsc. + espacio) | `carla mendoza` (minúsc.) | "Perdón, llené otra vez" | — | **Duplicado por persona** (30 días) |
| 04 | distinto | `+1 (305) 555-0142` (EEUU) | válido | João Pedro | En portugués, solo averiguando | — | Teléfono extranjero válido; tibio |
| 05 | distinto | **ausente** (campo faltante) | válido | `""` (vacío) | Emojis, precio, tarjeta | — | Sin nombre ni teléfono; procesa con correo |
| 06 | distinto | `593987650021` (**entero**) | válido | Rosa Elena | Neutro, valoración | `consent`, `tracking` | Tipo incorrecto; `city` es array |
| 07 | distinto | `098 765 4321` (EC, espacios) | válido | Daniela Vásconez | **~5 KB**, texto repetido, pide cita urgente | — | Mensaje gigante; alineadores → caliente |
| 08 | distinto | `1111111111` (claramente falso) | `promo@ganadinero.example` | `GANA DINERO YA` | Spam de inversión | — | **Basura** evidente |
| 09 | distinto | `0998877665` (EC) | válido | Jorge Paredes | `"info"` (una palabra) | — | Mensaje mínimo; tibio |
| 10 | distinto | `0987 111 222` (EC, espacios) | válido | Andrés Lozada | **Prompt injection** explícito | — | Ruta debe venir del código, no del texto |

### LLM — decisión de proveedor

**Opciones evaluadas:**

| Proveedor | Capa gratuita | Latencia | Límites | Ventaja | Desventaja |
|-----------|--------------|----------|---------|---------|------------|
| **Gemini Flash 1.5** | ✅ 15 req/min, 1M tokens/día | ~1-2 s | Generosos | JSON nativo (`response_mime_type`), sin tarjeta | Requiere cuenta Google |
| Groq (Llama 3.1 8B) | ✅ ~14 400 req/día | <0.5 s | 30 req/min | Muy rápido, sin tarjeta | Sin JSON mode; más alucinaciones |
| OpenRouter | ✅ $1 crédito gratis | variable | variable | Muchos modelos | Requiere tarjeta para algunos |

**Elegido: Gemini Flash 1.5** porque tiene `response_mime_type: application/json` que fuerza salida JSON, es el más fácil de validar con esquema estricto, y la latencia es aceptable para nuestro flujo asíncrono.

> **Pendiente de respuesta del usuario**: confirmar si tiene cuenta Google Cloud / Google AI Studio para la API key de Gemini.

---

## Etapa 1 — Webhook que guarda primero

### Supuestos tomados

| # | Supuesto | Justificación |
|---|----------|---------------|
| S-07 | El `event_id` ausente (o no parseable) se trata igual que el de evento_09 descrito en la tabla de payloads: se asigna un id interno con prefijo `int_` para que aparezca en `GET /leads/estado?desde=`. | El enunciado dice explícitamente "asígnales un id interno". |
| S-08 | En tests usamos SQLite `:memory:` en lugar de un archivo `.db` real. El engine se reemplaza en `conftest.py` antes de que cualquier test corra. | Así cada corrida de pytest empieza con DB limpia y no contamina datos reales. |
| S-09 | `BackgroundTasks` de FastAPI es suficiente para este caso; no usamos Celery/Redis. | El enunciado pide "un solo comando para arrancar"; Redis requiere un servidor separado. La limitación (la tarea puede perderse si el proceso muere) se mitiga en Etapa 7 con el estado `fallido` y el script `reprocesar.py`. |

### Decisiones de arquitectura

| Decisión | Alternativa descartada | Razón |
|----------|----------------------|-------|
| SQLAlchemy Core (no ORM) | ORM con modelos declarativos | Con tablas simples el ORM agrega complejidad sin beneficio visible |
| `aiosqlite` (async driver) | `sqlite3` síncrono | FastAPI es async; un driver síncrono bloquea el event loop |
| Leer raw bytes primero, parsear después | Dejar que FastAPI parsee automáticamente con `Body(...)` | Si el JSON viene roto, FastAPI devolvería 422 automáticamente — violando la regla de "nunca 4xx por dato malo del lead" |
| `IntegrityError` para idempotencia | SELECT + INSERT condicional | SELECT + INSERT tiene condición de carrera bajo paralelismo; UNIQUE + IntegrityError es atómico |
