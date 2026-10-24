import os
import json
import pulp
import requests
import time
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
        res = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=headers, timeout=10)
        if res.status_code == 200:
            files = res.json()["files"]
            config = json.loads(files["config.json"]["content"]) if "config.json" in files else {}
            history = json.loads(files["history.json"]["content"]) if "history.json" in files else []
            return config, history
        else:
            print(f"[WARNING] Gist hämtning svarade med statuskod {res.status_code}")
    except Exception as e:
        print(f"[ERROR] Fel vid hämtning från Gist: {e}")
    return {}, []

def save_config_to_gist(config_data):
    if not GIST_ID:
        print("[ERROR] GIST_ID saknas! Kan inte spara config.")
        return

    payload = {
        "files": {
            "config.json": {
                "content": json.dumps(config_data, indent=2)
            }
        }
    }
    
    for attempt in range(2):
        try:
            url = f"https://api.github.com/gists/{GIST_ID}"
            response = requests.patch(url, headers=headers, json=payload, timeout=10)
            if response.status_code == 200:
                return
            else:
                print(f"[WARNING] Försök {attempt+1}: Kunde inte spara config till Gist. Svar: {response.text}")
        except Exception as e:
            print(f"[WARNING] Försök {attempt+1} - Fel vid sparande av config till Gist: {e}")
    print("[ERROR] Misslyckades med att spara config till Gist efter flera försök.")

def save_history(history_data):
    if not GIST_ID:
        return

    trimmed_history = history_data[-200:]
    payload = {
        "files": {
            "history.json": {
                "content": json.dumps(trimmed_history, indent=2)
            }
        }
    }
    
    for attempt in range(2):
        try:
            url = f"https://api.github.com/gists/{GIST_ID}"
            response = requests.patch(url, headers=headers, json=payload, timeout=10)
            if response.status_code == 200:
                return
        except Exception as e:
            print(f"[WARNING] Försök {attempt+1} - Fel vid sparande av history till Gist: {e}")

# --- NORDPOOL API ---
# --- NORDPOOL API ---
def get_nordpool_price(zone="SE2", target_date=None):
    sweden_tz = ZoneInfo("Europe/Stockholm")
    if not target_date:
        target_date = datetime.now(sweden_tz)

    year = target_date.strftime("%Y")
    month = target_date.strftime("%m")
    day = target_date.strftime("%d")
    
    url = f"https://www.elprisetjustnu.se/api/v1/prices/{year}/{month}-{day}_{zone}.json"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    for attempt in range(2):
        try:
            res = requests.get(url, headers=headers, timeout=10)
            print(f"[DEBUG] Hämtar elpriser från {url} - Statuskod: {res.status_code}")
            
            if res.status_code == 200:
                data = res.json()
                if not isinstance(data, list) or len(data) == 0:
                    print(f"[WARNING] API returnerade tom lista eller ogiltigt format för {month}-{day}")
                    break

                prices = [round(item["SEK_per_kWh"] * 100, 2) for item in data]
                print(f"[DEBUG] Mottog {len(prices)} prisvärden från API.")

                if len(prices) == 96:
                    return prices
                elif len(prices) == 24:
                    prices_96 = []
                    for p in prices:
                        prices_96.extend([p] * 4)
                    return prices_96
                else:
                    # Om det är ett oväntat antal priser, fyll ut eller kapa till 96
                    print(f"[WARNING] Oväntat antal prisvärden ({len(prices)} st). Anpassar till 96 kvartar.")
                    while len(prices) < 96:
                        prices.append(prices[-1])
                    return prices[:96]
            else:
                print(f"[WARNING] ElprisetJustNu svarade med statuskod {res.status_code}.")
        except Exception as e:
            print(f"[WARNING] Försök {attempt+1} misslyckades vid hämtning av elpriser: {e}")
            if attempt == 1:
                print(f"[ERROR] Kunde inte hämta elpriser från ElprisetJustNu efter flera försök.")
        
    print(f"[ERROR] Kunde inte läsa priser för {month}-{day}, faller tillbaka på standardpris 50.0 öre.")
    return [50.0] * 96

