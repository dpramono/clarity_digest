import requests

# --- Konfiguration ---
# Beispiel: https://g8f...-dbmonty.adb.eu-zurich-1.oraclecloud.com/ords/dein_schema/pubmed_digest_articles/
# ORDS_URL = "https://<DEIN_OCI_HOST>/ords/<DEIN_SCHEMA>/pubmed_digest_articles/"
ORDS_URL = "https://gf45d602cd77ce9-dbmonty.adb.eu-zurich-1.oraclecloudapps.com/ords/vernetzer/cl_pubmed_digest_articles/"

def test_ords_get():
    """Prüft den Lesezugriff (HTTP GET) auf den ORDS-Endpunkt."""
    print(f"Test 1: Lesezugriff (GET) auf {ORDS_URL} ...")
    try:
        response = requests.get(ORDS_URL, timeout=10)
        
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            items = data.get("items", [])
            print("✅ ORDS-Verbindung erfolgreich!")
            print(f"Anzahl gefundener Datensätze: {len(items)}")
            if items:
                print("Erster Datensatz (Muster):", items[0])
        elif response.status_code == 404:
            print("❌ Endpunkt nicht gefunden (404). Prüfen Sie die URL oder ob Auto-REST aktiviert ist.")
        elif response.status_code == 401 or response.status_code == 403:
            print("🔒 Zugriff verweigert. Prüfen Sie, ob Privileges/Authentifizierung für das Auto-REST-Objekt aktiv sind.")
        else:
            print(f"⚠️ Antworterhalt mit Statuscode: {response.status_code}")
            print("Antwort:", response.text)
            
    except requests.exceptions.RequestException as e:
        print("❌ Verbindungsfehler (Netzwerk/DNS):")
        print(e)

if __name__ == "__main__":
    test_ords_connection = test_ords_get()