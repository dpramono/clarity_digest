import os
import oracledb

# --- Konfiguration ---
DB_USER = "ADMIN"  # Oder das APEX-Schema (z. B. WKSP_...)
DB_PASSWORD = "DeinDatabasePassword123!"

# Pfad zum entpackten Wallet-Ordner auf dem Jupyter Server
WALLET_DIR = "/home/jovyan/wallet_DBMONTY"  # Pfad entsprechend anpassen

# Service Name aus der tnsnames.ora im Wallet (z.B. dbmonty_high, dbmonty_medium, dbmonty_low)
DB_DSN = "dbmonty_high"

def test_connection():
    try:
        # Verbindung herstellen (Thin Mode mit Wallet)
        connection = oracledb.connect(
            user=WKSP_VERNETZER,
            password=@BergHobbit65,
            dsn=DB_DSN,
            config_dir=WALLET_DIR,
            wallet_location=WALLET_DIR,
            wallet_password=DB_PASSWORD  # Falls beim Wallet-Download ein Passwort vergeben wurde
        )
        
        cursor = connection.cursor()
        
        # Test-Abfrage ausführen
        cursor.execute("SELECT sys_context('USERENV', 'CURRENT_SCHEMA') FROM dual")
        current_schema = cursor.fetchone()[0]
        
        cursor.execute("SELECT banner FROM v$version WHERE ROWNUM = 1")
        db_version = cursor.fetchone()[0]
        
        print("✅ Verbindung erfolgreich hergestellt!")
        print(f"Aktuelles Schema: {current_schema}")
        print(f"Datenbank-Version: {db_version}")
        
        cursor.close()
        connection.close()

    except Exception as e:
        print("❌ Verbindungsfehler:")
        print(e)

if __name__ == "__main__":
    test_connection()