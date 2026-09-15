import os
import re
import glob
import requests

# --- Konfiguration ---
ORDS_URL = "https://gf45d602cd77ce9-dbmonty.adb.eu-zurich-1.oraclecloudapps.com/ords/vernetzer/cl_pubmed_digest_articles/"

def get_latest_md_files():
    """
    Sucht für jedes Fachgebiet unter '*_digest/output/' 
    den alphabetisch/zeitlich neuesten Datumsordner 
    und gibt den Pfad zur darin liegenden .md-Datei zurück.
    """
    latest_files = []
    
    # 1. Alle Fachgebiet-Ordner suchen (z. B. cardiology_digest, dermatology_digest)
    specialty_dirs = glob.glob("*_digest")
    
    for spec_dir in specialty_dirs:
        output_path = os.path.join(spec_dir, "output")
        if not os.path.exists(output_path):
            continue
            
        # 2. Alle Datums-Unterordner unter output/ ermitteln
        subdirs = [
            os.path.join(output_path, d) for d in os.listdir(output_path)
            if os.path.isdir(os.path.join(output_path, d))
        ]
        
        if subdirs:
            # Sortieren bringt den neuesten Timestamp nach ganz hinten ([-1])
            subdirs.sort()
            latest_dir = subdirs[-1]
            
            # 3. .md-Datei im neuesten Ordner suchen
            md_files = glob.glob(os.path.join(latest_dir, "*.md"))
            if md_files:
                latest_files.append(md_files[0]) # Nimmt z.B. cardiology_digest.md
                
    return latest_files

def parse_md_file(file_path):
    path_parts = file_path.split(os.sep)
    specialty_folder = path_parts[0]
    raw_specialty = specialty_folder.split('_')[0]
    medical_specialty = raw_specialty.capitalize()

    digest_date = None
    date_match_path = re.search(r'(\d{8})_\d{6}', file_path)
    if date_match_path:
        d_str = date_match_path.group(1)
        digest_date = f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:8]}"

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    date_match_header = re.search(r"#\s+.*Digest\s+-\s+(\d{4}-\d{2}-\d{2})", content)
    if date_match_header:
        digest_date = date_match_header.group(1)

    formatted_date = f"{digest_date}T00:00:00Z" if digest_date else None

    blocks = content.split("### ")
    articles = []

    for block in blocks[1:]:
        lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
        if not lines:
            continue

        title = lines[0]
        journal, score, sub_specialty, bottom_line, url = "", "", "", "", ""

        for line in lines[1:]:
            # Journal säubern (entfernt z. B. "- **Journal**:")
            if "Journal" in line:
                journal = re.sub(r'^[-\s]*\*\*Journal\*\*:\s*', '', line).strip()
            
            # Score & Subgroup säubern
            elif "Score" in line:
                parts = line.split("|")
                # Entfernt z. B. "- **Score**:"
                score = re.sub(r'^[-\s]*\*\*Score\*\*:\s*', '', parts[0]).strip()
                
                if len(parts) > 1 and "Subgroup" in parts[1]:
                    # Entfernt z. B. "**Subgroup**:"
                    sub_specialty = re.sub(r'^[-\s]*\*\*Subgroup\*\*:\s*', '', parts[1]).strip()
            
            # Bottom Line säubern (entfernt z. B. "- **Bottom Line**:")
            elif "Bottom Line" in line:
                bottom_line = re.sub(r'^[-\s]*\*\*Bottom Line\*\*:\s*', '', line).strip()
            
            # URL säubern (entfernt z. B. "- **URL**:")
            elif "URL" in line:
                url = re.sub(r'^[-\s]*\*\*URL\*\*:\s*', '', line).strip()

        articles.append({
            "digest_date": formatted_date,
            "medical_specialty": medical_specialty,
            "sub_specialty": sub_specialty,
            "title": title,
            "journal": journal,
            "score": score,
            "bottom_line": bottom_line,
            "url": url
        })

    return articles

def insert_via_ords(articles):
    headers = {"Content-Type": "application/json"}
    success_count = 0
    
    for article in articles:
        response = requests.post(ORDS_URL, json=article, headers=headers)
        if response.status_code in (200, 201):
            success_count += 1
        else:
            # Erweiterte Fehlerausgabe für exaktes Debugging
            print(f"  ❌ Status {response.status_code} bei '{article['title'][:30]}...': {response.text}")
            
    print(f"  ✅ Erfolgreich {success_count} von {len(articles)} Artikeln importiert.")

if __name__ == "__main__":
    files_to_process = get_latest_md_files()
    
    if not files_to_process:
        print("Keine passenden .md-Dateien in den output-Ordnern gefunden.")
    else:
        print(f"Gefundene neueste Dateien ({len(files_to_process)}):")
        for filePath in files_to_process:
            print(f"\nVerarbeite Datei: {filePath}")
            data = parse_md_file(filePath)
            insert_via_ords(data)