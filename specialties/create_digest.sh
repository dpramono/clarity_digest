#!/bin/bash

# Funktion zur Anzeige der Hilfe / Fehlermeldung
show_usage() {
    echo ""
    echo "========================================================"
    echo "  FEHLER: Es wurde kein Fachbereich (specialty) angegeben!"
    echo "========================================================"
    echo ""
    echo "  Nutzung:  ./create_digest.sh <specialty>"
    echo "  Beispiel: ./create_digest.sh cardiology"
    echo "  Beispiel: ./create_digest.sh oncology"
    echo ""
}

# 1. Prüfen, ob das Argument übergeben wurde
SPECIALTY="$1"

# Falls kein Argument angegeben wurde: Interaktiv nachfragen oder abbrechen
if [ -z "$SPECIALTY" ]; then
    show_usage
    
    # Interaktive Abfrage als Benutzerhilfe
    read -p "Möchten Sie den Fachbereich jetzt eingeben? (z.B. cardiology): " SPECIALTY
    
    # Falls immer noch nichts eingegeben wurde -> Abbruch
    if [ -z "$SPECIALTY" ]; then
        echo "--> Keine Eingabe erhalten. Vorgang wird abgebrochen."
        exit 1
    fi
fi

# 2. Ordnername definieren
TARGET_DIR="${SPECIALTY}_digest"

echo ""
echo "--------------------------------------------------------"
echo "Erstelle Verzeichnisstruktur für '$SPECIALTY'..."

# 3. Unterordner erstellen (-p verhindert Fehler, falls Ordner bereits existieren)
mkdir -p "$TARGET_DIR/params"
mkdir -p "$TARGET_DIR/refs"
mkdir -p "$TARGET_DIR/output"

echo "✔ Ordner erfolgreich erstellt:"
echo "   clarity/$TARGET_DIR/params"
echo "   clarity/$TARGET_DIR/refs"
echo "   clarity/$TARGET_DIR/output"
echo "--------------------------------------------------------"
echo ""

# Ausführungsübersicht anzeigen
ls -R "$TARGET_DIR"