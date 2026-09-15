import os
import re
import datetime
import oracledb

# --- Datenbank-Konfiguration ---
DB_USER = "dein_schema"
DB_PASSWORD = "dein_passwort"
DB_DSN = "dein_oci_dsn_oder_tns"

def parse_md_file(file_path):
    file_name = os.path.basename(file_path)
    
    # 1. medical_specialty aus dem Dateinamen extrahieren (z.B. "cardiology" -> "Cardiology")
    # Trennt beim ersten Unterstrich
    raw_specialty = file_name.split('_')[0]
    medical_specialty = raw_specialty.capitalize()
    
    # 2. Datum aus Dateinamen auslesen (z.B. 20260915 -> 2026-09-15) als Fallback
    date_match_filename = re.search(r'(\d{8})', file_name)
    digest_date = None
    if date_match_filename:
        d_str = date_match_filename.group(1)
        digest_date = f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:8]}"

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Falls Datum im Text-Header steht (z.B. # Cardio Digest - 2026-09-12), dieses bevorzugen
    date_match_header = re.search(r"#\s+.*Digest\s+-\s+(\d{4}-\d{2}-\d{2})", content)
    if date_match_header:
        digest_date = date_match_header.group(1)

    # Blöcke anhand von '### ' aufteilen
    blocks = content.split("### ")
    articles = []

    for block in blocks[1:]:
        lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
        if not lines:
            continue

        title = lines[0]
        journal = ""
        score = ""
        sub_specialty = ""
        bottom_line = ""
        url = ""

        for line in lines[1:]:
            if "**Journal**:" in line:
                journal = line.split(":**", 1)[-1].strip()
            elif "**Score**:" in line:
                parts = line.split("|")
                score = parts[0].split(":**", 1)[-1].strip()
                # Subgroup entnehmen und als sub_specialty speichern
                if len(parts) > 1 and "**Subgroup**:" in parts[1]:
                    sub_specialty = parts[1].split(":**", 1)[-1].strip()
            elif "**Bottom Line**:" in line:
                bottom_line = line.split(":**", 1)[-1].strip()
            elif "**URL**:" in line:
                url = line.split(":**", 1)[-1].strip()

        articles.append((
            digest_date,
            medical_specialty,
            sub_specialty,
            title,
            journal,
            score,
            bottom_line,
            url
        ))

    return articles

def insert_into_oracle(articles):
    connection = oracledb.connect(
        user=DB_USER,
        password=DB_PASSWORD,
        dsn=DB_DSN
    )
    cursor = connection.cursor()

    sql = """
        INSERT INTO pubmed_digest_articles (
            digest_date, medical_specialty, sub_specialty, 
            title, journal, score, bottom_line, url
        ) VALUES (
            TO_DATE(:1, 'YYYY-MM-DD'), :2, :3, :4, :5, :6, :7, :8
        )
    """

    cursor.executemany(sql, articles)
    connection.commit()
    print(f"Erfolgreich {cursor.rowcount} Artikel in die Tabelle eingefügt.")

    cursor.close()
    connection.close()

if __name__ == "__main__":
    # Pfad zur Datei auf OCI Jupyter
    md_file_path = "cardiology_digest_20260915_080220.md"
    
    data = parse_md_file(md_file_path)
    insert_into_oracle(data)