# --- TEMPERATUR PROGNOS (OPEN-METEO) ---
def get_hourly_temp_forecast(lat=63.25, lon=14.56, target_date=None):
    sweden_tz = ZoneInfo("Europe/Stockholm")
    if not target_date:
        target_date = datetime.now(sweden_tz)
    
    target_str = target_date.strftime("%Y-%m-%d") if hasattr(target_date, "strftime") else str(target_date)

    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=temperature_2m&timezone=Europe%2FStockholm&forecast_days=3"
    
    for attempt in range(2):
        try:
            res = requests.get(url, timeout=10)
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
            break
        except Exception as e:
            print(f"[WARNING] Försök {attempt+1} misslyckades för Open-Meteo prognos: {e}")
            if attempt == 1:
                print(f"[ERROR] Kunde inte hämta timprognos från Open-Meteo efter flera försök.")
    
    current_t = get_outside_temp()
    return [current_t] * 24

# --- TEMPERATUR.NU API ---
def get_outside_temp():
    url = f"https://www.temperatur.nu/jstemp.php?s={TEMPERATUR_NU_STATION}"
    
    for attempt in range(2):
        try:
            res = requests.get(url, timeout=10)
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
            break
        except Exception as e:
            print(f"[WARNING] Försök {attempt+1} misslyckades för temperatur.nu: {e}")
    
    # Reservlösning om temperatur.nu sviker: Open-Meteo current
    try:
        lat, lon = 63.25, 14.56
        url_om = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m"
        res_om = requests.get(url_om, timeout=8)
        if res_om.status_code == 200:
            return float(res_om.json()["current"]["temperature_2m"])
    except Exception as e:
        print(f"[WARNING] Även reservmätning via Open-Meteo current misslyckades: {e}")

    return 5.0

# --- AI STRATEGEN (GEMINI) ---
def get_ai_optimized_parameters(prices_96, temp_hourly, default_price_cap=50.0, default_min_on_blocks=44, target_day_label="idag"):
    if not GEMINI_API_KEY or not GEMINI_AVAILABLE:
        print("[WARNING] Ingen AI-nyckel tillgänglig, använder standardparametrar.")
        return default_price_cap, default_min_on_blocks, "Ingen AI-nyckel tillgänglig."

    prompt = f"""
    Du är en expert på energistyrning och optimering av elvärme i Norrland.
    
    Här är elpriserna i öre/kWh för varje kvart (96 st) för {target_day_label}:
    {prices_96}
    
    Här är den förväntade utetemperaturen per timme (°C, 24 timmar) för {target_day_label}:
    {temp_hourly}
    
    Din uppgift är att analysera prisbilden och kylan för att bestämma två nyckelparametrar för just detta dygn:
    1. "price_cap": Pristak i öre/kWh (om priset understiger detta betraktas det som billigt och pannan ska köras).
    2. "min_on_hours": Totalt antal timmar som värmen absolut minst behöver vara igång under dygnet för att inte huset ska kyla ur.

    Regler och riktlinjer för körtid:
    - Vid en dygnsmedeltemperatur kring 0 °C bör cirka 7 timmars total körtid räcka.
    - Om det är kallare ute krävs fler timmar; om det är mildare kan det vara ännu färre.
    - Om priset har kraftiga dalar ska pristaket sättas så att vi nyttjar dalarna utan att bränna el på topparna.
    
    Svara ENBART med ett giltigt JSON-objekt enligt detta format:
    {{"price_cap": <flyttal>, "min_on_hours": <heltal>, "motivering": "<kort svensk förklaring på 1-2 meningar>"}}
    """

    max_retries = 3
    retry_delay = 5

    for attempt in range(1, max_retries + 1):
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
            print(f"[AI ERROR] Försök {attempt}/{max_retries} misslyckades: {error_msg}")
            if attempt < max_retries:
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                return default_price_cap, default_min_on_blocks, f"⚠️ AI-fel: {error_msg[:30]}"

