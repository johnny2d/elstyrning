import os
import json
import pulp
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import tinytuya

# --- KONFIGURATION & MILJÖVARIABLER ---
GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("GIST_TOKEN")
TEMPERATUR_NU_STATION = os.getenv("TEMPERATUR_NU_STATION", "ostersund")

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
    trimmed_history = history_data[-200:]
    payload = {
        "files": {
            "history.json": {
                "content": json.dumps(trimmed_history, indent=2)
            }
        }
    }
    
    if not GIST_ID:
        return

    url = f"https://api.github.com/gists/{GIST_ID}"
    requests.patch(url, headers=headers, json=payload)

# --- NORDPOOL API (FÖR DAGEN ELLER MORGONDAGEN) ---
def get_nordpool_price(zone="SE2", target_date=None):
    sweden_tz = ZoneInfo("Europe/Stockholm")
    if not target_date:
        target_date = datetime.now(sweden_tz)

    year = target_date.strftime("%Y")
    # Använd strip för att ta bort eventuella ledande nollor om API:et är känsligt för det, 
    # eller standard %m-%d vilket brukar vara MM-DD. 
    # Standardformat för elprisetjustnu: YYYY/MM-DD_ZONE.json
    month = target_date.strftime("%m")
    day = target_date.strftime("%d")
    
    # Testa med bindestreck mellan år, månad och dag enligt deras exakta standard:
    url = f"https://www.elprisetjustnu.se/api/v1/prices/{year}/{month}-{day}_{zone}.json"
    print(f"[DEBUG] Försöker hämta URL: {url}")
    
    try:
        res = requests.get(url, timeout=10)
        print(f"[DEBUG] API Statuskod: {res.status_code}")
        
        if res.status_code == 200:
            data = res.json()
            print(f"[DEBUG] Antal mottagna poster från API: {len(data)}")
            prices = [round(item["SEK_per_kWh"] * 100, 2) for item in data]
            if len(prices) == 96:
                return prices
            elif len(prices) == 24:
                prices_96 = []
                for p in prices:
                    prices_96.extend([p] * 4)
                return prices_96
        else:
            print(f"[DEBUG] Felaktig statuskod från API: {res.text[:200]}")
    except Exception as e:
        print(f"[ERROR] Undantag vid hämtning av elpris-lista för {target_date.strftime('%Y-%m-%d')}: {e}")
        
    print(f"[WARNING] Kunde inte hämta priser för {target_date.strftime('%Y-%m-%d')}. Returnerar reservlista på 50 öre.")
    return [50.0] * 96

# --- TEMPERATUR.NU API ---
def get_outside_temp():
    station = os.getenv("TEMPERATUR_NU_STATION", "lugnvik")
    url = f"http://www.temperatur.nu/termo/{station}/temp.txt"
    try:
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            temp_str = res.text.strip()
            if temp_str.upper() == "N/A" or not temp_str:
                return 0.0
            return float(temp_str)
    except Exception:
        pass
    return 0.0

# --- TUYA CLOUD STYRNING ---
def set_tuya_switch(state: bool):
    if not all([TUYA_API_KEY, TUYA_API_SECRET, TUYA_DEVICE_ID]):
        return
    try:
        cloud = tinytuya.Cloud(
            apiEndpoint=f"https://openapi.tuya{TUYA_REGION}.com",
            apiKey=TUYA_API_KEY,
            apiSecret=TUYA_API_SECRET,
            devId=TUYA_DEVICE_ID
        )
        commands = {"commands": [{"code": "switch_1", "value": state}]}
        cloud.sendcommand(TUYA_DEVICE_ID, commands)
    except Exception:
        pass

def make_decision(prices, current_index, percentile_limit=50):
    if not prices or current_index >= len(prices):
        current_price = 50.0
        is_on = True
    else:
        current_price = prices[current_index]
        sorted_prices = sorted(prices)
        cutoff_index = int(len(sorted_prices) * (percentile_limit / 100.0)) - 1
        cutoff_index = max(0, cutoff_index)
        threshold_price = sorted_prices[cutoff_index]
        is_on = current_price <= threshold_price
    return "ON" if is_on else "OFF", current_price

