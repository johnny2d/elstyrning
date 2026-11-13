import os
import json
import math
import pulp
import requests
import time
import random
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

# Global minnescache så att samma elområde + datum ALDRIG hämtas mer än 1 gång per process
_PRICE_CACHE = {}

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

# --- NORDPOOL API (MED MINNESCACHE & SNABB RESERVTJÄNST) ---
def get_nordpool_price(zone="SE2", target_date=None):
    sweden_tz = ZoneInfo("Europe/Stockholm")
    if not target_date:
        target_date = datetime.now(sweden_tz)

    year = f"{target_date.year:04d}"
    month = f"{target_date.month:02d}"
    day = f"{target_date.day:02d}"
    zone_clean = zone.strip().upper()
    date_str = f"{year}-{month}-{day}"
    
    cache_key = (zone_clean, date_str)
    if cache_key in _PRICE_CACHE:
        return list(_PRICE_CACHE[cache_key])

    headers_np = {"User-Agent": "ElstyrningHomeAutomation/2.0"}

    # 1. Primär källa: ElprisetJustNu (Snabb timeout, inga 10s sovpauser)
    url = f"https://www.elprisetjustnu.se/api/v1/prices/{year}/{month}-{day}_{zone_clean}.json"
    print(f"[DEBUG] Hämtar elpriser (1 gång) från URL: {url}")
    
    for attempt in range(2):
        try:
            res = requests.get(url, headers=headers_np, timeout=4)
            if res.status_code == 200:
                data = res.json()
                prices = [round(item["SEK_per_kWh"] * 100, 2) for item in data]
                if len(prices) == 96:
                    _PRICE_CACHE[cache_key] = prices
                    return list(prices)
                elif len(prices) == 24:
                    prices_96 = [p for p in prices for _ in range(4)]
                    _PRICE_CACHE[cache_key] = prices_96
                    return list(prices_96)
            elif res.status_code == 404:
                print(f"[INFO] Priser för {date_str} ({zone_clean}) finns inte ännu (HTTP 404).")
                return [50.0] * 96
        except Exception as e:
            print(f"[WARNING] ElprisetJustNu försök {attempt+1} misslyckades: {e}")

    # 2. Snabb reservkälla om ElprisetJustNu har driftstörning: Spot-Hinta.fi (EUR -> SEK schablon ~11.50)
    try:
        sh_url = f"https://api.spot-hinta.fi/TodayAndDayForward?region={zone_clean}"
        res_sh = requests.get(sh_url, headers=headers_np, timeout=4)
        if res_sh.status_code == 200:
            sh_data = res_sh.json()
            day_items = [item for item in sh_data if str(item.get("DateTime", "")).startswith(date_str)]
            if len(day_items) == 96:
                prices = [round(float(item["PriceNoTax"]) * 1150.0, 2) for item in day_items]
                _PRICE_CACHE[cache_key] = prices
                return list(prices)
            elif len(day_items) == 24:
                prices_24 = [round(float(item["PriceNoTax"]) * 1150.0, 2) for item in day_items]
                prices_96 = [p for p in prices_24 for _ in range(4)]
                _PRICE_CACHE[cache_key] = prices_96
                return list(prices_96)
    except Exception as e:
        print(f"[WARNING] Reservkälla Spot-Hinta misslyckades: {e}")

    print("[INFO] Kunde inte hämta elpriser för datumet. Använder fallback.")
    return [50.0] * 96

