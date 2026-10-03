import os
import json
import requests
from datetime import datetime, timezone
import tinytuya

# --- KONFIGURATION & MILJÖVARIABLER ---
GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
TEMPERATUR_NU_STATION = os.getenv("TEMPERATUR_NU_STATION", "ostersund") # Byt till din närmsta station, t.ex. 'as' eller 'ostersund'

# Tuya Cloud API
TUYA_API_KEY = os.getenv("TUYA_API_KEY")
TUYA_API_SECRET = os.getenv("TUYA_API_SECRET")
TUYA_DEVICE_ID = os.getenv("TUYA_DEVICE_ID")
TUYA_REGION = os.getenv("TUYA_REGION", "eu")

# --- GIST-FUNKTIONER ---
headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json"
}

def load_gist_data():
    try:
        res = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=headers)
        if res.status_code == 200:
            files = res.json()["files"]
            config = json.loads(files["config.json"]["content"]) if "config.json" in files else {}
            history = json.loads(files["history.json"]["content"]) if "history.json" in files else []
            return config, history
    except Exception as e:
        print(f"Fel vid hämtning från Gist: {e}")
    return {}, []

def save_history(history_data):
    # Behåll bara de senaste 200 datapunkterna (cirka 2 dygn) för att inte fylla Gisten
    trimmed_history = history_data[-200:]
    payload = {
        "files": {
            "history.json": {
                "content": json.dumps(trimmed_history, indent=2)
            }
        }
    }
    requests.patch(f"https://api.github.com/gists/{GIST_ID}", headers=headers, json=payload)

# --- NORDPOOL API (KVARTSPRISER/SE3/SE2) ---
def get_nordpool_prices():
    now = datetime.now()
    date_str = now.strftime("%Y/%m-%d")
    # Vi hämtar för SE3 (ändra till SE2 om du bor längre norrut)
    url = f"https://www.nordpoolgroup.com/api/marketdata/page/10?currency=SEK&endDate={now.strftime('%d-%m-%Y')}"
    
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            prices = []
            # Förenklad parsing av Nordpools öppna API (öre/kWh inkl uppskattad moms)
            for row in data.get("data", {}).get("Rows", []):
                for col in row.get("Columns", []):
                    if col.get("Name") in ["SE3", "SE2"]:
                        val_str = col.get("Value", "").replace(",", ".").replace(" ", "")
                        try:
                            # Pris per MWh -> öre/kWh (multiplicera med 0.1 för SEK/MWh till öre/kWh)
                            price_ore = float(val_str) * 0.1 * 1.25 # inkl moms
                            prices.append(price_ore)
                        except ValueError:
                            pass
            return prices
    except Exception as e:
        print(f"Kunde inte hämta Nordpool-priser: {e}")
    
    # Backup-retur om API misslyckas
    return [50.0] * 96

# --- TEMPERATUR.NU API ---
def get_outside_temp():
    # Använd 'lugnvik' direkt i URL:en för ren text
    station = os.getenv("TEMPERATUR_NU_STATION", "lugnvik")
    url = f"http://www.temperatur.nu/termo/{station}/temp.txt"
    
    try:
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            # Tvätta texten och konvertera till decimaltal
            temp_str = res.text.strip()
            return float(temp_str)
    except Exception as e:
        print(f"Kunde inte hämta temperatur från temperatur.nu: {e}")
    
    return 0.0 # Standardvärde om anropet misslyckas

# --- TUYA CLOUD STYRNING ---
def set_tuya_switch(state: bool):
    if not all([TUYA_API_KEY, TUYA_API_SECRET, TUYA_DEVICE_ID]):
        print("Tuya API-nycklar saknas. Hoppar över fysisk styrning (testläge).")
        return
    
    try:
        cloud = tinytuya.Cloud(
            apiEndpoint=f"https://openapi.tuya{TUYA_REGION}.com",
            apiKey=TUYA_API_KEY,
            apiSecret=TUYA_API_SECRET,
            devId=TUYA_DEVICE_ID
        )
        
        # Skicka kommando för att slå på/av switch 1
        commands = {
            "commands": [
                {"code": "switch_1", "value": state}
            ]
        }
        res = cloud.sendcommand(TUYA_DEVICE_ID, commands)
        print(f"Tuya svar: {res}")
    except Exception as e:
        print(f"Fel vid Tuya-styrning: {e}")

# --- HOVUDLOGIK ---
def main():
    print("Startar elstyrningskontroll...")
    config, history = load_gist_data()
    
    current_temp = get_outside_temp()
    prices = get_nordpool_prices()
    
    # Beräkna aktuellt kvartsindex (0-95 under dygnet)
    now = datetime.now()
    quarter_index = (now.hour * 4) + (now.minute // 15)
    
    current_price = prices[quarter_index] if quarter_index < len(prices) else prices[0]
    
    # 1. Kolla Manuell överstyrning från GUI
    override = config.get("force_override", "AUTO")
    should_be_on = False
    
    if override == "PÅ (Tvinga igång)":
        should_be_on = True
        print("Läge: Manuell PÅ")
    elif override == "AV (Stäng av allt)":
        should_be_on = False
        print("Läge: Manuell AV")
    else:
        # AUTO-LÄGE
        max_percent = config.get("max_price_percent", 50)
        max_cap = config.get("max_price_cap", 250)
        cold_threshold = config.get("cold_temp_threshold", 0)
        min_mins_cold = config.get("min_minutes_cold", 15)
        
        # Sortera alla priser för att hitta gränsvärdet för de X% billigaste
        sorted_prices = sorted(prices)
        cutoff_index = int(len(sorted_prices) * (max_percent / 100.0))
        price_limit = sorted_prices[min(cutoff_index, len(sorted_prices) - 1)]
        
        # Regel A: Är det inom de billigaste procenten OCH under maxpris-spärren?
        if current_price <= price_limit and current_price <= max_cap:
            should_be_on = True
            
        # Regel B: Koldskydd – om det är kallt, se till att det går minst viss tid per timme
        if current_temp <= cold_threshold and min_mins_cold > 0:
            mins_in_hour = now.minute
            if mins_in_hour < min_mins_cold:
                should_be_on = True
                print(f"Koldskydd aktiverat ({current_temp}°C <= {cold_threshold}°C): Tvingar körtid första {min_mins_cold} min i timmen.")

    status_str = "ON" if should_be_on else "OFF"
    print(f"Pris: {current_price:.2f} öre/kWh | Temp: {current_temp}°C | Beslut: {status_str}")
    
    # Slå på eller av Deltaco/Tuya-kontakten
    set_tuya_switch(should_be_on)
    
    # Spara datapunkt till historiken
    log_entry = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M"),
        "price": round(current_price, 2),
        "status": status_str,
        "temp": round(current_temp, 1)
    }
    history.append(log_entry)
    save_history(history)
    print("Loggat till history.json i Gist!")

if __name__ == "__main__":
    main()