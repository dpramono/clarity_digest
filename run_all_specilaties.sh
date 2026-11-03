#!/bin/bash
# Startet run_digest.sh für mehrere Fachgebiete parallel, Ausgabe pro Fach in logs/<fach>.log.
#
# Nutzung:  ./run_all_specilaties.sh [modus] [fachgebiet ...]
#   modus:  check | test | prod | rebuild  (Standard: prod)
#   ohne Fachgebiete: alle unter specialties/, die params/branding.json haben
#
# Beispiele:
#   ./run_all_specilaties.sh                        alle Fächer, produktiv
#   ./run_all_specilaties.sh test                   alle Fächer, Testlauf
#   ./run_all_specilaties.sh check cardiology dermatology

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
mkdir -p logs

MODE="prod"
case "${1:-}" in
    check|test|prod|rebuild) MODE="$1"; shift ;;
esac

if [ "$#" -gt 0 ]; then
    SPECIALTIES=("$@")
else
    SPECIALTIES=()
    for d in specialties/*/; do
        [ -f "$d/params/branding.json" ] && SPECIALTIES+=("$(basename "$d")")
    done
fi

for spec in "${SPECIALTIES[@]}"; do
    if [ ! -f "specialties/$spec/params/branding.json" ]; then
        echo "  !! Überspringe '$spec': keine Parameterdateien (./create_digest.sh $spec)."
        continue
    fi
    ./run_digest.sh "$spec" "$MODE" > "logs/${spec}.log" 2>&1 &
    echo "Gestartet: $spec · $MODE (PID $!, Log: logs/${spec}.log)"
done

wait
echo "Alle gestarteten Fächer abgeschlossen."