# --- TUYA CLOUD STYRNING & STATUS ---
def set_tuya_switch(state: bool):
    if not all([TUYA_API_KEY, TUYA_API_SECRET, TUYA_DEVICE_ID]):
        print("[TUYA ERROR] Saknar API-nycklar för Tuya.")
        return
    
    for attempt in range(2):
        try:
            cloud = tinytuya.Cloud(
                apiRegion=TUYA_REGION,
                apiKey=TUYA_API_KEY,
                apiSecret=TUYA_API_SECRET,
                devId=TUYA_DEVICE_ID
            )
            
            success = False
            for code_name in ["switch", "switch_1"]:
                commands = {
                    "commands": [
                        {
                            "code": code_name,
                            "value": state
                        }
                    ]
                }
                response = cloud.sendcommand(TUYA_DEVICE_ID, commands)
                print(f"[TUYA CMD ({code_name})]: state={state}, response={response}")
                
                if response and (response.get("success") is True or response.get("result") is True):
                    success = True
                    break
            if success:
                return
        except Exception as e:
            print(f"[WARNING] Försök {attempt+1} - [TUYA ERROR] Kunde inte skicka kommando: {e}")
    print("[ERROR] Tuya kommando misslyckades efter flera försök.")

def get_tuya_device_status():
    if not all([TUYA_API_KEY, TUYA_API_SECRET, TUYA_DEVICE_ID]):
        return False, "OFF"

    for attempt in range(2):
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
                return True, "OFF"
        except Exception as e:
            print(f"[WARNING] Försök {attempt+1} - [TUYA STATUS ERROR]: {e}")
    
    return False, "OFF"

