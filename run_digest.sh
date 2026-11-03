#!/bin/bash
# Startet clarity_digest.py für ein Fachgebiet (Ordner unter specialties/).
#
# Nutzung:  ./run_digest.sh <fachgebiet> [modus] [weitere Optionen für clarity_digest.py]
#
# Modi:
#   check   Parameterdateien prüfen, Journalnamen bei PubMed testen (kostenlos, kein State)
#   test    kleiner Lauf in specialties/<fach>/test_state, State bleibt unverändert (Standard)
#   prod    produktiver Wochenlauf: PDF + Word, State wird fortgeschrieben
#   rebuild Dokumente aus dem letzten Lauf neu bauen (keine PubMed-Abfrage, keine Bewertung)
#
# Beispiele:
#   ./run_digest.sh cardiology check
#   ./run_digest.sh cardiology test
#   ./run_digest.sh cardiology test --provider gemini
#   ./run_digest.sh cardiology prod --lang de --only "Strukturelle Kardiologie"
#   ./run_digest.sh dermatology check

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPECIALTIES_DIR="$SCRIPT_DIR/specialties"
PYTHON="${PYTHON:-python3}"

show_usage() {
    sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
    echo ""
    echo "Vorhandene Fachgebiete:"
    for d in "$SPECIALTIES_DIR"/*/; do
        [ -d "$d" ] && echo "  - $(basename "$d")"
    done
}

SPECIALTY="${1:-}"
if [ -z "$SPECIALTY" ] || [ "$SPECIALTY" = "-h" ] || [ "$SPECIALTY" = "--help" ]; then
    show_usage
    exit 1
fi
shift

if [ ! -d "$SPECIALTIES_DIR/$SPECIALTY" ]; then
    echo "FEHLER: Fachgebiet '$SPECIALTY' nicht gefunden ($SPECIALTIES_DIR/$SPECIALTY)."
    echo "Neues Fachgebiet anlegen mit:  ./specialties/create_digest.sh $SPECIALTY"
    echo ""
    show_usage
    exit 1
fi

MODE="test"
case "${1:-}" in
    check|test|prod|rebuild) MODE="$1"; shift ;;
esac

cd "$SCRIPT_DIR"
echo "Fachgebiet: $SPECIALTY · Modus: $MODE"

case "$MODE" in
    check)
        exec "$PYTHON" clarity_digest.py --specialty "$SPECIALTY" --check-params "$@"
        ;;
    test)
        export STATE_DIR="$SPECIALTIES_DIR/$SPECIALTY/test_state"
        exec "$PYTHON" clarity_digest.py --specialty "$SPECIALTY" \
            --days 7 --no-dedup --limit 40 --pdf --docx "$@"
        ;;
    prod)
        exec "$PYTHON" clarity_digest.py --specialty "$SPECIALTY" \
            --days 7 --pdf --docx --live "$@"
        ;;
    rebuild)
        exec "$PYTHON" clarity_digest.py --specialty "$SPECIALTY" --rebuild --pdf --docx "$@"
        ;;
esac
