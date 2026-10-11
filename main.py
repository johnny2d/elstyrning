import os
import json
import pulp  # Kan ha kvar denna om den används på andra ställen, eller ta bort
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
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

def save_config_to_gist(config_data):
    """Sparar hela config-dictionaryn till config.json i Gist."""
    payload = {
        "files": {
            "config.json": {
                "content": json.dumps(config_data, indent=2)
            }
        }
    }
    
    if not GIST_ID:
        print("[ERROR] GIST_ID saknas! Kan inte spara config.")
        return

    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        response = requests.patch(url, headers=headers, json=payload)
        if response.status_code == 200:
            print("Konfigurationen har sparats till Gist!")
        else:
            print(f"[ERROR] Kunde inte spara config till Gist. Svar: {response.text}")
    except Exception as e:
        print(f"[ERROR] Fel vid sparande av config till Gist: {e}")

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

# --- NORDPOOL API (FÖR DAGEN ELLER MORGONDAGEN) ---
def get_nordpool_price(zone="SE2", target_date=None):
    """
    Hämtar samtliga elpriser för ett givet datum i öre/kWh (inkl. moms) som en lista (96 kvartar).
    Om target_date (datetime-objekt) inte anges används dagens datum.
    """
    sweden_tz = ZoneInfo("Europe/Stockholm")
    if not target_date:
        target_date = datetime.now(sweden_tz)

    year = target_date.strftime("%Y")
    month_day = target_date.strftime("%m-%d")
    
    url = f"https://www.elprisetjustnu.se/api/v1/prices/{year}/{month_day}_{zone}.json"
    
    try:
        res = requests.get(url, timeout=10)
        print(f"Hämtar URL: {url} - Statuskod: {res.status_code}")
        if res.status_code == 200:
            data = res.json()
            # Omvandla från SEK/kWh till öre/kWh
            prices = [round(item["SEK_per_kWh"] * 100, 2) for item in data]
            if len(prices) == 96:
                return prices
            elif len(prices) == 24: # Fallback om API mot förmodan bara ger timmar
                prices_96 = []
                for p in prices:
                    prices_96.extend([p] * 4)
                return prices_96
    except Exception as e:
        print(f"[ERROR] Fel vid hämtning av elpris-lista för {target_date.strftime('%Y-%m-%d')}: {e}")
        
    print(f"[WARNING] Kunde inte hämta priser för {target_date.strftime('%Y-%m-%d')}. Returnerar reservlista.")
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



def generate_daily_schedule(prices_96, max_off_blocks=12, min_on_blocks=32):
    if not prices_96 or len(prices_96) != 96:
        raise ValueError("Måste skicka in exakt 96 kvartsspriser.")

    # 1. Skapa optimeringsproblemet
    prob = pulp.LpProblem("Varmekostnad_Minimering", pulp.LpMinimize)

    # 2. Skapa en dictionary med 96 binära variabler (0 eller 1)
    # Detta skapar variablerna: x[0], x[1], ..., x[95]
    print("PULP VERSION:", pulp.__version__)
    print("DIR PULP:", dir(pulp))
    x = pulp.LpVariable.dicts("slot", range(96), cat=pulp.LpBinary)
    # 3. MÅLFUNKTION: Minimera summan av pris * status
    prob += pulp.lpSum([x[i] * prices_96[i] for i in range(96)])

    # 4. VILLKOR 1: Minst N kvartar PÅ totalt
    prob += pulp.lpSum([x[i] for i in range(96)]) >= min_on_blocks

    # 5. VILLKOR 2: Komfortspärr – Får inte vara AV i mer än max_off_blocks kvartar i rad
    for i in range(96 - max_off_blocks):
        prob += pulp.lpSum([x[j] for j in range(i, i + max_off_blocks + 1)]) >= 1

    import os
    # Leta upp den fungerande 64-bitarsversionen av CBC-solvern
    solver_path = os.path.join(
        os.path.dirname(pulp.__file__), 
        "solverdir", "cbc", "win", "64", "cbc.exe"
    )
    
    # 6. Lös problemet med COIN_CMD eftersom en anpassad sökväg används
    # Låt PuLP välja standard-solver automatiskt (fungerar bäst i molnet)
    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    # Returnera som boolean-lista
    return [bool(x[i].varValue) for i in range(96)]