# --- PULP OPTIMERINGSMOTOR ---
def generate_daily_schedule(
    prices_96, 
    max_off_blocks=8, 
    min_on_blocks=28, 
    current_temp=15.0, 
    cold_threshold=5.0, 
    min_minutes_cold=15, 
    price_cap=50.0
):
    if not prices_96 or len(prices_96) != 96:
        raise ValueError("Måste skicka in exakt 96 kvartspriser.")

    prob = pulp.LpProblem("Varmekostnad_Minimering", pulp.LpMinimize)
    x = pulp.LpVariable.dicts("slot", range(96), cat=pulp.LpBinary)
    
    costs = []
    for i in range(96):
        price = prices_96[i]
        effective_cost = price - 50.0 if (price_cap is not None and price <= price_cap) else price
        costs.append(x[i] * effective_cost)
    prob += pulp.lpSum(costs)

    prob += pulp.lpSum([x[i] for i in range(96)]) >= min_on_blocks

    for i in range(96 - max_off_blocks):
        prob += pulp.lpSum([x[j] for j in range(i, i + max_off_blocks + 1)]) >= 1

    for i in range(1, 95):
        prob += x[i] <= x[i - 1] + x[i + 1]

    for i in range(4, 96):
        prev_price = prices_96[i - 4]
        curr_price = prices_96[i]
        if prev_price > 60.0 and (curr_price <= prev_price * 0.70 or (prev_price - curr_price) >= 30.0):
            end_slot = min(96, i + 3)
            for j in range(i, end_slot):
                prob += x[j] == 1

    if current_temp <= cold_threshold and min_minutes_cold > 0:
        blocks_per_hour_required = max(1, min_minutes_cold // 15)
        for hour in range(24):
            start_slot = hour * 4
            end_slot = start_slot + 4
            prob += pulp.lpSum([x[j] for j in range(start_slot, end_slot)]) >= blocks_per_hour_required

    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    return [bool(x[i].varValue) if x[i].varValue is not None else False for i in range(96)]

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
            except Exception as e:
                print(f"[WARNING] Kunde inte parsa sista historiktiden: {e}")

        current_temp = get_outside_temp()
        
        max_off = config.get("max_off_blocks", 12)
        min_on = config.get("min_on_blocks", 44)
        price_cap = config.get("price_cap", 50.0)

        active_schedule_date = config.get("schedule_date")
        daily_schedule = config.get("daily_schedule")
        last_ai_update_str = config.get("last_ai_update")
        
        tomorrow_schedule_date = config.get("tomorrow_schedule_date")
        tomorrow_schedule = config.get("tomorrow_schedule")

        # 1. Kontrollera om vi behöver räkna om (ny dag eller mer än 3 timmar sedan sist)
        needs_ai_update = False
        if active_schedule_date != today_str:
            if tomorrow_schedule_date == today_str and tomorrow_schedule:
                daily_schedule = tomorrow_schedule
                active_schedule_date = today_str
                config["tomorrow_schedule"] = None
                config["tomorrow_schedule_date"] = None
            else:
                needs_ai_update = True
        elif not last_ai_update_str:
            needs_ai_update = True
        else:
            try:
                last_update_dt = datetime.strptime(last_ai_update_str, "%Y-%m-%d %H:%M").replace(tzinfo=sweden_tz)
                if (now - last_update_dt).total_seconds() >= 3 * 3600:
                    needs_ai_update = True
            except Exception as e:
                print(f"[WARNING] Kunde inte parsa last_ai_update: {e}")
                needs_ai_update = True

        if needs_ai_update:
            print("[AI UPDATE] Kör automatisk omräkning av AI-parametrar och schema (3-timmarsintervall eller ny dag)...")
            prices_today = get_nordpool_price(zone="SE2", target_date=now)
            temp_forecast = get_hourly_temp_forecast(target_date=now)
            
            p_cap, m_blocks, reasoning = get_ai_optimized_parameters(
                prices_today, temp_forecast, default_price_cap=50.0, default_min_on_blocks=min_on, target_day_label="idag"
            )
            
            daily_schedule = generate_daily_schedule(
                prices_today, max_off_blocks=max_off, min_on_blocks=m_blocks, 
                current_temp=current_temp, price_cap=p_cap
            )
            
            active_schedule_date = today_str
            config["price_cap"] = p_cap
            config["min_on_blocks"] = m_blocks
            config["ai_latest_reasoning"] = reasoning
            config["last_ai_update"] = now.strftime("%Y-%m-%d %H:%M")

        config["schedule_date"] = active_schedule_date
        config["daily_schedule"] = daily_schedule
        save_config_to_gist(config)

        # 2. Hämta morgondagens schema efter kl 14 (isolerat med try/except så det inte kraschar dagsstyrningen)
        try:
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
        except Exception as e:
            print(f"[ERROR] Misslyckades med att generera/spara morgondagens schema: {e}")

        prices_today = get_nordpool_price(zone="SE2", target_date=now)
        quarter_index = (now.hour * 4) + (now.minute // 15)
        current_price = prices_today[quarter_index] if quarter_index < len(prices_today) else 50.0

        # Utvärdera styrlogik och skapa tydlig motivering
        override = config.get("force_override", "AUTO")
        should_be_on = False
        reason = "Okänd anledning"
        
        cold_threshold = config.get("cold_temp_threshold", 0)
        min_mins_cold = config.get("min_minutes_cold", 15)

        if override == "PÅ (Tvinga igång)":
            should_be_on = True
            reason = "Manuell överstyrning: Tvinga PÅ"
        elif override == "AV (Stäng av allt)":
            should_be_on = False
            reason = "Manuell överstyrning: Stäng AV"
        else:
            if current_price <= price_cap:
                should_be_on = True
                reason = f"Priset ({current_price:.1f} öre) understiger pristak ({price_cap:.1f} öre)"
            else:
                if daily_schedule and len(daily_schedule) == 96:
                    should_be_on = daily_schedule[quarter_index]
                    reason = f"Optimeringstabell (PuLP): Status är {'PÅ' if should_be_on else 'AV'} vid pris {current_price:.1f} öre"
                else:
                    should_be_on = True
                    reason = "Inget schema tillgängligt, kör som standard"

            if current_temp <= cold_threshold and min_mins_cold > 0:
                if now.minute < min_mins_cold:
                    should_be_on = True
                    reason = f"Köldskydd aktivt (Temp: {current_temp}°C <= {cold_threshold}°C, minut {now.minute} < {min_mins_cold})"

        print(f"[STYRLED] Tid: {now.strftime('%H:%M')} | Beslut: {'PÅ' if should_be_on else 'AV'} | Orsak: {reason}")

        # Skicka kommando till reläet
        set_tuya_switch(should_be_on)
        
        # Ge Tuya-molnet 5 sekund att synka
        time.sleep(5)
        
        # Sätt status baserat på vad vi skickade (för att slippa att molnet släpar efter i loggen)
        final_status = "ON" if should_be_on else "OFF"
        
        log_entry = {
            "timestamp": now.strftime("%Y-%m-%d %H:%M"),
            "price": round(current_price, 2),
            "status": final_status,
            "temp": round(current_temp, 1),
            "reason": reason
        }
        history.append(log_entry)
        save_history(history)
    except Exception as e:
        print(f"[FATAL ERROR] Skriptet kraschade i main(): {e}")

if __name__ == "__main__":
    main()