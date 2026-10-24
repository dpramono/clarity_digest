#!/usr/bin/env python3
"""
Durchsucht automatisch alle Fachgebiets-Ordner (*_digest) im Verzeichnis 'specialties/',
ermittelt jeweils das neueste Output-Verzeichnis mit der passenden .docx-Datei 
und lädt die extrahierten Studien via ORDS REST API in die Oracle Cloud DB.
"""

import os
import re
import docx
import requests
import argparse
from pathlib import Path

# ORDS REST API Endpoint
ORDS_URL = "https://gf45d602cd77ce9-dbmonty.adb.eu-zurich-1.oraclecloudapps.com/ords/vernetzer/cl_pubmed_digest_studies/"

def get_all_specialty_dirs(base_path="specialties"):
    """
    Findet alle Verzeichnisse unter 'specialties/', die auf '_digest' enden.
    """
    base = Path(base_path)
    if not base.exists() or not base.is_dir():
        raise FileNotFoundError(f"Das Basis-Verzeichnis '{base_path}' wurde nicht gefunden.")
    
    # Filtert alle Ordner, die auf '_digest' enden
    digest_dirs = [d for d in base.iterdir() if d.is_dir() and d.name.endswith("_digest")]
    return sorted(digest_dirs, key=lambda x: x.name)


def get_latest_docx_from_specialty(specialty_dir_path):
    """
    Ermittelt im Ordner 'output' des übergebenen Fachgebiets
    das neueste Unterverzeichnis und gibt den Pfad zur .docx-Datei zurück.
    """
    output_path = specialty_dir_path / "output"
    
    if not output_path.exists() or not output_path.is_dir():
        print(f"⚠️ Kein 'output'-Verzeichnis in '{specialty_dir_path}' gefunden. Überspringe...")
        return None
    
    # Alle Unterverzeichnisse im output-Ordner ermitteln
    subdirs = [d for d in output_path.iterdir() if d.is_dir() and not d.name.startswith(".")]
    if not subdirs:
        print(f"⚠️ Keine Output-Unterordner in '{output_path}' gefunden. Überspringe...")
        return None
    
    # Neuestes Verzeichnis nach Änderungszeitpunkt (mtime) auswählen
    latest_dir = max(subdirs, key=lambda d: d.stat().st_mtime)
    
    # .docx-Dateien im neuesten Verzeichnis suchen
    docx_files = list(latest_dir.glob("*.docx"))
    if not docx_files:
        print(f"⚠️ Keine .docx-Datei im Ordner '{latest_dir}' gefunden. Überspringe...")
        return None
    
    # Bevorzugt Dateien wählen, die mit 'clarity_' beginnen
    for docx_file in docx_files:
        if docx_file.name.startswith("clarity_"):
            return docx_file
            
    return docx_files[0]


def parse_docx_to_studies(docx_path, specialty_name):
    """
    Parst das Word-Dokument und extrahiert die einzelnen Studienblöcke.
    """
    doc = docx.Document(docx_path)
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    
    studies = []
    current_subspecialty = "General"
    current_study = None
    
    # RegEx zur Erkennung von Studien-Headern (z. B. "1  Yield of Atrial...")
    study_header_re = re.compile(r"^(\d+)\s+(.+)$")
    
    for p in paragraphs:
        # Erkennung von Sub-Kategorien (z. B. ELECTROPHYSIOLOGY & DEVICES)
        if p.isupper() and len(p) < 60 and not p.startswith("CLARITY"):
            current_subspecialty = p.title()
            continue
            
        # Prüfen, ob ein neuer Studienblock beginnt
        match = study_header_re.match(p)
        if match:
            if current_study:
                studies.append(current_study)
            
            study_order = int(match.group(1))
            title = match.group(2)
            
            current_study = {
                "specialty": specialty_name,
                "subspecialty": current_subspecialty,
                "study_order": study_order,
                "title": title,
                "journal": "",
                "pub_date": "",
                "authors": "",
                "study_type": "",
                "design_info": "",
                "summary": "",
                "authors_conclusion": "",
                "pubmed_url": "",
                "doi": ""
            }
            continue
        
        # Details innerhalb der aktuellen Studie befüllen
        if current_study:
            if " · " in p and not current_study["journal"]:
                parts = p.split(" · ")
                if len(parts) >= 2:
                    current_study["journal"] = parts[0]
                    current_study["pub_date"] = parts[1]
                if len(parts) >= 3:
                    current_study["authors"] = parts[2]
            elif p.startswith("●"):
                current_study["study_type"] = p.replace("●", "").strip()
            elif p.startswith("SUMMARY"):
                current_study["summary"] = p.replace("SUMMARY", "").strip()
            elif p.startswith("AUTHORS' CONCLUSION"):
                current_study["authors_conclusion"] = p.replace("AUTHORS' CONCLUSION (AS REPORTED)", "").strip()
            elif "DOI:" in p:
                doi_match = re.search(r"DOI:\s*(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", p)
                if doi_match:
                    current_study["doi"] = doi_match.group(1)
            elif "pubmed.ncbi.nlm.nih.gov" in p:
                current_study["pubmed_url"] = p

    if current_study:
        studies.append(current_study)
        
    return studies


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


def process_all_specialties(base_path="specialties"):
    specialty_dirs = get_all_specialty_dirs(base_path)
    print(f"Gefundene Fachgebiets-Verzeichnisse ({len(specialty_dirs)}): {[d.name for d in specialty_dirs]}\n" + "-"*60)
    
    total_uploaded = 0

    for spec_dir in specialty_dirs:
        # Fachgebietsname ableiten (z. B. 'cardiology_digest' -> 'Cardiology')
        specialty_db_name = spec_dir.name.replace("_digest", "").capitalize()
        print(f"\n📁 Verarbeite Fachgebiet: {spec_dir.name} ({specialty_db_name})")
        
        docx_file = get_latest_docx_from_specialty(spec_dir)
        if not docx_file:
            continue
            
        print(f"  📄 Neueste Datei: {docx_file}")
        
        # Studien parsen & hochladen
        studies = parse_docx_to_studies(docx_file, specialty_name=specialty_db_name)
        print(f"  🔍 {len(studies)} Studien extrahiert. Starte Upload...")
        
        uploaded = upload_to_ords(studies)
        total_uploaded += uploaded

    print("\n" + "="*60)
    print(f"✅ Verarbeitung abgeschlossen! Gesamtzahl hochgeladener Studien: {total_uploaded}")


if __name__ == "__main__":
    process_all_specialties()