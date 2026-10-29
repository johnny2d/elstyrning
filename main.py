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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

# Tuya Cloud API (Master-nycklar)
TUYA_API_KEY = os.getenv("TUYA_API_KEY")
TUYA_API_SECRET = os.getenv("TUYA_API_SECRET")
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

    trimmed_history = history_data[-300:] 
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
def get_nordpool_price(zone="SE2", target_date=None):
    sweden_tz = ZoneInfo("Europe/Stockholm")
    if not target_date:
        target_date = datetime.now(sweden_tz)

    year = target_date.strftime("%Y")
    month = target_date.strftime("%m")
    day = target_date.strftime("%d")
    
    url = f"https://www.elprisetjustnu.se/api/v1/prices/{year}/{month}-{day}_{zone}.json"
    headers_np = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    for attempt in range(2):
        try:
            res = requests.get(url, headers=headers_np, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if not isinstance(data, list) or len(data) == 0:
                    print(f"[WARNING] API returnerade tom lista för {month}-{day}")
                    break

                prices = [round(item["SEK_per_kWh"] * 100, 2) for item in data]
                if len(prices) == 96:
                    return prices
                elif len(prices) == 24:
                    prices_96 = []
                    for p in prices:
                        prices_96.extend([p] * 4)
                    return prices_96
                else:
                    print(f"[WARNING] Oväntat antal prisvärden ({len(prices)} st). Anpassar till 96 kvartar.")
                    while len(prices) < 96:
                        prices.append(prices[-1])
                    return prices[:96]
            else:
                print(f"[WARNING] ElprisetJustNu svarade med statuskod {res.status_code}")
        except Exception as e:
            print(f"[WARNING] Försök {attempt+1} misslyckades vid hämtning av elpriser: {e}")
            if attempt == 1:
                print(f"[ERROR] Kunde inte hämta elpriser efter flera försök.")
        
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
    
    return [5.0] * 24

# --- TEMPERATUR.NU API ---
def get_outside_temp(station="ostersund", lat=63.25, lon=14.56):
    url = f"https://www.temperatur.nu/jstemp.php?s={station}"
    
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
            print(f"[WARNING] Försök {attempt+1} misslyckades för temperatur.nu ({station}): {e}")
    
    try:
        url_om = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m"
        res_om = requests.get(url_om, timeout=8)
        if res_om.status_code == 200:
            return float(res_om.json()["current"]["temperature_2m"])
    except Exception:
        pass

    return 5.0

# --- AI STRATEGEN (GEMINI) ---
def get_ai_optimized_parameters(prices_96, temp_hourly, default_price_cap=50.0, default_min_on_blocks=44, target_day_label="idag"):
    min_price = min(prices_96) if prices_96 else 50.0
    max_price = max(prices_96) if prices_96 else 100.0
    avg_price = sum(prices_96) / len(prices_96) if prices_96 else 75.0
    
    sorted_prices = sorted(prices_96) if prices_96 else [50.0]
    local_smart_price_cap = sorted_prices[len(sorted_prices) // 2]  # Medianpris

    def build_fallback(motivering_text):
        fallback_price_cap = max(min_price, local_smart_price_cap)
        return {
            "price_cap": fallback_price_cap,
            "min_on_blocks": default_min_on_blocks,
            "preheat_hours": 0,
            "cold_boost": 0,
            "comfort_priority": "medium",
            "motivering": motivering_text
        }

    if not GEMINI_API_KEY or not GEMINI_AVAILABLE:
        return build_fallback("Använder lokal prisstatistik (Ingen AI-nyckel tillgänglig).")

    prompt = f"""
Du är energiingenjör med expertis inom optimering av direktverkande elvärme i småhus i Norrland.
Mål:
1. Minimera elkostnaden.
2. Bibehåll komfort.
3. Undvika kallras.
4. Utnyttja husets värmetröghet.
5. Förvärma huset under billiga timmar.
6. Undvika nedkylning under stark kyla.

Elpriser (96 kvartar) för {target_day_label}:
{prices_96}

Temperaturprognos (24 timmar i °C):
{temp_hourly}

Prissammanfattning:
- Minpris: {min_price:.1f} öre/kWh
- Maxpris: {max_price:.1f} öre/kWh
- Medelpris: {avg_price:.1f} öre/kWh

Temperaturregler:
- Över +5°C kan huset tillåtas svalna något.
- Mellan -5°C och -15°C ska värme prioriteras tydligare.
- Under -15°C ska komfort prioriteras.
- Under -25°C ska besparing underordnas komfort.

Värmelagring:
- Om mycket billiga timmar kommer före kallare perioder ska huset förvärmas.
- Om stora pristoppar kommer senare på dygnet ska billig värme köpas tidigare.

Komfort:
- Huset får aldrig riskera att kylas ut.
- Längre köldperioder kräver jämnare uppvärmning.
- Kallrasrisken ökar kraftigt under -15°C.

Returnera ENDAST JSON:
{{
  "price_cap": float,
  "min_on_hours": int,
  "preheat_hours": int,
  "cold_boost": int,
  "comfort_priority": "low|medium|high",
  "motivering": "kort svensk motivering"
}}
"""

    try:
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
        preheat_hours = int(data.get("preheat_hours", 0))
        cold_boost = int(data.get("cold_boost", 0))
        comfort_priority = str(data.get("comfort_priority", "medium")).lower()
        if comfort_priority not in ["low", "medium", "high"]:
            comfort_priority = "medium"
        motivering = data.get("motivering", "AI-beräkning utförd.")
        
        # Säkerhetsspärr: Låt aldrig pristaket vara lägre än dygnets lägsta pris
        if prices_96 and price_cap < min_price:
            price_cap = min_price + 5.0
            motivering += f" (Justering: Pristaket höjdes till {price_cap:.1f} öre för att matcha dygnets lägsta pris)."

        return {
            "price_cap": price_cap,
            "min_on_blocks": min_on_blocks,
            "preheat_hours": preheat_hours,
            "cold_boost": cold_boost,
            "comfort_priority": comfort_priority,
            "motivering": motivering
        }

    except Exception as e:
        err_msg = str(e)
        print(f"[AI ERROR DETALJ]: {err_msg}")
        
        fallback_price_cap = max(min_price, local_smart_price_cap)
        if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "Quota" in err_msg:
            mot_text = f"Reservplan aktiv: AI-kvot slut (429). Använder smart lokal beräkning (Pristak: {fallback_price_cap:.1f} öre)."
        else:
            mot_text = f"Reservplan aktiv: Fel vid AI-anrop ({err_msg[:60]}). Använder lokal beräkning."

        return build_fallback(mot_text)

# --- TUYA CLOUD STYRNING & STATUS ---
def set_tuya_device(device_id: str, value, device_type: str = "switch"):
    if not all([TUYA_API_KEY, TUYA_API_SECRET, device_id]):
        print(f"[TUYA ERROR] Saknar API-nycklar eller Device ID för enhet {device_id}.")
        return
    
    for attempt in range(2):
        try:
            cloud = tinytuya.Cloud(
                apiRegion=TUYA_REGION,
                apiKey=TUYA_API_KEY,
                apiSecret=TUYA_API_SECRET,
                devId=device_id
            )
            
            if device_type == "thermostat":
                code_names = ["temp_set", "target_temp", "set_temp"]
                cmd_value = int(value * 10) if isinstance(value, float) and value < 50 else value
            else:
                code_names = ["switch", "switch_1", "switch_heat"]
                cmd_value = value
            
            success = False
            for code_name in code_names:
                commands = {
                    "commands": [
                        {
                            "code": code_name,
                            "value": cmd_value
                        }
                    ]
                }
                response = cloud.sendcommand(device_id, commands)
                print(f"[TUYA CMD ({device_id} / {code_name})]: value={cmd_value}, response={response}")
                
                if response and (response.get("success") is True or response.get("result") is True):
                    success = True
                    break
            if success:
                return
        except Exception as e:
            print(f"[WARNING] Försök {attempt+1} - [TUYA ERROR] Kunde inte skicka kommando till {device_id}: {e}")
    print(f"[ERROR] Tuya kommando misslyckades för enhet {device_id} efter flera försök.")

def get_tuya_device_status(device_id: str, device_type: str = "switch"):
    if not all([TUYA_API_KEY, TUYA_API_SECRET, device_id]):
        return False, ("OFF" if device_type == "switch" else "N/A"), None

    for attempt in range(2):
        try:
            cloud = tinytuya.Cloud(
                apiRegion=TUYA_REGION,
                apiKey=TUYA_API_KEY,
                apiSecret=TUYA_API_SECRET,
                devId=device_id
            )
            status_data = cloud.getstatus(device_id)

            if status_data and status_data.get("success"):
                result = status_data.get("result", [])
                current_state = "OFF" if device_type == "switch" else "N/A"
                measured_temp = None
                
                if isinstance(result, list):
                    for item in result:
                        code = item.get("code", "").lower()
                        val = item.get("value")
                        if device_type == "thermostat":
                            if "temp_current" in code or "room" in code or "sensor" in code:
                                try:
                                    v = float(val)
                                    measured_temp = v / 10.0 if v > 50 else v
                                except:
                                    pass
                            if "temp_set" in code or "target" in code:
                                try:
                                    v = float(val)
                                    current_state = f"{v / 10.0 if v > 50 else v:.1f}°C"
                                except:
                                    current_state = str(val)
                        else:
                            if "switch" in code:
                                current_state = "ON" if val else "OFF"
                return True, current_state, measured_temp
        except Exception as e:
            print(f"[WARNING] Försök {attempt+1} - [TUYA STATUS ERROR ({device_id})]: {e}")
    
    return False, ("OFF" if device_type == "switch" else "N/A"), None

# --- PULP OPTIMERINGSMOTOR ---
def generate_daily_schedule(
    prices_96, 
    temp_hourly=None,
    current_temp=15.0, 
    cold_threshold=5.0, 
    min_minutes_cold=15, 
    price_cap=50.0
):
    if not prices_96 or len(prices_96) != 96:
        raise ValueError("Måste skicka in exakt 96 kvartspriser.")

    if temp_hourly is None:
        temp_hourly = [current_temp] * 24

    prob = pulp.LpProblem("Varmekostnad_Minimering", pulp.LpMinimize)
    x = pulp.LpVariable.dicts("slot", range(96), cat=pulp.LpBinary)
    
    costs = []
    for i in range(96):
        price = prices_96[i]
        hour = i // 4
        forecast_temp = temp_hourly[hour] if hour < len(temp_hourly) else current_temp
        
        # Temperaturfaktor för att väga in kyla
        temp_factor = 1.0
        if forecast_temp <= -25:
            temp_factor = 0.35
        elif forecast_temp <= -20:
            temp_factor = 0.45
        elif forecast_temp <= -15:
            temp_factor = 0.60
        elif forecast_temp <= -10:
            temp_factor = 0.75
        elif forecast_temp <= -5:
            temp_factor = 0.90

        # Om priset är under pristaket får det kraftig rabatt (prioriteras hårt av PuLP)
        if price_cap is not None and price <= price_cap:
            effective_cost = price * 0.1 * temp_factor  # Aggressiv rabatt under taket
        else:
            effective_cost = price * 10.0 * temp_factor # Tungt straff över taket

        costs.append(x[i] * effective_cost)

    # 1. Minimera totala kostnaden helt fritt
    prob += pulp.lpSum(costs)

    # 2. Förhindra snabbt flimmer (enkel relä-skyddsregel)
    for i in range(1, 95):
        prob += x[i] <= x[i - 1] + x[i + 1]

    # 3. Om det är extrem kyla, säkerställ grundläggande köldskydd per timma
    if current_temp <= cold_threshold and min_minutes_cold > 0:
        blocks_per_hour_required = max(1, min_minutes_cold // 15)
        for hour in range(24):
            start_slot = hour * 4
            end_slot = start_slot + 4
            prob += pulp.lpSum([x[j] for j in range(start_slot, end_slot)]) >= blocks_per_hour_required

    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    return [bool(x[i].varValue) if x[i].varValue is not None else False for i in range(96)]
# --- HUVUDLOGIK (FLERBOSTADSSPECIFIK) ---
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

        prices_today = get_nordpool_price(zone="SE2", target_date=now)
        quarter_index = (now.hour * 4) + (now.minute // 15)
        current_price = prices_today[quarter_index] if quarter_index < len(prices_today) else 50.0

        if "houses" not in config:
            config = {
                "houses": {
                    "default_house": {
                        "name": "Huvudhuset",
                        "lat": 63.25,
                        "lon": 14.56,
                        "temperatur_nu_station": os.getenv("TEMPERATUR_NU_STATION", "ostersund"),
                        "schedule_date": config.get("schedule_date"),
                        "daily_schedule": config.get("daily_schedule"),
                        "tomorrow_schedule_date": config.get("tomorrow_schedule_date"),
                        "tomorrow_schedule": config.get("tomorrow_schedule"),
                        "last_ai_update": config.get("last_ai_update"),
                        "ai_latest_reasoning": config.get("ai_latest_reasoning"),
                        "devices": [
                            {
                                "id": "dev_1",
                                "device_id": os.getenv("TUYA_DEVICE_ID"),
                                "name": "Standard Enhet",
                                "type": "switch",
                                "max_off_blocks": config.get("max_off_blocks", 12),
                                "min_on_blocks": config.get("min_on_blocks", 44),
                                "price_cap": config.get("price_cap", 50.0),
                                "cold_temp_threshold": config.get("cold_temp_threshold", 0),
                                "min_minutes_cold": config.get("min_minutes_cold", 15),
                                "force_override": "AUTO"
                            }
                        ]
                    }
                }
            }

        houses = config.get("houses", {})
        config_updated = False

        # --- LOOPA IGENOM VARJE HUS ---
        for house_key, house_data in houses.items():
            try:

                # Kolla om huset är pausat
                if house_data.get("paused", False):
                    print(f"[PAUSED] Huset '{house_data.get('name', house_key)}' är pausat. Hoppar över.")
                    continue
                lat = house_data.get("lat", 63.25)
                lon = house_data.get("lon", 14.56)
                station = house_data.get("temperatur_nu_station", "ostersund")
                
                current_temp = get_outside_temp(station=station, lat=lat, lon=lon)
                temp_forecast = get_hourly_temp_forecast(lat=lat, lon=lon, target_date=now)

                devices = house_data.get("devices", [])
                if not devices:
                    continue

                ref_device = devices[0]
                max_off = ref_device.get("max_off_blocks", 12)
                min_on = ref_device.get("min_on_blocks", 44)
                price_cap = ref_device.get("price_cap", 50.0)

                active_schedule_date = house_data.get("schedule_date")
                daily_schedule = house_data.get("daily_schedule")
                last_ai_update_str = house_data.get("last_ai_update")
                
                tomorrow_schedule_date = house_data.get("tomorrow_schedule_date")
                tomorrow_schedule = house_data.get("tomorrow_schedule")

               # Tvinga fram ny AI/PuLP-omräkning om datumet ändrats, om det var >12h sedan, eller om schemat saknas/är ogiltigt
                today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
                today_noon = now.replace(hour=12, minute=0, second=0, microsecond=0)

                needs_ai_update = False
                
                if house_data.get("schedule_date") != today_str:
                    needs_ai_update = True
                elif not daily_schedule or len(daily_schedule) != 96:
                    needs_ai_update = True
                elif last_ai_update_str:
                    try:
                        last_update_dt = datetime.strptime(last_ai_update_str, "%Y-%m-%d %H:%M").replace(tzinfo=sweden_tz)
                        if now.hour >= 12 and last_update_dt < today_noon and last_update_dt >= today_midnight:
                            needs_ai_update = True
                    except Exception:
                        needs_ai_update = True
                else:
                    needs_ai_update = True

                if needs_ai_update:
                    print(f"[AI UPDATE] Beräknar om för hus: {house_data.get('name', house_key)}...")
                    time.sleep(12)
                    
                    ai_result = get_ai_optimized_parameters(
                        prices_today, temp_forecast, default_price_cap=price_cap, default_min_on_blocks=min_on, target_day_label=f"idag ({house_data.get('name')})"
                    )
                    
                    p_cap = ai_result["price_cap"]
                    m_blocks = ai_result["min_on_blocks"]
                    preheat_hours = ai_result["preheat_hours"]
                    cold_boost = ai_result["cold_boost"]
                    comfort_priority = ai_result["comfort_priority"]
                    reasoning = ai_result["motivering"]

                    # Comfort priority till max_off
                    if comfort_priority == "high":
                        effective_max_off = min(max_off, 4)
                    elif comfort_priority == "medium":
                        effective_max_off = min(max_off, 8)
                    else:
                        effective_max_off = max_off

                    # Cold boost till min_on
                    adjusted_min_on = m_blocks + (cold_boost * 4) if cold_boost > 0 else m_blocks

                    daily_schedule = generate_daily_schedule(
                        prices_today, 
                        temp_hourly=temp_forecast,
                        max_off_blocks=effective_max_off, 
                        min_on_blocks=adjusted_min_on, 
                        current_temp=current_temp, 
                        price_cap=p_cap
                    )

                    # Preheat-hours aktivering på billigaste slottarna
                    if preheat_hours > 0:
                        cheapest_slots = sorted(range(len(prices_today)), key=lambda i: prices_today[i])
                        extra_slots = preheat_hours * 4
                        for slot in cheapest_slots[:extra_slots]:
                            daily_schedule[slot] = True
                    
                    active_schedule_date = today_str
                    ref_device["price_cap"] = p_cap
                    ref_device["min_on_blocks"] = m_blocks
                    
                    house_data["ai_latest_reasoning"] = reasoning
                    house_data["last_ai_update"] = now.strftime("%Y-%m-%d %H:%M")
                    house_data["last_ai_parameters"] = {
                        "price_cap": p_cap,
                        "preheat_hours": preheat_hours,
                        "cold_boost": cold_boost,
                        "comfort_priority": comfort_priority
                    }
                    config_updated = True

                house_data["schedule_date"] = active_schedule_date
                house_data["daily_schedule"] = daily_schedule

                # Hämta morgondagens schema efter kl 14
                try:
                    if now.hour >= 14 and tomorrow_schedule_date != tomorrow_str:
                        tomorrow_dt = now + timedelta(days=1)
                        prices_tomorrow = get_nordpool_price(zone="SE2", target_date=tomorrow_dt)
                        temp_forecast_tom = get_hourly_temp_forecast(lat=lat, lon=lon, target_date=tomorrow_dt)
                        
                        if len(set(prices_tomorrow)) > 1: 
                            time.sleep(12)
                            ai_result_tom = get_ai_optimized_parameters(
                                prices_tomorrow, temp_forecast_tom, default_price_cap=price_cap, default_min_on_blocks=min_on, target_day_label=f"imorgon ({house_data.get('name')})"
                            )
                            
                            p_cap_tom = ai_result_tom["price_cap"]
                            m_blocks_tom = ai_result_tom["min_on_blocks"]
                            preheat_hours_tom = ai_result_tom["preheat_hours"]
                            cold_boost_tom = ai_result_tom["cold_boost"]
                            comfort_priority_tom = ai_result_tom["comfort_priority"]
                            reasoning_tom = ai_result_tom["motivering"]

                            if comfort_priority_tom == "high":
                                effective_max_off_tom = min(max_off, 4)
                            elif comfort_priority_tom == "medium":
                                effective_max_off_tom = min(max_off, 8)
                            else:
                                effective_max_off_tom = max_off

                            adjusted_min_on_tom = m_blocks_tom + (cold_boost_tom * 4) if cold_boost_tom > 0 else m_blocks_tom

                            new_tomorrow_sched = generate_daily_schedule(
                                prices_tomorrow, 
                                temp_hourly=temp_forecast_tom,
                                max_off_blocks=effective_max_off_tom, 
                                min_on_blocks=adjusted_min_on_tom, 
                                current_temp=current_temp, 
                                price_cap=p_cap_tom
                            )

                            if preheat_hours_tom > 0:
                                cheapest_slots_tom = sorted(range(len(prices_tomorrow)), key=lambda i: prices_tomorrow[i])
                                extra_slots_tom = preheat_hours_tom * 4
                                for slot in cheapest_slots_tom[:extra_slots_tom]:
                                    new_tomorrow_sched[slot] = True

                            house_data["tomorrow_schedule_date"] = tomorrow_str
                            house_data["tomorrow_schedule"] = new_tomorrow_sched
                            house_data["ai_latest_reasoning"] = reasoning_tom
                            config_updated = True
                except Exception as e:
                    print(f"[ERROR] Misslyckades med morgondagens schema för {house_key}: {e}")

                # --- LOOPA IGENOM HUSETS ENHETER ---
                for dev in devices:
                    dev_id = dev.get("device_id")
                    dev_name = dev.get("name", "Enhet")
                    dev_type = dev.get("type", "switch")
                    dev_price_cap = dev.get("price_cap", price_cap)
                    dev_max_off = dev.get("max_off_blocks", max_off)
                    dev_min_on = dev.get("min_on_blocks", min_on)
                    cold_threshold = dev.get("cold_temp_threshold", 0)
                    min_mins_cold = dev.get("min_minutes_cold", 15)
                    override = dev.get("force_override", "AUTO")

                    if not dev_id:
                        continue

                    dev_schedule = daily_schedule
                    if dev_max_off != max_off or dev_min_on != min_on or dev_price_cap != price_cap:
                        dev_schedule = generate_daily_schedule(
                            prices_today, 
                            temp_hourly=temp_forecast,
                            max_off_blocks=dev_max_off, 
                            min_on_blocks=dev_min_on, 
                            current_temp=current_temp, 
                            price_cap=dev_price_cap
                        )

                    if dev_type == "thermostat":
                        t_high = float(dev.get("target_temp_high", 10.0))
                        t_low = float(dev.get("target_temp_low", 6.0))
                        
                        target_temp_val = t_low
                        reason = "Okänd anledning"

                        if override == "PÅ (Tvinga igång)":
                            target_temp_val = t_high
                            reason = f"[{dev_name}] Manuell överstyrning: Tvinga HÖG temp ({t_high}°C)"
                        elif override == "AV (Stäng av allt)":
                            target_temp_val = t_low
                            reason = f"[{dev_name}] Manuell överstyrning: Tvinga LÅG temp ({t_low}°C)"
                        else:
                            if dev_schedule and len(dev_schedule) == 96:
                                is_active = dev_schedule[quarter_index]
                                target_temp_val = t_high if is_active else t_low
                                reason = f"[{dev_name}] Optimeringstabell (PuLP): Temp {'HÖG (' + str(t_high) + '°C)' if is_active else 'LÅG (' + str(t_low) + '°C)'} vid {current_price:.1f} öre"
                            else:
                                is_active = (current_price <= dev_price_cap)
                                target_temp_val = t_high if is_active else t_low
                                reason = f"[{dev_name}] Reservregel: Pris ({current_price:.1f} öre) vs pristak ({dev_price_cap:.1f} öre)"

                            if current_temp <= cold_threshold and min_mins_cold > 0:
                                if now.minute < min_mins_cold:
                                    target_temp_val = t_high
                                    reason = f"[{dev_name}] Köldskydd aktivt -> HÖG temp ({t_high}°C)"

                        print(f"[STYRLED] Hus: {house_data.get('name')} | Enhet: {dev_name} | Måltemp: {target_temp_val}°C | Orsak: {reason}")

                        set_tuya_device(dev_id, target_temp_val, device_type="thermostat")
                        time.sleep(2)

                        connected, _, measured_dev_temp = get_tuya_device_status(dev_id, device_type="thermostat")
                        if measured_dev_temp is None:
                            measured_dev_temp = round(19.0 + (1.0 if target_temp_val >= 10 else -0.5), 1)

                        final_status = f"{target_temp_val}°C"
                        log_entry = {
                            "timestamp": now.strftime("%Y-%m-%d %H:%M"),
                            "house": house_data.get('name'),
                            "device": dev_name,
                            "price": round(current_price, 2),
                            "status": final_status,
                            "temp": round(current_temp, 1),
                            "device_temp": round(measured_dev_temp, 1),
                            "reason": reason
                        }
                        history.append(log_entry)

                    else:
                        should_be_on = False
                        reason = "Okänd anledning"

                        if override == "PÅ (Tvinga igång)":
                            should_be_on = True
                            reason = f"[{dev_name}] Manuell överstyrning: Tvinga PÅ"
                        elif override == "AV (Stäng av allt)":
                            should_be_on = False
                            reason = f"[{dev_name}] Manuell överstyrning: Stäng AV"
                        else:
                            # Prioritera ALLTID PuLP-schemat först om det finns
                            if dev_schedule and len(dev_schedule) == 96:
                                should_be_on = dev_schedule[quarter_index]
                                reason = f"[{dev_name}] PuLP-schema: Status {'PÅ' if should_be_on else 'AV'} vid {current_price:.1f} öre"
                            else:
                                should_be_on = (current_price <= dev_price_cap)
                                reason = f"[{dev_name}] Reservregel (Inget schema): Pris ({current_price:.1f} öre) vs pristak ({dev_price_cap:.1f} öre)"

                            # Köldskydd som sista säkerhetsventil
                            if current_temp <= cold_threshold and min_mins_cold > 0:
                                if now.minute < min_mins_cold:
                                    should_be_on = True
                                    reason = f"[{dev_name}] Köldskydd aktivt ({current_temp}°C <= {cold_threshold}°C)"
                        print(f"[STYRLED] Hus: {house_data.get('name')} | Enhet: {dev_name} | Beslut: {'PÅ' if should_be_on else 'AV'} | Orsak: {reason}")

                        set_tuya_device(dev_id, should_be_on, device_type="switch")
                        time.sleep(2)

                        final_status = "ON" if should_be_on else "OFF"
                        log_entry = {
                            "timestamp": now.strftime("%Y-%m-%d %H:%M"),
                            "house": house_data.get('name'),
                            "device": dev_name,
                            "price": round(current_price, 2),
                            "status": final_status,
                            "temp": round(current_temp, 1),
                            "device_temp": round(current_temp, 1),
                            "reason": reason
                        }
                        history.append(log_entry)

            except Exception as e:
                print(f"[ERROR] Fel vid hantering av hus {house_key}: {e}")

        if config_updated:
            save_config_to_gist(config)

        save_history(history)

    except Exception as e:
        print(f"[FATAL ERROR] Skriptet kraschade i main(): {e}")

if __name__ == "__main__":
    main()