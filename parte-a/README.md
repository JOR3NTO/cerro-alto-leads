# parte-a — Sistema de leads Clínica Dental Cerro Alto

Este directorio contiene **todo el código** del sistema.

## Estructura

```
parte-a/
├── app/                # Módulos de la aplicación
│   ├── __init__.py
│   ├── main.py         # Entry-point FastAPI
│   ├── webhook.py      # Endpoint POST /webhook/leads
│   ├── database.py     # Esquema SQLite y acceso
│   ├── normalizacion.py
│   ├── deduplicacion.py
│   ├── enriquecimiento.py
│   ├── clasificacion.py
│   ├── enrutado.py
│   ├── estado.py       # Endpoint GET /leads/estado
│   └── errores.py      # Manejo central de errores y avisos
├── prompts/            # Prompts versionados del LLM
├── tests/              # Tests y script de reproceso
├── reprocesar.py       # Script para reprocesar leads fallidos
├── LEARNING.md         # Resumen de aprendizajes por etapa
└── DECISIONES.md       # Decisiones, supuestos y recortes
```

## Inicio rápido

```bash
# (se completa en la Etapa 10)
```
