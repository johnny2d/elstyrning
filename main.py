import os
import json
import pulp
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import tinytuya

# Försök importera det nya 'google-genai' SDK:t i första hand, annars fallback till 'google-generativeai'
GEMINI_AVAILABLE = False
try:
    from google import genai
    GEMINI_AVAILABLE = "genai_new"
except ImportError:
    try:
        import google.genai as genai_old
        GEMINI_AVAILABLE = "genai_old"
    except ImportError:
        GEMINI_AVAILABLE = False

# --- KONFIGURATION & MILJÖVARIABLER ---
GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("GIST_TOKEN")
TEMPERATUR_NU_STATION = os.getenv("TEMPERATUR_NU_STATION", "ostersund")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

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
        if response.status_code != 200:
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

# --- NORDPOOL API ---
def get_nordpool_price(zone="SE2", target_date=None):
    sweden_tz = ZoneInfo("Europe/Stockholm")
    if not target_date:
        target_date = datetime.now(sweden_tz)

    year = target_date.strftime("%Y")
    month = target_date.strftime("%m")
    day = target_date.strftime("%d")
    
    url = f"https://www.elprisetjustnu.se/api/v1/prices/{year}/{month}-{day}_{zone}.json"
    
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            prices = [round(item["SEK_per_kWh"] * 100, 2) for item in data]
            if len(prices) == 96:
                return prices
            elif len(prices) == 24:
                prices_96 = []
                for p in prices:
                    prices_96.extend([p] * 4)
                return prices_96
    except Exception as e:
        print(f"[ERROR] Undantag vid hämtning av elpriser: {e}")
        
    return [50.0] * 96

# --- TEMPERATUR PROGNOS (OPEN-METEO) ---
def get_hourly_temp_forecast(lat=63.25, lon=14.56, target_date=None):
    """Hämtar timvis temperaturprognos för ett specifikt datum genom att matcha mot tidsstämplarna."""
    sweden_tz = ZoneInfo("Europe/Stockholm")
    if not target_date:
        target_date = datetime.now(sweden_tz)
    
    target_str = target_date.strftime("%Y-%m-%d") if hasattr(target_date, "strftime") else str(target_date)

    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=temperature_2m&timezone=Europe%2FStockholm&forecast_days=3"
    try:
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if "hourly" in data and "time" in data["hourly"] and "temperature_2m" in data["hourly"]:
                times = data["hourly"]["time"]
                temps = data["hourly"]["temperature_2m"]
                
                day_temps = [temp for t, temp in zip(times, temps) if t.startswith(target_str)]
                
                if len(day_temps) == 24:
                    return day_temps
                elif len(day_temps) > 0:
                    while len(day_temps) < 24:
                        day_temps.append(day_temps[-1])
                    return day_temps[:24]
    except Exception as e:
        print(f"[ERROR] Kunde inte hämta timprognos från Open-Meteo: {e}")
    
    current_t = get_outside_temp()
    return [current_t] * 24

# --- TEMPERATUR.NU API (MED LUGNVIK-LÄNK) ---
def get_outside_temp():
    url = "https://www.temperatur.nu/jstemp.php?s=lugnvik"
    try:
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            text = res.text.strip()
            try:
                data = res.json()
                if "temp" in data:
                    return float(data["temp"])
            except Exception:
                pass
            
            import re
            numbers = re.findall(r"[-+]?\d*\.\d+|\d+", text)
            if numbers:
                val = float(numbers[0])
                if -40 <= val <= 40:
                    return val
    except Exception as e:
        print(f"[ERROR] Kunde inte hämta temperatur från Lugnvik: {e}")
    
    try:
        lat, lon = 63.25, 14.56
        url_om = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m"
        res_om = requests.get(url_om, timeout=4)
        if res_om.status_code == 200:
            return float(res_om.json()["current"]["temperature_2m"])
    except Exception:
        pass

    return 5.0