# --- TEMPERATUR PROGNOS (SMHI -> MET NORWAY / YR -> OPEN-METEO) ---
def get_hourly_temp_forecast(lat=63.25, lon=14.56, target_date=None):
    sweden_tz = ZoneInfo("Europe/Stockholm")
    if not target_date:
        target_date = datetime.now(sweden_tz)
    
    target_str = target_date.strftime("%Y-%m-%d") if hasattr(target_date, "strftime") else str(target_date)
    headers_ua = {"User-Agent": "ElstyrningHomeAutomation/2.0 (kontakt@elstyrning.se)"}

    # 1. Primär källa: SMHI Öppna Data (PMP3g - mycket snabb och stabil i Sverige)
    try:
        smhi_url = f"https://opendata-download-metfcst.smhi.se/api/category/pmp3g/version/2/geotype/point/lon/{round(float(lon), 4)}/lat/{round(float(lat), 4)}/data.json"
        res = requests.get(smhi_url, headers=headers_ua, timeout=6)
        if res.status_code == 200:
            data = res.json()
            hourly_map = {}
            for entry in data.get("timeSeries", []):
                valid_time_utc = datetime.fromisoformat(entry["validTime"].replace("Z", "+00:00"))
                valid_time_swe = valid_time_utc.astimezone(sweden_tz)
                if valid_time_swe.strftime("%Y-%m-%d") == target_str:
                    for param in entry.get("parameters", []):
                        if param.get("name") == "t":
                            hourly_map[valid_time_swe.hour] = float(param["values"][0])
            
            if hourly_map:
                first_known = hourly_map[min(hourly_map.keys())]
                result = []
                last_val = first_known
                for h in range(24):
                    if h in hourly_map:
                        last_val = hourly_map[h]
                    result.append(last_val)
                print(f"[WEATHER] Hämtade 24h prognos från SMHI för {target_str}")
                return result
    except Exception as e:
        print(f"[WARNING] SMHI prognos misslyckades: {e}")

    # 2. Sekundär källa: MET Norway / Yr.no
    try:
        yr_url = f"https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={round(float(lat), 4)}&lon={round(float(lon), 4)}"
        res = requests.get(yr_url, headers=headers_ua, timeout=6)
        if res.status_code == 200:
            data = res.json()
            hourly_map = {}
            for entry in data.get("properties", {}).get("timeseries", []):
                valid_time_utc = datetime.fromisoformat(entry["time"].replace("Z", "+00:00"))
                valid_time_swe = valid_time_utc.astimezone(sweden_tz)
                if valid_time_swe.strftime("%Y-%m-%d") == target_str:
                    temp_val = entry["data"]["instant"]["details"]["air_temperature"]
                    hourly_map[valid_time_swe.hour] = float(temp_val)

            if hourly_map:
                first_known = hourly_map[min(hourly_map.keys())]
                result = []
                last_val = first_known
                for h in range(24):
                    if h in hourly_map:
                        last_val = hourly_map[h]
                    result.append(last_val)
                print(f"[WEATHER] Hämtade 24h prognos från MET Norway (Yr) för {target_str}")
                return result
    except Exception as e:
        print(f"[WARNING] MET Norway prognos misslyckades: {e}")

    # 3. Sista reserv: Open-Meteo (med kortare timeout så den inte låser körningen)
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=temperature_2m&timezone=Europe%2FStockholm&forecast_days=3"
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
        print(f"[WARNING] Open-Meteo reservprognos misslyckades: {e}")
    
    return [5.0] * 24

# --- UTETEMPERATUR NU (TEMPERATUR.NU -> SMHI -> MET NORWAY -> OPEN-METEO) ---
def get_outside_temp(station="ostersund", lat=63.25, lon=14.56):
    headers_ua = {"User-Agent": "ElstyrningHomeAutomation/2.0 (kontakt@elstyrning.se)"}

    # 1. Försök med Temperatur.nu
    if station:
        url = f"https://www.temperatur.nu/jstemp.php?s={station}"
        for attempt in range(2):
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
                        if -45 <= val <= 40:
                            return val
                break
            except Exception as e:
                print(f"[WARNING] Försök {attempt+1} misslyckades för temperatur.nu ({station}): {e}")

    # 2. Fallback 1: SMHI PMP3g
    try:
        smhi_url = f"https://opendata-download-metfcst.smhi.se/api/category/pmp3g/version/2/geotype/point/lon/{round(float(lon), 4)}/lat/{round(float(lat), 4)}/data.json"
        res_smhi = requests.get(smhi_url, headers=headers_ua, timeout=5)
        if res_smhi.status_code == 200:
            ts = res_smhi.json().get("timeSeries", [])
            if ts:
                for param in ts[0].get("parameters", []):
                    if param.get("name") == "t":
                        return float(param["values"][0])
    except Exception:
        pass

    # 3. Fallback 2: MET Norway (Yr.no)
    try:
        yr_url = f"https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={round(float(lat), 4)}&lon={round(float(lon), 4)}"
        res_yr = requests.get(yr_url, headers=headers_ua, timeout=5)
        if res_yr.status_code == 200:
            ts = res_yr.json().get("properties", {}).get("timeseries", [])
            if ts:
                return float(ts[0]["data"]["instant"]["details"]["air_temperature"])
    except Exception:
        pass

    # 4. Fallback 3: Open-Meteo
    try:
        url_om = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m"
        res_om = requests.get(url_om, timeout=4)
        if res_om.status_code == 200:
            return float(res_om.json()["current"]["temperature_2m"])
    except Exception:
        pass

    return 5.0

# --- BERÄKNA DRIFTTID BASERAT PÅ PROGNOS (DETERMINISTISKT) ---
def calculate_required_runtime(temp_forecast, current_temp=5.0):
    if not temp_forecast or len(temp_forecast) < 24:
        temp_forecast = [current_temp] * 24
        
    avg_temp = sum(temp_forecast) / len(temp_forecast)
    hours_below_zero = sum(1 for t in temp_forecast if t <= 0)
    hours_below_minus5 = sum(1 for t in temp_forecast if t <= -5)
    hours_below_minus15 = sum(1 for t in temp_forecast if t <= -15)
    hours_below_minus20 = sum(1 for t in temp_forecast if t <= -20)

    if hours_below_minus20 >= 8:
        return 20
    elif hours_below_minus15 >= 12:
        return 16
    elif hours_below_minus5 >= 8:
        return 12
    elif hours_below_zero >= 18:
        return 12
    elif hours_below_zero >= 8:
        return 10
    elif avg_temp < 5.0:
        return 8
    else:
        return 6

