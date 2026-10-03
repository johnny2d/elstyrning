import os
import json
import requests
from datetime import datetime, timezone
import tinytuya

# --- KONFIGURATION & MILJÖVARIABLER ---
GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("GIST_TOKEN")
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
    # Behåll bara de senaste 200 datapunkterna
    trimmed_history = history_data[-200:]
    payload = {
        "files": {
            "history.json": {
                "content": json.dumps(trimmed_history, indent=2)
            }
        }
    }
    
    # --- DEBUG: Kontrollera variabler innan anrop ---
    print(f"[DEBUG] GIST_ID: '{GIST_ID}'")
    print(f"[DEBUG] Headers som skickas: {headers}")
    
    if not GIST_ID:
        print("[ERROR] GIST_ID saknas eller är tom! Avbryter sparande.")
        return

    url = f"https://api.github.com/gists/{GIST_ID}"
    response = requests.patch(url, headers=headers, json=payload)
    
    # --- DEBUG: Kontrollera svar från GitHub API ---
    print(f"[DEBUG] GitHub API Statuskod: {response.status_code}")
    
    if response.status_code == 200:
        print("Loggat till history.json i Gist!")
    else:
        print(f"[ERROR] Kunde inte spara till Gist. Svar från GitHub: {response.text}")
# --- NORDPOOL API (KVARTSPRISER/SE3/SE2) ---
import requests
from datetime import datetime

def get_nordpool_price(zone="SE3"):
    """
    Hämtar samtliga elpriser för innevarande dygn i öre/kWh (inkl. moms) som en lista.
    Zoner: SE1, SE2, SE3, SE4
    """
    now = datetime.now()
    year = now.strftime("%Y")
    month_day = now.strftime("%m-%d")
    
    url = f"https://www.elprisetjustnu.se/api/v1/prices/{year}/{month_day}_{zone}.json"
    
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            # Omvandla från SEK/kWh till öre/kWh (SEK_per_kWh * 100) för alla tidsintervall
            prices = [round(item["SEK_per_kWh"] * 100, 2) for item in data]
            if prices:
                return prices
    except Exception as e:
        print(f"[ERROR] Fel vid hämtning av elpris-lista: {e}")
        
    print("[WARNING] Använder reservpris-lista (50.00 öre/kWh)")
    # Reservlista för ett helt dygn om anropet misslyckas (96 kvartar)
    return [50.0] * 96

# --- TEMPERATUR.NU API ---
def get_outside_temp():
    station = os.getenv("TEMPERATUR_NU_STATION", "lugnvik")
    url = f"http://www.temperatur.nu/termo/{station}/temp.txt"
    
    try:
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            temp_str = res.text.strip()
            # Om temperatur.nu svarar med 'N/A' eller tom text
            if temp_str.upper() == "N/A" or not temp_str:
                print("Temperatur.nu returnerade N/A, använder 0.0 som reserv.")
                return 0.0
            return float(temp_str)
    except Exception as e:
        print(f"Kunde inte hämta temperatur från temperatur.nu: {e}")
    
    return 0.0

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
def make_decision(prices, current_index, percentile_limit=50):
    """
    Bestämmer om värmen ska vara ON eller OFF.
    percentile_limit=50 innebär att den bara är igång under de 50% billigaste kvartarna för dagen.
    """
    if not prices or current_index >= len(prices):
        current_price = 50.0
        is_on = True
    else:
        current_price = prices[current_index]
        
        # Sortera alla dygnets priser för att hitta gränsvärdet (tröskeln)
        sorted_prices = sorted(prices)
        cutoff_index = int(len(sorted_prices) * (percentile_limit / 100.0)) - 1
        cutoff_index = max(0, cutoff_index)
        threshold_price = sorted_prices[cutoff_index]
        
        # Slå på om det aktuella priset är lägre än eller lika med tröskeln
        is_on = current_price <= threshold_price
        
        print(f"[LOG] Aktuellt pris: {current_price:.2f} öre/kWh")
        print(f"[LOG] Tröskelvärde ({percentile_limit}% billigaste): {threshold_price:.2f} öre/kWh")
        
    return "ON" if is_on else "OFF", current_price
# --- HOVUDLOGIK ---
def main():
    print("Startar elstyrningskontroll...")
    config, history = load_gist_data()
    
    current_temp = get_outside_temp()
    prices = get_nordpool_price(zone="SE3")
    
    now = datetime.now()
    
    # Dynamisk indexering beroende på om API returnerar 96 kvartar eller 24 timmar
    if len(prices) == 24:
        quarter_index = now.hour
    else:
        quarter_index = (now.hour * 4) + (now.minute // 15)
    
    # Säkerställ att index finns i listan (faller tillbaka på sista elementet eller pris 0 vid gränsfall)
    if quarter_index < len(prices):
        current_price = prices[quarter_index]
    else:
        current_price = prices[-1] if prices else 50.0
    
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
            
        # Regel B: Köldskydd – om det är kallt, se till att det går minst viss tid per timme
        if current_temp <= cold_threshold and min_mins_cold > 0:
            mins_in_hour = now.minute
            if mins_in_hour < min_mins_cold:
                should_be_on = True
                print(f"Köldskydd aktiverat ({current_temp}°C <= {cold_threshold}°C): Tvingar körtid första {min_mins_cold} min i timmen.")

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