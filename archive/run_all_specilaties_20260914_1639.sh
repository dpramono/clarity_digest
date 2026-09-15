#!/bin/bash
# Fachgebiete parallel starten, Ausgaben pro Fachgebiet protokolliert.
#
# Nutzung:
#   ./run_all_specialties.sh                                   -> alle Fachgebiete (Default-Liste unten)
#   ./run_all_specialties.sh cardiology_digest dermatology_digest   -> nur diese zwei
#
cd "$(dirname "$0")"   # sicherstellen, dass wir im clarity/-Ordner stehen
mkdir -p logs

# Default-Liste, falls keine Fachgebiete als Argumente uebergeben werden
DEFAULT_SPECIALTIES=(cardiology_digest dermatology_digest pulmonology_digest internal_digest)

if [ "$#" -gt 0 ]; then
    SPECIALTIES=("$@")
else
    SPECIALTIES=("${DEFAULT_SPECIALTIES[@]}")
fi

for spec in "${SPECIALTIES[@]}"; do
    if [ ! -d "$spec" ]; then
        echo "  !! Ueberspringe '$spec': Ordner nicht gefunden."
        continue
    fi
    python3 gemini_clarity_digest.py --specialty-dir "$spec" \
        > "logs/${spec}.log" 2>&1 &
    echo "Gestartet: $spec (PID $!)"
done

wait
echo "Alle gestarteten Faecher abgeschlossen."