# --- AI STRATEGEN (GEMINI) MED BÄTTRE KVOT- OCH FELHANTERING ---
def get_ai_optimized_parameters(prices_96, temp_hourly, default_price_cap=50.0, default_min_on_blocks=44, target_day_label="idag"):
    if not GEMINI_API_KEY or not GEMINI_AVAILABLE:
        return default_price_cap, default_min_on_blocks, "Ingen AI-nyckel tillgänglig."

    prompt = f"""
    Du är en expert på energistyrning och optimering av elvärme i Norrland.
    
    Här är elpriserna i öre/kWh för varje kvart (96 st) för {target_day_label}:
    {prices_96}
    
    Här är den förväntade utetemperaturen per timme (°C, 24 timmar) för {target_day_label}:
    {temp_hourly}
    
    Din uppgift är att analysera prisbilden och kylan för att bestämma två nyckelparametrar för just detta dygn:
    1. "price_cap": Pristak i öre/kWh (om priset understiger detta betraktas det som billigt och pannan ska köras).
    2. "min_on_hours": Totalt antal timmar (mellan 6 och 20) som värmen absolut minst behöver vara igång under dygnet för att inte huset ska kyla ur givet utetemperaturen.

    Regler:
    - Om det är kallare ute krävs fler timmar.
    - Om priset har kraftiga dalar ska pristaket sättas så att vi nyttjar dalarna utan att bränna el på topparna.
    
    Svara ENBART med ett giltigt JSON-objekt enligt detta format:
    {{"price_cap": <flyttal>, "min_on_hours": <heltal>, "motivering": "<kort svensk förklaring på 1-2 meningar>"}}
    """

    try:
        raw_text = ""
        if GEMINI_AVAILABLE == "genai_new":
            client = genai.Client(api_key=GEMINI_API_KEY)
            response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
            raw_text = response.text
        elif GEMINI_AVAILABLE == "genai_old":
            genai_old.configure(api_key=GEMINI_API_KEY)
            model = genai_old.GenerativeModel('gemini-1.5-flash')
            response = model.generate_content(prompt)
            raw_text = response.text

        clean_json = raw_text.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)
        
        price_cap = float(data.get("price_cap", default_price_cap))
        min_on_blocks = int(data.get("min_on_hours", int(default_min_on_blocks / 4))) * 4
        motivering = data.get("motivering", "AI-beräkning utförd.")
        
        return price_cap, min_on_blocks, motivering
    except Exception as e:
        error_msg = str(e)
        print(f"[AI ERROR] Fel vid anrop till Gemini API: {error_msg}")
        
        if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
            reasoning = "⚠️ API-kvoten för Gemini är tillfälligt slut (429). Använder standardvärden."
        elif "503" in error_msg or "UNAVAILABLE" in error_msg:
            reasoning = "⚠️ Gemini är tillfälligt överbelastad (503). Använder standardvärden."
        else:
            reasoning = f"⚠️ AI-fel ({error_msg[:30]}...). Använder standardvärden."
            
        return default_price_cap, default_min_on_blocks, reasoning

# --- TUYA CLOUD STYRNING & STATUS ---
def set_tuya_switch(state: bool):
    if not all([TUYA_API_KEY, TUYA_API_SECRET, TUYA_DEVICE_ID]):
        return
    try:
        cloud = tinytuya.Cloud(
            apiRegion=TUYA_REGION,
            apiKey=TUYA_API_KEY,
            apiSecret=TUYA_API_SECRET,
            devId=TUYA_DEVICE_ID
        )
        # Tuya Cloud API förväntar sig en lista med kommandoobjekt i "commands"
        commands = {
            "commands": [
                {
                    "code": "switch",
                    "value": state
                }
            ]
        }
        response = cloud.sendcommand(TUYA_DEVICE_ID, commands)
        print(f"[TUYA SEND COMMAND RESULT]: {response}")
    except Exception as e:
        print(f"[TUYA ERROR] Kunde inte skicka kommando: {e}")
def get_tuya_device_status():
    """Hämtar aktuell status från Tuya-enheten (ON/OFF) robust oavsett kodnamn."""
    if not all([TUYA_API_KEY, TUYA_API_SECRET, TUYA_DEVICE_ID]):
        return False, "OFF"

    try:
        cloud = tinytuya.Cloud(
            apiRegion=TUYA_REGION,
            apiKey=TUYA_API_KEY,
            apiSecret=TUYA_API_SECRET,
            devId=TUYA_DEVICE_ID
        )
        status_data = cloud.getstatus(TUYA_DEVICE_ID)

        if status_data and status_data.get("success"):
            result = status_data.get("result", [])
            if isinstance(result, list):
                for item in result:
                    code = item.get("code", "")
                    if "switch" in code.lower():
                        is_on = item.get("value", False)
                        return True, ("ON" if is_on else "OFF")
            return True, "ON"
    except Exception as e:
        print(f"[TUYA STATUS ERROR]: {e}")
    
    return False, "OFF"

