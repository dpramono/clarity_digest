#!/bin/bash
# Legt ein neues Fachgebiet unter specialties/<fachgebiet>/ an.
#
# Nutzung:  ./specialties/create_digest.sh <fachgebiet> [vorlage]
#   vorlage: bestehendes Fachgebiet, dessen params/ als Ausgangspunkt kopiert wird
#            (Standard: cardiology). Mit "-" wird nur die leere Struktur angelegt.
#
# Beispiele:
#   ./specialties/create_digest.sh dermatology
#   ./specialties/create_digest.sh oncology -

set -euo pipefail

SPECIALTIES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

show_usage() {
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
}

SPECIALTY="${1:-}"
if [ -z "$SPECIALTY" ]; then
    show_usage
    read -p "Fachgebiet jetzt eingeben (z.B. dermatology): " SPECIALTY
    [ -z "$SPECIALTY" ] && { echo "--> Keine Eingabe. Abbruch."; exit 1; }
fi
TEMPLATE="${2:-cardiology}"

TARGET="$SPECIALTIES_DIR/$SPECIALTY"
mkdir -p "$TARGET/params/prompts" "$TARGET/refs" "$TARGET/output"

if [ "$TEMPLATE" != "-" ]; then
    SRC="$SPECIALTIES_DIR/$TEMPLATE/params"
    [ -d "$SRC" ] || { echo "FEHLER: Vorlage '$TEMPLATE' nicht gefunden ($SRC)."; exit 1; }
    # vorhandene Dateien werden nie überschrieben (-n); Archiv der Vorlage wird nicht kopiert
    rsync -a --ignore-existing --exclude '_archiv_*' --exclude '.DS_Store' "$SRC/" "$TARGET/params/"
    echo "✔ params/ aus '$TEMPLATE' übernommen (bestehende Dateien unverändert)."
fi

echo "✔ Fachgebiet angelegt: $TARGET"
echo ""
echo "Jetzt anpassen (alles in $TARGET/params/):"
echo "  subgroups.lst, subgroup_labels.json, subgroup_aliases.json  Subgruppen + Übersetzungen"
echo "  journals_spec.lst, journals_adjacent.lst, top_journals.lst  Journals"
echo "  tiab_keywords.lst, canary_trials.lst, journal_display.json  Stichwörter, Studiennamen"
echo "  branding.json (field_name, field_name_de, file_slug, digest_title)"
echo "  prompts/*.txt                                                Rollen und Regeln der KI"
echo "  focus_topic.json                                             löschen, falls kein Schwerpunktthema"
echo "Danach prüfen:  ./run_digest.sh $SPECIALTY check"