def generate_daily_schedule(prices_96, max_off_blocks=12, min_on_blocks=32):
    if not prices_96 or len(prices_96) != 96:
        raise ValueError("Måste skicka in exakt 96 kvartsspriser.")

    prob = pulp.LpProblem("Varmekostnad_Minimering", pulp.LpMinimize)
    x = pulp.LpVariable.dicts("slot", range(96), cat=pulp.LpBinary)
    
    prob += pulp.lpSum([x[i] * prices_96[i] for i in range(96)])
    prob += pulp.lpSum([x[i] for i in range(96)]) >= min_on_blocks

    for i in range(96 - max_off_blocks):
        prob += pulp.lpSum([x[j] for j in range(i, i + max_off_blocks + 1)]) >= 1

    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    return [bool(x[i].varValue) for i in range(96)]

# --- HUVUDLOGIK ---
def main():
    print("Startar elstyrningskontroll...")
    config, history = load_gist_data()
    
    sweden_tz = ZoneInfo("Europe/Stockholm")
    now = datetime.now(sweden_tz)
    today_str = now.strftime("%Y-%m-%d")
    
    tomorrow_dt = now + timedelta(days=1)
    tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")

    current_temp = get_outside_temp()
    
    # --- 1. SCHEMAHANTERING & DYGNSVÄXLING ---
    active_schedule_date = config.get("schedule_date")
    daily_schedule = config.get("daily_schedule")
    
    tomorrow_schedule_date = config.get("tomorrow_schedule_date")
    tomorrow_schedule = config.get("tomorrow_schedule")

    if active_schedule_date != today_str:
        if tomorrow_schedule_date == today_str and tomorrow_schedule:
            print("Nytt dygn: Flyttar över morgondagens förberäknade schema till aktivt schema!")
            daily_schedule = tomorrow_schedule
            active_schedule_date = today_str
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
        save_config_to_gist(config)

    # --- 2. FÖRHANDSBERÄKNING FÖR MORGONDAGEN ---
    print(f"[DEBUG] Nuvarande timme: {now.hour}, sparat morgondagsschema datum: {tomorrow_schedule_date}, förväntat: {tomorrow_str}")
    if now.hour >= 14 and tomorrow_schedule_date != tomorrow_str:
        print("Klockan är efter 14:00. Försöker hämta och optimera morgondagens priser...")
        prices_tomorrow = get_nordpool_price(zone="SE2", target_date=tomorrow_dt)
        
        print(f"[DEBUG] Unika prisvärden i morgondagens prislista: {len(set(prices_tomorrow))}")
        if len(set(prices_tomorrow)) > 1: 
            max_off = config.get("max_off_blocks", 12)
            min_on = config.get("min_on_blocks", 32)
            
            print("Kör PuLP-optimering för morgondagen...")
            new_tomorrow_sched = generate_daily_schedule(prices_tomorrow, max_off_blocks=max_off, min_on_blocks=min_on)
            
            config["tomorrow_schedule_date"] = tomorrow_str
            config["tomorrow_schedule"] = new_tomorrow_sched
            save_config_to_gist(config)
            print("Morgondagens schema har förhandsberäknats och sparats!")
        else:
            print("[WARNING] Priserna för morgondagen returnerade endast 50-öres fallback. Sparar inte.")

    # --- 3. AVSLUTANDE DRIFTKÖRNING FÖR AKTUELL KVART ---
    prices_today = get_nordpool_price(zone="SE2", target_date=now)
    quarter_index = (now.hour * 4) + (now.minute // 15)
    current_price = prices_today[quarter_index] if quarter_index < len(prices_today) else 50.0

    override = config.get("force_override", "AUTO")
    should_be_on = False
    
    if override == "PÅ (Tvinga igång)":
        should_be_on = True
    elif override == "AV (Stäng av allt)":
        should_be_on = False
    else:
        if daily_schedule and len(daily_schedule) == 96:
            should_be_on = daily_schedule[quarter_index]
        else:
            should_be_on = True

        cold_threshold = config.get("cold_temp_threshold", 0)
        min_mins_cold = config.get("min_minutes_cold", 15)
        
        if current_temp <= cold_threshold and min_mins_cold > 0:
            if now.minute < min_mins_cold:
                should_be_on = True

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