# --- HOVUDLOGIK ---
def main():
    print("Startar elstyrningskontroll...")
    config, history = load_gist_data()
    
    sweden_tz = ZoneInfo("Europe/Stockholm")
    now = datetime.now(sweden_tz)
    today_str = now.strftime("%Y-%m-%d")
    
    # Beräkna morgondagens datum med inbyggda timedelta
    tomorrow_dt = now + timedelta(days=1)
    tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")
   
   

    current_temp = get_outside_temp()
    
    # --- 1. SCHEMAHANTERING & DYGNSVÄXLING ---
    active_schedule_date = config.get("schedule_date")
    daily_schedule = config.get("daily_schedule")
    
    tomorrow_schedule_date = config.get("tomorrow_schedule_date")
    tomorrow_schedule = config.get("tomorrow_schedule")

    # Om dygnet har skiftat och vi har morgondagens schema sparat som dagens, flytta fram det!
    if active_schedule_date != today_str:
        if tomorrow_schedule_date == today_str and tomorrow_schedule:
            print("Nytt dygn: Flyttar över morgondagens förberäknade schema till aktivt schema!")
            daily_schedule = tomorrow_schedule
            active_schedule_date = today_str
            # Rensa morgondagens eftersom det nu är idag
            config["tomorrow_schedule"] = None
            config["tomorrow_schedule_date"] = None
        else:
            print("Saknar giltigt schema för idag. Genererar on-the-fly...")
            prices_today = get_nordpool_price(zone="SE2", target_date=now)
            max_off = config.get("max_off_blocks", 12)
            min_on = config.get("min_on_blocks", 32)
            daily_schedule = generate_daily_schedule(prices_today, max_off_blocks=max_off, min_on_blocks=min_on)
            active_schedule_date = today_str

        config["schedule_date"] = active_schedule_date
        config["daily_schedule"] = daily_schedule
        # Spara uppdateringen till Gist direkt
        save_config_to_gist(config)

    # --- 2. FÖRHANDSBERÄKNING FÖR MORGONDAGEN (Körs efter kl. 14:00 om det saknas) ---
    if now.hour >= 14 and tomorrow_schedule_date != tomorrow_str:
        print("Klockan är efter 14:00. Försöker hämta och optimera morgondagens priser...")
        prices_tomorrow = get_nordpool_price(zone="SE2", target_date=tomorrow_dt)
        
        # Kontrollera att vi faktisk fått nya priser (inte bara fallback på 50.0 överallt)
        if len(set(prices_tomorrow)) > 1: 
            max_off = config.get("max_off_blocks", 12)
            min_on = config.get("min_on_blocks", 32)
            
            print("Kör PuLP-optimering för morgondagen...")
            new_tomorrow_sched = generate_daily_schedule(prices_tomorrow, max_off_blocks=max_off, min_on_blocks=min_on)
            
            config["tomorrow_schedule_date"] = tomorrow_str
            config["tomorrow_schedule"] = new_tomorrow_sched
            save_config_to_gist(config)
            print("Morgondagens schema har förhandsberäknats och sparats!")

    # --- 3. AVSLUTANDE DRIFTKÖRNING FÖR AKTUELL KVART ---
    prices_today = get_nordpool_price(zone="SE2", target_date=now)
    quarter_index = (now.hour * 4) + (now.minute // 15)
    current_price = prices_today[quarter_index] if quarter_index < len(prices_today) else 50.0

    override = config.get("force_override", "AUTO")
    should_be_on = False
    
    if override == "PÅ (Tvinga igång)":
        should_be_on = True
        print("Läge: Manuell PÅ")
    elif override == "AV (Stäng av allt)":
        should_be_on = False
        print("Läge: Manuell AV")
    else:
        # Hämta status från det aktiva dygns-schemat
        if daily_schedule and len(daily_schedule) == 96:
            should_be_on = daily_schedule[quarter_index]
        else:
            should_be_on = True # Fallback

        # Köldskydd som säkerhetsspärr
        cold_threshold = config.get("cold_temp_threshold", 0)
        min_mins_cold = config.get("min_minutes_cold", 15)
        
        if current_temp <= cold_threshold and min_mins_cold > 0:
            if now.minute < min_mins_cold:
                should_be_on = True
                print(f"Köldskydd aktiverat ({current_temp}°C <= {cold_threshold}°C): Tvingar igång.")

    status_str = "ON" if should_be_on else "OFF"
    print(f"Pris: {current_price:.2f} öre/kWh | Temp: {current_temp}°C | Beslut: {status_str}")
    
    set_tuya_switch(should_be_on)
    
    log_entry = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M"),
        "price": round(current_price, 2),
        "status": status_str,
        "temp": round(current_temp, 1)
    }
    history.append(log_entry)
    save_history(history)