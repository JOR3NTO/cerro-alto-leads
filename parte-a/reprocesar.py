#!/usr/bin/env python3
"""reprocesar.py — CLI para reprocesar leads fallidos o huérfanos.

Uso:
    python reprocesar.py --event-id <event_id>
    python reprocesar.py --todos
    python reprocesar.py --dry-run
    python reprocesar.py --limite 10 --solo-fallidos

Ejemplos:
    # Ver qué leads necesitan reproceso sin modificarlos
    python reprocesar.py --dry-run

    # Reprocesar un lead específico
    python reprocesar.py --event-id evt_01JB7Q3M2X4K8R9T5W6Y

    # Reprocesar todos los leads pendientes de rescate
    python reprocesar.py --todos
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Aseguramos que la carpeta parte-a esté en el path de Python
PARTE_A_DIR = Path(__file__).resolve().parent
if str(PARTE_A_DIR) not in sys.path:
    sys.path.insert(0, str(PARTE_A_DIR))

from app.database import close_db, engine, init_db
from app.reproceso import buscar_leads_reprocesables, reprocesar_lote


async def main_async(args: argparse.Namespace) -> int:
    await init_db()

    try:
        # 1. Buscar candidatos a reproceso
        candidatos = await buscar_leads_reprocesables(
            engine=engine,
            event_id=args.event_id,
            incluir_huerfanos=not args.solo_fallidos,
            min_minutos_huerfano=args.minutos_huerfano,
            limite=args.limite,
        )

        if not candidatos:
            print("✅ No hay leads que requieran reprocesamiento en este momento.")
            return 0

        # Filtrar solo los que son elegibles
        elegibles = [c for c in candidatos if c["elegible"]]

        print(f"\n📋 Se encontraron {len(candidatos)} lead(s) analizado(s):")
        print(f"   • {len(elegibles)} elegible(s) para reproceso")
        print(f"   • {len(candidatos) - len(elegibles)} no elegible(s)\n")

        print(f"{'EVENT ID':<26} {'ESTADO':<12} {'ANTIGÜEDAD':<14} {'ELEGIBLE':<10} {'MOTIVO / DETALLE'}")
        print("-" * 80)
        for c in candidatos:
            eleg_txt = "SÍ" if c["elegible"] else "NO"
            antig_txt = f"{c['antiguedad_min']:.1f} min"
            print(f"{c['event_id']:<26} {c['estado_actual']:<12} {antig_txt:<14} {eleg_txt:<10} {c['razon_elegibilidad']}")
        print("-" * 80)

        # Si fue modo seco (dry-run), terminar aquí
        if args.dry_run:
            print("\n🔍 Modo --dry-run activado: No se aplicaron modificaciones.")
            return 0

        if not elegibles:
            print("\n⚠️ Ningún lead cumple las condiciones para ser reprocesado.")
            return 0

        # Si no especificó --todos ni --event-id, advertir al usuario
        if not args.todos and not args.event_id:
            print("\n⚠️ Debe especificar --todos o --event-id para ejecutar el reproceso.")
            print("   Use: python reprocesar.py --todos")
            return 1

        # 2. Ejecutar reproceso
        ids_a_reprocesar = [c["event_id"] for c in elegibles]
        print(f"\n🚀 Iniciando reprocesamiento de {len(ids_a_reprocesar)} lead(s)...\n")

        resumen = await reprocesar_lote(engine, ids_a_reprocesar)

        print("\n📊 RESULTADOS DEL REPROCESO:")
        print(f"{'EVENT ID':<26} {'RESULTADO':<10} {'ESTADO FINAL':<14} {'RUTA':<10} {'DETALLE'}")
        print("-" * 80)
        for d in resumen["detalles"]:
            estado_txt = "EXITOSO" if d["exito"] else "FALLIDO"
            final_estado = d["datos"].get("estado", "desconocido")
            final_ruta = d["datos"].get("ruta") or "-"
            print(f"{d['event_id']:<26} {estado_txt:<10} {final_estado:<14} {final_ruta:<10} {d['mensaje']}")
        print("-" * 80)

        print(f"\nResumen final: {resumen['exitosos']} exitoso(s), {resumen['fallidos']} fallido(s) de {resumen['total']} totales.")
        return 0 if resumen["fallidos"] == 0 else 1

    finally:
        await close_db()


def main():
    parser = argparse.ArgumentParser(
        description="Script de rescate y reprocesamiento de leads fallidos o huérfanos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--event-id", type=str, help="Reprocesa un lead específico por su event_id.")
    parser.add_argument("--todos", action="store_true", help="Reprocesa todos los leads elegibles encontrados.")
    parser.add_argument("--limite", type=int, default=50, help="Límite máximo de leads a reprocesar (default: 50).")
    parser.add_argument("--solo-fallidos", action="store_true", help="Solo reprocesa estado 'fallido', ignora pendientes huérfanos.")
    parser.add_argument(
        "--minutos-huerfano",
        type=float,
        default=10.0,
        help="Minutos de antigüedad para considerar un lead pendiente como huérfano (default: 10.0).",
    )
    parser.add_argument("--dry-run", "--seco", dest="dry_run", action="store_true", help="Muestra los leads a reprocesar sin ejecutar cambios.")

    args = parser.parse_args()
    codigo_salida = asyncio.run(main_async(args))
    sys.exit(codigo_salida)


if __name__ == "__main__":
    main()