# --- AI STRATEGEN (GEMINI) ---
def get_ai_optimized_parameters(prices_96, temp_hourly, current_temp=5.0, target_temp=20.0, default_price_cap=50.0, target_day_label="idag", required_runtime_hours=None, **kwargs):
    min_price = min(prices_96) if prices_96 else 50.0
    max_price = max(prices_96) if prices_96 else 100.0
    avg_price = sum(prices_96) / len(prices_96) if prices_96 else 75.0
    
    sorted_prices = sorted(prices_96) if prices_96 else [50.0]
    local_smart_price_cap = sorted_prices[len(sorted_prices) // 2]
    avg_temp = sum(temp_hourly) / len(temp_hourly) if temp_hourly else current_temp

    if required_runtime_hours is None:
        required_runtime_hours = calculate_required_runtime(temp_hourly, current_temp=current_temp)

    def build_fallback(motivering_text):
        fallback_price_cap = max(min_price, local_smart_price_cap)
        return {
            "price_cap": fallback_price_cap,
            "max_consecutive_off_hours": 4 if avg_temp >= 0 else 2,
            "preheat_hours": 0,
            "required_runtime_hours": required_runtime_hours,
            "comfort_priority": "high" if avg_temp < -10 else "medium",
            "price_pattern": "volatile",
            "motivering": motivering_text
        }

    if not GEMINI_API_KEY or not GEMINI_AVAILABLE:
        return build_fallback("Använder lokal prisstatistik (Ingen AI-nyckel tillgänglig).")

    prompt = f"""
Du är en senior energiingenjör med expertis inom:
- direktverkande elvärme
- småhus i kallt nordiskt klimat
- värmetröghet i byggnader
- Nordpools kvartspriser
- energiekonomi
- komfortoptimering
- förvärmning och värmelagring

==================================================
MÅL
==================================================
Ditt uppdrag är att minimera den totala uppvärmningskostnaden utan att riskera att huset kyls ut.
Huset ska betraktas som ett värmelager.
Billig el ska användas för att lagra värme.
Dyr el ska undvikas när lagrad värme rimligen kan användas istället.
Det är viktigare att systemet är robust än att det hittar den absolut lägsta möjliga kostnaden.

==================================================
INDATA
==================================================
Elpriser (96 kvartar) för {target_day_label}:
{prices_96}

Temperaturprognos (24 timmar i °C):
{temp_hourly}

Prissammanfattning:
- Minpris: {min_price:.1f} öre/kWh
- Maxpris: {max_price:.1f} öre/kWh
- Medelpris: {avg_price:.1f} öre/kWh

==================================================
ANALYSERA FÖRST
==================================================
Identifiera:
1. Billiga prisfönster
2. Dyra prisfönster
3. Mycket dyra pristoppar
4. Långa sammanhängande dyrperioder
5. Dubbla lågprisperioder ("double valley")
6. Om en kall period följer efter en billig period
7. Om en dyr period sammanfaller med stark kyla
8. Om prisprofilen är jämn eller mycket volatil
9. Dela upp dygnet i tre delar: morgon, dag, kväll/natt och analysera prisprofilen för varje del.
10. Identifiera om det finns risk för att huset kyls ut under dygnets dyraste perioder.

==================================================
VIKTIGA PRINCIPER
==================================================
A) VÄRMELAGRING
Huset fungerar som ett värmelager.
Om flera billiga timmar föregår en dyr period ska värme flyttas till dessa billigare timmar.

B) LÅNGA DYRPERIODER
Om dyrperioden överstiger cirka 8 timmar ska extra försiktighet användas.

C) EKONOMISK REALISM
Modellen ska inte jaga högsta möjliga besparingsprocent om elpriset redan är mycket lågt utan prioritera komfort och robust drift.

D) TEMPERATUR OCH KOMFORT
Över +10°C: komfortbehovet är relativt lågt.
Mellan 5°C och -10°C: komfort och ekonomi ska balanseras.
Mellan -10°C och -20°C: komfortprioritet ska öka.
Under -20°C: komfort prioriteras högt.

G) SKYDDSREGLER
- Sätt aldrig price_cap lägre än dygnets lägsta pris.
- Sätt aldrig max_consecutive_off_hours högre än 6 timmar.
- Huset får aldrig riskera att kylas ut.

==================================================
MÅLTEMPERATUR
==================================================
Önskad inomhustemperatur: {target_temp}°C

==================================================
UTDATA
==================================================
Returnera ENDAST giltig JSON:
{{
  "price_cap": float,
  "max_consecutive_off_hours": int,
  "comfort_priority": "low|medium|high",
  "price_pattern": "flat|single_valley|double_valley|volatile",
  "motivering": "kort svensk motivering utan att nämna specifik drifttid"
}}
Returnera endast JSON och ingen annan text.
"""

    max_retries = 3
    delay = 5.0
    raw_text = None

    for attempt in range(max_retries):
        try:
            if GEMINI_AVAILABLE == "genai_new":
                client = genai.Client(api_key=GEMINI_API_KEY)
                response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
                raw_text = response.text
                break
            elif GEMINI_AVAILABLE == "genai_old":
                genai_old.configure(api_key=GEMINI_API_KEY)
                model = genai_old.GenerativeModel('gemini-1.5-flash')
                response = model.generate_content(prompt)
                raw_text = response.text
                break
        except Exception as e:
            err_str = str(e)
            if ("429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "503" in err_str or "UNAVAILABLE" in err_str) and attempt < max_retries - 1:
                sleep_time = delay + random.uniform(1.0, 3.0)
                print(f"[WARNING] Google API överbelastat (Försök {attempt+1}/{max_retries}). Väntar {sleep_time:.1f}s...")
                time.sleep(sleep_time)
                delay *= 2
            else:
                raise e

    try:
        clean_json = raw_text.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)
        print(
            f"[AI RAW] "
            f"max_off={data.get('max_consecutive_off_hours')} | "
            f"price_cap={data.get('price_cap')} | "
            f"preheat={data.get('preheat_hours')}"
        )
        price_cap = float(data.get("price_cap", default_price_cap))
        max_consecutive_off_hours = int(data.get("max_consecutive_off_hours", 4))
        preheat_hours = 0  # Låst till 0 så att PuLP-optimeringen inte skrivs över i efterhand
        comfort_priority = str(data.get("comfort_priority", "medium"))
        price_pattern = str(data.get("price_pattern", "volatile"))
        motivering = data.get("motivering", "AI-beräkning utförd.")
        
        if max_consecutive_off_hours > 6:
            max_consecutive_off_hours = 6

        if prices_96 and price_cap < min_price:
            price_cap = min_price + 5.0
            motivering += f" (Justering: Pristaket höjdes till {price_cap:.1f} öre för att matcha dygnets lägsta pris)."

        return {
            "price_cap": price_cap,
            "max_consecutive_off_hours": max_consecutive_off_hours,
            "preheat_hours": preheat_hours,
            "required_runtime_hours": required_runtime_hours,
            "comfort_priority": comfort_priority,
            "price_pattern": price_pattern,
            "motivering": motivering
        }

    except Exception as e:
        err_msg = str(e)
        print(f"[AI ERROR DETALJ]: {err_msg}")
        return build_fallback(f"Reservplan aktiv: Fel vid AI-anrop ({err_msg[:60]}).")

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

def get_tuya_device_status(device_id: str, device_type: str = "switch", include_battery: bool = False):
    if not all([TUYA_API_KEY, TUYA_API_SECRET, device_id]):
        return (False, ("OFF" if device_type == "switch" else "N/A"), None, None) if include_battery else (False, ("OFF" if device_type == "switch" else "N/A"), None)

    print(f"[TUYA STATUS] device_id={device_id} type={device_type}")
    for attempt in range(2):
        try:
            cloud = tinytuya.Cloud(
                apiRegion=TUYA_REGION,
                apiKey=TUYA_API_KEY,
                apiSecret=TUYA_API_SECRET,
                devId=device_id
            )
            
            # 1. Kontrollera om enheten faktiskt är ONLINE i Tuya Cloud (inte bara cachad status)
            try:
                dev_info = cloud.cloudrequest(f"/v1.0/devices/{device_id}")
                if isinstance(dev_info, dict) and dev_info.get("success") and isinstance(dev_info.get("result"), dict):
                    is_online = dev_info["result"].get("online", True)
                    if not is_online:
                        print(f"[TUYA OFFLINE] Enhet {device_id} rapporterar online=False i Tuya Cloud!")
                        if include_battery:
                            return False, "OFFLINE", None, None
                        return False, "OFFLINE", None
            except Exception as e_info:
                print(f"[TUYA INFO WARN] Kunde inte verifiera online-flagga för {device_id}: {e_info}")

            # 2. Hämta datapunkter (temperatur, switch-läge, batteri)
            status_data = cloud.getstatus(device_id)
            print(f"[TUYA RAW RESPONSE] {device_id}: {status_data}")

            if status_data and status_data.get("success"):
                result = status_data.get("result", [])
                current_state = "OFF" if device_type == "switch" else "N/A"
                measured_temp = None
                battery_level = None

                for item in result:
                    print(f"[TUYA DP] code={item.get('code')} value={item.get('value')}")
                
                if isinstance(result, list):
                    for item in result:
                        code = item.get("code", "").lower()
                        val = item.get("value")

                        BATTERY_CODES = ["va_battery", "battery_percentage", "battery_value", "battery", "residual_electricity"]
                        if code in BATTERY_CODES:
                            try:
                                battery_level = int(float(val))
                            except Exception:
                                battery_level = str(val)
                        elif code == "battery_state" and battery_level is None:
                            state_map = {"high": "Hög (100%)", "middle": "Medel (50%)", "low": "Låg ⚠️ (10%)"}
                            battery_level = state_map.get(str(val).lower(), str(val))

                        if device_type == "thermostat":
                            TEMPERATURE_CODES = ["va_temperature", "temp_current", "temperature", "room_temp", "sensor_temp"]
                            if code in TEMPERATURE_CODES:
                                try:
                                    v = float(val)
                                    measured_temp = v / 10.0 if v > 50 else v
                                except Exception:
                                    pass
                            if "temp_set" in code or "target" in code:
                                try:
                                    v = float(val)
                                    current_state = f"{v / 10.0 if v > 50 else v:.1f}°C"
                                except Exception:
                                    current_state = str(val)
                        else:
                            if "switch" in code:
                                current_state = "ON" if val else "OFF"

                if include_battery:
                    return True, current_state, measured_temp, battery_level
                return True, current_state, measured_temp

        except Exception as e:
            print(f"[WARNING] Försök {attempt+1} - [TUYA STATUS ERROR ({device_id})]: {e}")
    
    if include_battery:
        return False, ("OFF" if device_type == "switch" else "N/A"), None, None
    return False, ("OFF" if device_type == "switch" else "N/A"), None

# --- PULP OPTIMERINGSMOTOR MED LOKAL TOPPKAPNING ---
def generate_daily_schedule(
    prices_96, 
    temp_hourly=None,
    current_temp=15.0, 
    cold_threshold=5.0, 
    min_minutes_cold=15, 
    price_cap=50.0,
    required_runtime_hours=8,
    max_consecutive_off_hours=6
):

    print(
        f"[PULP START] "
        f"price_cap={price_cap} | "
        f"runtime={required_runtime_hours}h | "
        f"max_off={max_consecutive_off_hours}h | "
        f"utetemp={current_temp:.1f}C"
    )

    hours_below_zero = sum(1 for t in temp_hourly if t <= 0) if temp_hourly else 0
    hours_below_minus5 = sum(1 for t in temp_hourly if t <= -5) if temp_hourly else 0
    hours_below_minus15 = sum(1 for t in temp_hourly if t <= -15) if temp_hourly else 0
    hours_below_minus20 = sum(1 for t in temp_hourly if t <= -20) if temp_hourly else 0

    if hours_below_minus20 >= 8:
        max_consecutive_off_hours = min(max_consecutive_off_hours, 1)
    elif hours_below_minus15 >= 12:
        max_consecutive_off_hours = min(max_consecutive_off_hours, 2)
    elif hours_below_minus5 >= 8:
        max_consecutive_off_hours = min(max_consecutive_off_hours, 3)
    elif hours_below_zero >= 18:
        max_consecutive_off_hours = min(max_consecutive_off_hours, 4)
    elif hours_below_zero >= 8:
        max_consecutive_off_hours = min(max_consecutive_off_hours, 5)

    print(
        f"[PULP SAFETY] "
        f"hours_below_zero={hours_below_zero} | "
        f"hours_below_minus5={hours_below_minus5} | "
        f"hours_below_minus15={hours_below_minus15} | "
        f"runtime_after_rules={required_runtime_hours}h | "
        f"max_off_after_rules={max_consecutive_off_hours}h"
    )

    prob = pulp.LpProblem("Varmekostnad_Minimering", pulp.LpMinimize)
    x = pulp.LpVariable.dicts("slot", range(96), cat=pulp.LpBinary)

    # --- KOSTNADSBERÄKNING MED LOKAL TOPPKAPNING (GLIDANDE FÖNSTER) ---
    is_flat_fallback = (len(set(prices_96)) <= 1)
    costs = []
    for i in range(96):
        price = prices_96[i]
        
        if is_flat_fallback:
            # Om reservpriser (50 öre platt) används: lägg på en svag sinusvåg 
            # så att PuLP fördelar drifttiden jämnt över hela dygnet
            effective_cost = price + math.sin(i * math.pi / 6.0) * 0.5
        else:
            # Beräkna lokalt snittpris för de närmaste 2 timmarna före och efter (±8 kvartar)
            w_start = max(0, i - 8)
            w_end = min(96, i + 9)
            window_prices = prices_96[w_start:w_end]
            local_avg = sum(window_prices) / len(window_prices) if window_prices else price
            
            # Jämför kvartens pris med sitt lokala snitt
            spike_ratio = price / max(1.0, local_avg)
            
            # Om kvarten är en lokal pristopp (> 12% dyrare än omgivande tid) får den ett exponentiellt straff!
            if spike_ratio > 1.12:
                effective_cost = price * (spike_ratio ** 3) * 2.5
            elif price_cap is not None and price > price_cap:
                effective_cost = price * 5.0
            else:
                effective_cost = price
            
        costs.append(x[i] * effective_cost)

    prob += pulp.lpSum(costs)

    # 1. Totalt drifttidskrav över dygnet + balansering mellan dygnshalvor
    required_blocks = max(4, min(96, required_runtime_hours * 4))
    prob += pulp.lpSum([x[i] for i in range(96)]) >= required_blocks
    
    # Förhindra att all värme hamnar på ena dygnshalvan vid kallfront/prisfall
    prob += pulp.lpSum([x[i] for i in range(0, 48)]) >= int(required_blocks * 0.35)
    prob += pulp.lpSum([x[i] for i in range(48, 96)]) >= int(required_blocks * 0.35)
    
    # 2. Max AV-tid
    max_off_slots = max(4, max_consecutive_off_hours * 4)
    for i in range(96 - max_off_slots):
        prob += pulp.lpSum([x[j] for j in range(i, i + max_off_slots + 1)]) >= 1

    # 3. Dubbelt reläskydd (Minst 30 min PÅ i sträck och minst 30 min AV i sträck)
    for i in range(1, 95):
        # PÅ-skydd (-x[i-1] + x[i] - x[i+1] <= 0)
        prob += x[i] <= x[i - 1] + x[i + 1]
        # AV-skydd (x[i-1] - x[i] + x[i+1] <= 1)
        prob += x[i - 1] - x[i] + x[i + 1] <= 1

    # Kör optimeringen
    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    
    result_schedule = [
        bool(x[i].varValue) if x[i].varValue is not None else False 
        for i in range(96)
    ]
    active_blocks = sum(result_schedule)

    print(f"[PULP RESULT] Aktiva block={active_blocks}/96 ({active_blocks/4:.1f}h)")
    print(f"[PULP CHECK] requested={required_runtime_hours}h | actual={active_blocks/4:.1f}h")
    
    off_runs = []
    run = 0
    for state in result_schedule:
        if not state:
            run += 1
        else:
            if run:
                off_runs.append(run)
            run = 0
    if run:
        off_runs.append(run)

    longest_off = max(off_runs) / 4 if off_runs else 0
    print(f"[PULP OFF] longest_off={longest_off:.2f}h")

    return result_schedule

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

        quarter_index = (now.hour * 4) + (now.minute // 15)
       
        if "houses" not in config:
            config = {
                "houses": {
                    "default_house": {
                        "name": "Huvudhuset",
                        "lat": 63.25,
                        "lon": 14.56,
                        "zone": "SE2",
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
                                "price_cap": config.get("price_cap", 50.0),
                                "target_room_temp": 20.0,
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

        for house_key, house_data in houses.items():
            try:
                if house_data.get("paused", False):
                    print(f"[PAUSED] Huset '{house_data.get('name', house_key)}' är pausat. Hoppar över.")
                    continue
                lat = house_data.get("lat", 63.25)
                lon = house_data.get("lon", 14.56)
                station = house_data.get("temperatur_nu_station", "ostersund")
                zone = house_data.get("zone", "SE2")
                
                prices_today = get_nordpool_price(zone=zone, target_date=now)
                if house_data.get("prices_today") != prices_today and len(set(prices_today)) > 1:
                    house_data["prices_today"] = prices_today
                    config_updated = True
                current_price = prices_today[quarter_index] if quarter_index < len(prices_today) else 50.0
                
                current_temp = get_outside_temp(station=station, lat=lat, lon=lon)
                if house_data.get("last_outside_temp") != round(current_temp, 1):
                    house_data["last_outside_temp"] = round(current_temp, 1)
                    config_updated = True

                devices = house_data.get("devices", [])
                if not devices:
                    continue

                ref_device = devices[0]
                price_cap = ref_device.get("price_cap", 50.0)
                house_target_temp = float(ref_device.get("target_room_temp", 20.0))

                active_schedule_date = house_data.get("schedule_date")
                daily_schedule = house_data.get("daily_schedule")
                last_ai_update_str = house_data.get("last_ai_update")
                
                tomorrow_schedule_date = house_data.get("tomorrow_schedule_date")
                tomorrow_schedule = house_data.get("tomorrow_schedule")

                today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
                today_noon = now.replace(hour=12, minute=0, second=0, microsecond=0)

                last_attempt_str = house_data.get("last_ai_attempt")
                in_cooldown = False
                if last_attempt_str:
                    try:
                        last_attempt_dt = datetime.strptime(last_attempt_str, "%Y-%m-%d %H:%M").replace(tzinfo=sweden_tz)
                        if (now - last_attempt_dt).total_seconds() < 3 * 3600:
                            in_cooldown = True
                    except Exception:
                        pass

                needs_ai_update = False
                if not in_cooldown:
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

                # MIDNATTS-RULLNING: Om vi klivit in i ett nytt dygn och redan har morgondagens schema redo
                if active_schedule_date != today_str and tomorrow_schedule_date == today_str:
                    if tomorrow_schedule and len(tomorrow_schedule) == 96:
                        print(f"[MIDNIGHT ROLLOVER] Rullar över förberäknat schema för {today_str} ({house_data.get('name')})")
                        daily_schedule = tomorrow_schedule
                        active_schedule_date = today_str
                        house_data["daily_schedule"] = daily_schedule
                        house_data["schedule_date"] = today_str
                        if house_data.get("tomorrow_ai_reasoning"):
                            house_data["ai_latest_reasoning"] = house_data.get("tomorrow_ai_reasoning")
                        if house_data.get("tomorrow_temp_forecast"):
                            house_data["temp_forecast_today"] = house_data.get("tomorrow_temp_forecast")
                        if house_data.get("prices_tomorrow"):
                            house_data["prices_today"] = house_data.get("prices_tomorrow")
                        house_data["last_ai_update"] = now.strftime("%Y-%m-%d %H:%M")
                        config_updated = True
                        needs_ai_update = False

                # Hämta bara ny 24h-prognos vid omräkning (2 ggr/dag) eller om den saknas i Gist
                temp_forecast = house_data.get("temp_forecast_today")
                if needs_ai_update or not temp_forecast or len(temp_forecast) < 24:
                    temp_forecast = get_hourly_temp_forecast(lat=lat, lon=lon, target_date=now)
                    house_data["temp_forecast_today"] = temp_forecast
                    config_updated = True

                req_runtime = calculate_required_runtime(temp_forecast, current_temp=current_temp)
                max_off_h = 4 if (sum(temp_forecast) / len(temp_forecast) if temp_forecast else current_temp) >= 0 else 2

                if needs_ai_update:
                    print(f"[AI UPDATE] Beräknar om för hus: {house_data.get('name', house_key)}...")
                    try:
                        time.sleep(5 + random.uniform(0, 3))
                        ai_result = get_ai_optimized_parameters(
                            prices_today, 
                            temp_forecast, 
                            current_temp=current_temp,
                            target_temp=house_target_temp,
                            default_price_cap=price_cap, 
                            required_runtime_hours=req_runtime,
                            target_day_label=f"idag ({house_data.get('name')})"
                        )
                        
                        p_cap = ai_result["price_cap"]
                        max_off_h = ai_result["max_consecutive_off_hours"]
                        reasoning = ai_result["motivering"]

                        new_schedule = generate_daily_schedule(
                            prices_today, 
                            temp_hourly=temp_forecast,
                            current_temp=current_temp, 
                            price_cap=p_cap,
                            required_runtime_hours=req_runtime,
                            max_consecutive_off_hours=max_off_h
                        )
                        
                        daily_schedule = new_schedule
                        active_schedule_date = today_str
                        ref_device["price_cap"] = p_cap
                        
                        house_data["ai_latest_reasoning"] = reasoning
                        house_data["last_ai_update"] = now.strftime("%Y-%m-%d %H:%M")
                        house_data["last_ai_attempt"] = now.strftime("%Y-%m-%d %H:%M")
                        config_updated = True

                    except Exception as ai_err:
                        print(f"[WARNING] Cooldown aktiv: AI-omräkning misslyckades ({ai_err}). Använder lokal PuLP-reservplan.")
                        house_data["ai_latest_reasoning"] = f"⚠️ AI-anrop misslyckades ({ai_err}). Använder lokal smart reservplan."
                        
                        fallback_p_cap = max(prices_today) * 0.6 if prices_today else 50.0
                        daily_schedule = generate_daily_schedule(
                            prices_today, 
                            temp_hourly=temp_forecast,
                            current_temp=current_temp, 
                            price_cap=fallback_p_cap,
                            required_runtime_hours=req_runtime,
                            max_consecutive_off_hours=max_off_h
                        )
                        active_schedule_date = today_str
                        house_data["last_ai_attempt"] = now.strftime("%Y-%m-%d %H:%M")
                        config_updated = True

                house_data["schedule_date"] = active_schedule_date
                house_data["daily_schedule"] = daily_schedule

                try:
                    if now.hour >= 14 and tomorrow_schedule_date != tomorrow_str:
                        tomorrow_dt = now + timedelta(days=1)
                        prices_tomorrow = get_nordpool_price(zone=zone, target_date=tomorrow_dt)

                        if len(set(prices_tomorrow)) > 1: 
                            temp_forecast_tom = get_hourly_temp_forecast(lat=lat, lon=lon, target_date=tomorrow_dt)
                            req_runtime_tom = calculate_required_runtime(temp_forecast_tom, current_temp=current_temp)
                            time.sleep(5 + random.uniform(0, 3))
                            ai_result_tom = get_ai_optimized_parameters(
                                prices_tomorrow, 
                                temp_forecast_tom, 
                                current_temp=current_temp,
                                target_temp=house_target_temp,
                                default_price_cap=price_cap, 
                                required_runtime_hours=req_runtime_tom,
                                target_day_label=f"imorgon ({house_data.get('name')})"
                            )
                            
                            p_cap_tom = ai_result_tom["price_cap"]
                            max_off_h_tom = ai_result_tom["max_consecutive_off_hours"]
                            reasoning_tom = ai_result_tom["motivering"]

                            new_tomorrow_sched = generate_daily_schedule(
                                prices_tomorrow, 
                                temp_hourly=temp_forecast_tom,
                                current_temp=current_temp, 
                                price_cap=p_cap_tom,
                                required_runtime_hours=req_runtime_tom,
                                max_consecutive_off_hours=max_off_h_tom
                            )

                            house_data["tomorrow_schedule_date"] = tomorrow_str
                            house_data["tomorrow_schedule"] = new_tomorrow_sched
                            house_data["prices_tomorrow"] = prices_tomorrow
                            house_data["tomorrow_temp_forecast"] = temp_forecast_tom
                            house_data["tomorrow_ai_reasoning"] = reasoning_tom
                            config_updated = True
                except Exception as e:
                    print(f"[ERROR] Misslyckades med morgondagens schema för {house_key}: {e}")

                for dev in devices:
                    dev_id = dev.get("device_id")
                    dev_name = dev.get("name", "Enhet")
                    dev_type = dev.get("type", "switch")
                    dev_price_cap = dev.get("price_cap", price_cap)
                    cold_threshold = dev.get("cold_temp_threshold", 0)
                    min_mins_cold = dev.get("min_minutes_cold", 15)
                    override = dev.get("force_override", "AUTO")

                    if not dev_id:
                        continue
                    dev_schedule = daily_schedule
                    if dev_price_cap != price_cap:
                        dev_schedule = generate_daily_schedule(
                            prices_today,
                            temp_hourly=temp_forecast,
                            current_temp=current_temp,
                            price_cap=dev_price_cap,
                            required_runtime_hours=req_runtime,
                            max_consecutive_off_hours=max_off_h
                        )

                    thermo_id = dev.get("thermometer_device_id")
                    target_room_temp = float(dev.get("target_room_temp", 20.0))
                    measured_room_temp = None
                    thermo_active = False

                    if thermo_id:
                        thermo_active, _, measured_room_temp = get_tuya_device_status(thermo_id, device_type="thermostat")
                        
                        # Säkerhetsvakt mot "frusen" batteritermometer (samma värde > 4 timmar trots värme PÅ)
                        if thermo_active and measured_room_temp is not None and history:
                            dev_hist = [
                                h for h in history 
                                if h.get("house") == house_data.get("name") and h.get("device") == dev_name
                            ][-16:]
                            if len(dev_hist) >= 16:
                                recent_temps = [h.get("room_temp") for h in dev_hist]
                                recent_on = sum(1 for h in dev_hist if h.get("status") == "ON")
                                if all(t == round(measured_room_temp, 1) for t in recent_temps) and recent_on >= 8:
                                    print(f"[THERMO STUCK WARN] Givare för {dev_name} har legat låst på {measured_room_temp}C i >4h trots drift. Ignorerar givare!")
                                    thermo_active = False

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

                        set_tuya_device(dev_id, target_temp_val, device_type="thermostat")
                        time.sleep(2)

                        connected, _, measured_dev_temp = get_tuya_device_status(dev_id, device_type="thermostat", include_battery=False)
                        final_room_temp = measured_dev_temp if measured_dev_temp is not None else measured_room_temp

                        final_status = f"{target_temp_val}°C"
                        log_entry = {
                            "timestamp": now.strftime("%Y-%m-%d %H:%M"),
                            "house": house_data.get('name'),
                            "device": dev_name,
                            "price": round(current_price, 2),
                            "status": final_status,
                            "temp": round(current_temp, 1),
                            "device_temp": round(final_room_temp, 1) if final_room_temp is not None else None,
                            "room_temp": round(final_room_temp, 1) if final_room_temp is not None else None,
                            "reason": reason
                        }
                        history.append(log_entry)

                    else: # Typ: Switch
                        should_be_on = False
                        reason = "Okänd anledning"

                        if override == "PÅ (Tvinga igång)":
                            should_be_on = True
                            reason = f"[{dev_name}] Manuell överstyrning: Tvinga PÅ"
                        elif override == "AV (Stäng av allt)":
                            should_be_on = False
                            reason = f"[{dev_name}] Manuell överstyrning: Stäng AV"
                        else:
                            if dev_schedule and len(dev_schedule) == 96:
                                should_be_on = dev_schedule[quarter_index]
                                reason = f"[{dev_name}] PuLP-schema: Status {'PÅ' if should_be_on else 'AV'} vid {current_price:.1f} öre"
                            else:
                                should_be_on = (current_price <= dev_price_cap)
                                reason = f"[{dev_name}] Reservregel (Inget schema): Pris ({current_price:.1f} öre) vs pristak ({dev_price_cap:.1f} öre)"

                            if thermo_active and measured_room_temp is not None:
                                if measured_room_temp < target_room_temp - 0.5:
                                    should_be_on = True
                                    reason = f"[{dev_name}] Klimatreglering: Rumstemp ({measured_room_temp:.1f}°C) < Mål ({target_room_temp:.1f}°C) -> Tvingar PÅ"
                                elif measured_room_temp >= target_room_temp + 0.2:
                                    should_be_on = False
                                    reason = f"[{dev_name}] Klimatreglering: Måltemp nådd ({measured_room_temp:.1f}°C >= {target_room_temp:.1f}°C) -> Stänger AV"
                            elif thermo_id and not thermo_active:
                                reason += " [⚠️ Termometer offline - kör på enbart tidsschema]"

                            if current_temp <= cold_threshold and min_mins_cold > 0:
                                if now.minute < min_mins_cold:
                                    should_be_on = True
                                    reason = f"[{dev_name}] Köldskydd aktivt ({current_temp}°C <= {cold_threshold}°C)"

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
                            "device_temp": round(measured_room_temp, 1) if measured_room_temp is not None else None,
                            "room_temp": round(measured_room_temp, 1) if measured_room_temp is not None else None,
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