# --- PULP OPTIMERINGSMOTOR ---
def generate_daily_schedule(prices_96, max_off_blocks=12, min_on_blocks=44, current_temp=15.0, cold_threshold=5.0, min_minutes_cold=15, price_cap=50.0):
    if not prices_96 or len(prices_96) != 96:
        raise ValueError("Måste skicka in exakt 96 kvartsspriser.")

    prob = pulp.LpProblem("Varmekostnad_Minimering", pulp.LpMinimize)
    x = pulp.LpVariable.dicts("slot", range(96), cat=pulp.LpBinary)
    
    prob += pulp.lpSum([x[i] * prices_96[i] for i in range(96)])
    prob += pulp.lpSum([x[i] for i in range(96)]) >= min_on_blocks

    for i in range(96 - max_off_blocks):
        prob += pulp.lpSum([x[j] for j in range(i, i + max_off_blocks + 1)]) >= 1

    if price_cap is not None:
        for i in range(96):
            if prices_96[i] <= price_cap:
                prob += x[i] == 1

    if current_temp <= cold_threshold and min_minutes_cold > 0:
        blocks_per_hour_required = max(1, min_minutes_cold // 15)
        for hour in range(24):
            start_slot = hour * 4
            end_slot = start_slot + 4
            prob += pulp.lpSum([x[j] for j in range(start_slot, end_slot)]) >= blocks_per_hour_required

    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    return [bool(x[i].varValue) for i in range(96)]

# --- HUVUDLOGIK ---
def main():
    try:
        config, history = load_gist_data()
        
        sweden_tz = ZoneInfo("Europe/Stockholm")
        now = datetime.now(sweden_tz)
        today_str = now.strftime("%Y-%m-%d")
        
        tomorrow_dt = now + timedelta(days=1)
        tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")

        if history:
            last_entry = history[-1]
            try:
                last_time = datetime.strptime(last_entry["timestamp"], "%Y-%m-%d %H:%M").replace(tzinfo=sweden_tz)
                if (now - last_time).total_seconds() / 60 < 8:
                    return
            except Exception:
                pass

        current_temp = get_outside_temp()
        
        max_off = config.get("max_off_blocks", 12)
        min_on = config.get("min_on_blocks", 44)
        price_cap = config.get("price_cap", 50.0)

        active_schedule_date = config.get("schedule_date")
        daily_schedule = config.get("daily_schedule")
        
        tomorrow_schedule_date = config.get("tomorrow_schedule_date")
        tomorrow_schedule = config.get("tomorrow_schedule")

        # 1. Hämta schema för IDAG om det saknas
        if active_schedule_date != today_str:
            if tomorrow_schedule_date == today_str and tomorrow_schedule:
                daily_schedule = tomorrow_schedule
                active_schedule_date = today_str
                config["tomorrow_schedule"] = None
                config["tomorrow_schedule_date"] = None
            else:
                prices_today = get_nordpool_price(zone="SE2", target_date=now)
                temp_forecast = get_hourly_temp_forecast(target_date=now)
                
                p_cap, m_blocks, reasoning = get_ai_optimized_parameters(
                    prices_today, temp_forecast, default_price_cap=50.0, default_min_on_blocks=min_on, target_day_label="idag"
                )
                
                daily_schedule = generate_daily_schedule(prices_today, max_off_blocks=max_off, min_on_blocks=m_blocks, current_temp=current_temp, price_cap=p_cap)
                active_schedule_date = today_str
                config["price_cap"] = p_cap
                config["min_on_blocks"] = m_blocks
                config["ai_latest_reasoning"] = reasoning

            config["schedule_date"] = active_schedule_date
            config["daily_schedule"] = daily_schedule
            save_config_to_gist(config)

        # 2. Hämta schema för IMORGON (endast efter kl 14 om det saknas)
        if now.hour >= 14 and tomorrow_schedule_date != tomorrow_str:
            prices_tomorrow = get_nordpool_price(zone="SE2", target_date=tomorrow_dt)
            temp_forecast_tom = get_hourly_temp_forecast(target_date=tomorrow_dt)
            
            if len(set(prices_tomorrow)) > 1: 
                time.sleep(2)
                
                p_cap_tom, m_blocks_tom, reasoning_tom = get_ai_optimized_parameters(
                    prices_tomorrow, temp_forecast_tom, default_price_cap=50.0, default_min_on_blocks=min_on, target_day_label="imorgon"
                )
                
                new_tomorrow_sched = generate_daily_schedule(
                    prices_tomorrow, max_off_blocks=max_off, min_on_blocks=m_blocks_tom, current_temp=current_temp, price_cap=p_cap_tom
                )
                
                config["tomorrow_schedule_date"] = tomorrow_str
                config["tomorrow_schedule"] = new_tomorrow_sched
                config["ai_latest_reasoning"] = reasoning_tom
                save_config_to_gist(config)

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
            if current_price <= price_cap:
                should_be_on = True
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

        # Skicka kommandot till Tuya
        set_tuya_switch(should_be_on)
        
        # Läs av den faktiska statusen från Tuya efter kommandot för att säkerställa att det gick igenom
        _, actual_tuya_status = get_tuya_device_status()
        
        log_entry = {
            "timestamp": now.strftime("%Y-%m-%d %H:%M"),
            "price": round(current_price, 2),
            "status": actual_tuya_status,  # Sparar den bekräftade statusen här
            "temp": round(current_temp, 1)
        }
        history.append(log_entry)
        save_history(history)
    except Exception as e:
        print(f"[FATAL ERROR] Skriptet kraschade: {e}")

if __name__ == "__main__":
    main()