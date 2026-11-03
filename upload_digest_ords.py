#!/usr/bin/env python3
"""
Durchsucht automatisch alle Fachgebiets-Ordner im Verzeichnis 'specialties/',
ermittelt jeweils das neueste Output-Verzeichnis mit der Datei studies.json
(von clarity_digest.py geschrieben) und lädt die Studien via ORDS REST API in die Oracle Cloud DB.

Nutzung:
  python3 upload_digest_ords.py                       alle Fachgebiete hochladen
  python3 upload_digest_ords.py cardiology            nur dieses Fachgebiet
  python3 upload_digest_ords.py --dry-run             nur anzeigen, nichts senden
"""

import json
import requests
import argparse
from pathlib import Path

# ORDS REST API Endpoint
ORDS_URL = "https://gf45d602cd77ce9-dbmonty.adb.eu-zurich-1.oraclecloudapps.com/ords/vernetzer/cl_pubmed_digest_studies/"

STUDIES_FILE = "studies.json"

# Spalten der Tabelle; weitere Felder in studies.json (pmid, headline, ...) werden nicht gesendet
DB_FIELDS = ("specialty", "subspecialty", "study_order", "title", "journal", "pub_date",
             "authors", "study_type", "design_info", "summary", "authors_conclusion",
             "pubmed_url", "doi")


def get_all_specialty_dirs(base_path="specialties"):
    """
    Findet alle Fachgebiets-Verzeichnisse unter 'specialties/' (mit Unterordner 'output').
    """
    base = Path(base_path)
    if not base.exists() or not base.is_dir():
        raise FileNotFoundError(f"Das Basis-Verzeichnis '{base_path}' wurde nicht gefunden.")

    # Filtert alle Ordner, die einen 'output'-Unterordner haben (z. B. cardiology, dermatology)
    digest_dirs = [d for d in base.iterdir() if d.is_dir() and (d / "output").is_dir()]
    return sorted(digest_dirs, key=lambda x: x.name)


def get_latest_studies_from_specialty(specialty_dir_path):
    """
    Ermittelt im Ordner 'output' des übergebenen Fachgebiets das neueste
    Unterverzeichnis mit einer studies.json und gibt deren Pfad zurück.
    """
    output_path = specialty_dir_path / "output"

    if not output_path.exists() or not output_path.is_dir():
        print(f"⚠️ Kein 'output'-Verzeichnis in '{specialty_dir_path}' gefunden. Überspringe...")
        return None

    # Unterordner heissen <fach>_digest_<JJJJMMTT_HHMMSS>: nach Zeitstempel sortieren, neuester zuerst
    subdirs = [d for d in output_path.iterdir()
               if d.is_dir() and not d.name.startswith(".") and (d / STUDIES_FILE).exists()]
    if not subdirs:
        print(f"⚠️ Keine {STUDIES_FILE} in den Unterordnern von '{output_path}' gefunden "
              f"(erst nach einem produktiven Lauf von clarity_digest.py). Überspringe...")
        return None

    latest_dir = max(subdirs, key=lambda d: d.name[-15:])
    return latest_dir / STUDIES_FILE


def load_studies(studies_path):
    """
    Liest studies.json und behält pro Studie nur die Spalten der Datenbank.
    """
    with open(studies_path, encoding="utf-8") as f:
        rows = json.load(f)
    return [{k: row.get(k, "") for k in DB_FIELDS} for row in rows]


def upload_to_ords(studies):
    """
    Sendet die extrahierten Studien via POST-Request an den ORDS-Endpunkt.
    """
    headers = {"Content-Type": "application/json"}
    success_count = 0

    for idx, study in enumerate(studies, 1):
        try:
            response = requests.post(ORDS_URL, json=study, headers=headers, timeout=15)
            if response.status_code in (200, 201):
                success_count += 1
                print(f"  [{idx}/{len(studies)}] OK: {study['title'][:45]}...")
            else:
                print(f"  [{idx}/{len(studies)}] Fehler {response.status_code}: {response.text}")
        except Exception as e:
            print(f"  [{idx}/{len(studies)}] HTTP-Fehler: {e}")

    return success_count


def process_all_specialties(base_path="specialties", only=None, dry_run=False):
    specialty_dirs = get_all_specialty_dirs(base_path)
    if only:
        specialty_dirs = [d for d in specialty_dirs if d.name in only]
    print(f"Gefundene Fachgebiets-Verzeichnisse ({len(specialty_dirs)}): {[d.name for d in specialty_dirs]}\n" + "-"*60)

    total_uploaded = 0

    for spec_dir in specialty_dirs:
        print(f"\n📁 Verarbeite Fachgebiet: {spec_dir.name}")

        studies_file = get_latest_studies_from_specialty(spec_dir)
        if not studies_file:
            continue

        print(f"  📄 Neueste Datei: {studies_file}")

        studies = load_studies(studies_file)
        if dry_run:
            print(f"  🔍 {len(studies)} Studien (--dry-run, nichts gesendet):")
            for s in studies:
                print(f"    {s['study_order']:>2}. [{s['subspecialty'][:28]}] {s['title'][:60]}")
            continue
        print(f"  🔍 {len(studies)} Studien gelesen. Starte Upload...")

        uploaded = upload_to_ords(studies)
        total_uploaded += uploaded

    print("\n" + "="*60)
    print(f"✅ Verarbeitung abgeschlossen! Gesamtzahl hochgeladener Studien: {total_uploaded}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Lädt die Studien des neuesten Laufs pro Fachgebiet in die DB.")
    ap.add_argument("specialties", nargs="*", help="nur diese Fachgebiete (Standard: alle)")
    ap.add_argument("--dry-run", action="store_true", help="nur anzeigen, nichts senden")
    args = ap.parse_args()
    process_all_specialties(only=args.specialties or None, dry_run=args.dry_run)
