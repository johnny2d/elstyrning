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

    # Utökad historikbuffert (1000 rader) så att 16-kvartarsvakten har full täckning för alla enheter
    trimmed_history = history_data[-1000:] 
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


# Inbyggd direktuppslagning för vanliga orter + cache för automatisk ortsuppslagning
_LOCATION_CACHE = {
    "ostersund": (63.1792, 14.6357),
    "östersund": (63.1792, 14.6357),
    "as": (63.2486, 14.5639),
    "ås": (63.2486, 14.5639),
    "krokom": (63.3260, 14.4485),
    "ronnofors": (63.6500, 13.8667),
    "rönnöfors": (63.6500, 13.8667),
    "offerdal": (63.4667, 14.0167),
    "vejmon": (63.5350, 14.1200),
    "alsen": (63.3833, 13.9333),
    "are": (63.3988, 13.0815),
    "åre": (63.3988, 13.0815),
    "frosön": (63.1930, 14.5220),
    "frösön": (63.1930, 14.5220),
    "smedsasen": (63.3500, 14.2500),
    "smedsåsen": (63.3500, 14.2500),
}

def resolve_location(place_name: str, fallback_lat=63.25, fallback_lon=14.56):
    if not place_name:
        return fallback_lat, fallback_lon
    
    clean_name = str(place_name).strip().lower()
    if clean_name in _LOCATION_CACHE:
        return _LOCATION_CACHE[clean_name]

    # Om du skriver in någon helt ny ort i Sverige slår den upp orten automatiskt 1 gång
    try:
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={requests.utils.quote(clean_name)}&count=1&language=sv&format=json"
        res = requests.get(geo_url, timeout=4)
        if res.status_code == 200:
            results = res.json().get("results", [])
            if results:
                lat_val = round(float(results[0]["latitude"]), 4)
                lon_val = round(float(results[0]["longitude"]), 4)
                _LOCATION_CACHE[clean_name] = (lat_val, lon_val)
                print(f"[GEO] Hittade ort '{place_name}' -> ({lat_val}, {lon_val})")
                return lat_val, lon_val
    except Exception as e:
        print(f"[GEO WARN] Kunde inte slå upp ort '{place_name}': {e}")

    return fallback_lat, fallback_lon

# --- TEMPERATUR PROGNOS (SMHI -> MET NORWAY / YR -> OPEN-METEO) ---
def get_hourly_temp_forecast(lat=63.25, lon=14.56, target_date=None, station=None):
    lat, lon = resolve_location(station, lat, lon)
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
                print(f"[WEATHER] Hämtade 24h prognos från SMHI för '{station or 'standard'}' ({target_str})")
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
                print(f"[WEATHER] Hämtade 24h prognos från MET Norway (Yr) för '{station or 'standard'}' ({target_str})")
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

# --- UTETEMPERATUR NU (SMHI -> MET NORWAY) ---
def get_outside_temp(station="ostersund", lat=63.25, lon=14.56):
    lat, lon = resolve_location(station, lat, lon)
    headers_ua = {"User-Agent": "ElstyrningHomeAutomation/2.0 (kontakt@elstyrning.se)"}

    # 1. Primär källa: SMHI PMP3g för vald ort
    try:
        smhi_url = f"https://opendata-download-metfcst.smhi.se/api/category/pmp3g/version/2/geotype/point/lon/{round(float(lon), 4)}/lat/{round(float(lat), 4)}/data.json"
        res_smhi = requests.get(smhi_url, headers=headers_ua, timeout=5)
        if res_smhi.status_code == 200:
            ts = res_smhi.json().get("timeSeries", [])
            if ts:
                for param in ts[0].get("parameters", []):
                    if param.get("name") == "t":
                        val = float(param["values"][0])
                        print(f"[TEMP] Utetemperatur från SMHI för '{station}': {val}°C")
                        return val
    except Exception as e:
        print(f"[WARNING] SMHI utetemp misslyckades för '{station}': {e}")

    # 2. Sekundär källa: MET Norway (Yr.no) för vald ort
    try:
        yr_url = f"https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={round(float(lat), 4)}&lon={round(float(lon), 4)}"
        res_yr = requests.get(yr_url, headers=headers_ua, timeout=5)
        if res_yr.status_code == 200:
            ts = res_yr.json().get("properties", {}).get("timeseries", [])
            if ts:
                val = float(ts[0]["data"]["instant"]["details"]["air_temperature"])
                print(f"[TEMP] Utetemperatur från MET Norway för '{station}': {val}°C")
                return val
    except Exception as e:
        print(f"[WARNING] MET Norway utetemp misslyckades för '{station}': {e}")

    return 0.0

# --- BERÄKNA DRIFTTID BASERAT PÅ PROGNOS & MÅLTEMPERATUR (DETERMINISTISKT) ---
def calculate_required_runtime(temp_forecast, current_temp=5.0, target_temp=20.0):
    if not temp_forecast or len(temp_forecast) < 24:
        temp_forecast = [current_temp] * 24
        
    avg_temp = sum(temp_forecast) / len(temp_forecast)
    hours_below_zero = sum(1 for t in temp_forecast if t <= 0)
    hours_below_minus5 = sum(1 for t in temp_forecast if t <= -5)
    hours_below_minus15 = sum(1 for t in temp_forecast if t <= -15)
    hours_below_minus20 = sum(1 for t in temp_forecast if t <= -20)

    if hours_below_minus20 >= 8:
        base_hours = 20
    elif hours_below_minus15 >= 12:
        base_hours = 16
    elif hours_below_minus5 >= 8:
        base_hours = 12
    elif hours_below_zero >= 18:
        base_hours = 12
    elif hours_below_zero >= 8:
        base_hours = 10
    elif avg_temp < 5.0:
        base_hours = 8
    else:
        base_hours = 6

    # Skalning för utrymmen med lägre måltemperatur (t.ex. källare/förråd/garage < 16°C)
    # Värmeförlusten är fysikaliskt proportionell mot temperaturskillnaden (T_inne - T_ute)
    if target_temp is not None and float(target_temp) < 16.0:
        delta_actual = max(2.0, float(target_temp) - avg_temp)
        delta_ref = max(5.0, 20.0 - avg_temp)
        scale_factor = max(0.35, min(1.0, delta_actual / delta_ref))
        scaled_hours = int(round(base_hours * scale_factor))
        return max(3, min(20, scaled_hours))

    return base_hours

# --- AI STRATEGEN (GEMINI) ---
def get_ai_optimized_parameters(prices_96, temp_hourly, current_temp=5.0, target_temp=20.0, default_price_cap=50.0, target_day_label="idag", required_runtime_hours=None, **kwargs):
    min_price = min(prices_96) if prices_96 else 50.0
    max_price = max(prices_96) if prices_96 else 100.0
    avg_price = sum(prices_96) / len(prices_96) if prices_96 else 75.0
    
    sorted_prices = sorted(prices_96) if prices_96 else [50.0]
    local_smart_price_cap = sorted_prices[len(sorted_prices) // 2]
    avg_temp = sum(temp_hourly) / len(temp_hourly) if temp_hourly else current_temp

    if required_runtime_hours is None:
        required_runtime_hours = calculate_required_runtime(temp_hourly, current_temp=current_temp, target_temp=target_temp)

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

# --- HJÄLPFUNKTION FÖR KANALVAL PÅ DUBBELUTTAG (T.EX. DELTACO SH-P02E) ---
def _parse_device_channel(device_id: str, switch_channel: str = "auto"):
    """
    Stöder både val av uttag i gränssnittet ('auto', 'both', 'switch_1', 'switch_2')
    och suffix i Device ID (t.ex. 'bfcf180...#1' eller 'bfcf180...#2').
    """
    clean_id = str(device_id or "").strip()
    ch = str(switch_channel or "auto").strip().lower()
    if "#" in clean_id:
        parts = clean_id.split("#", 1)
        clean_id = parts[0].strip()
        suffix = parts[1].strip().lower()
        if suffix in ["1", "switch_1"]:
            ch = "switch_1"
        elif suffix in ["2", "switch_2"]:
            ch = "switch_2"
        elif suffix in ["both", "all", "1+2"]:
            ch = "both"
    return clean_id, ch

# --- TUYA CLOUD STYRNING & STATUS ---
def set_tuya_device(device_id: str, value, device_type: str = "switch", switch_channel: str = "auto") -> bool:
    clean_id, ch = _parse_device_channel(device_id, switch_channel)
    if not all([TUYA_API_KEY, TUYA_API_SECRET, clean_id]):
        print(f"[TUYA ERROR] Saknar API-nycklar eller Device ID för enhet {clean_id}.")
        return False
    
    for attempt in range(2):
        try:
            cloud = tinytuya.Cloud(
                apiRegion=TUYA_REGION,
                apiKey=TUYA_API_KEY,
                apiSecret=TUYA_API_SECRET,
                devId=clean_id
            )
            
            # Kontrollera först om enheten faktiskt har WiFi-kontakt i Tuya Cloud
            try:
                dev_info = cloud.cloudrequest(f"/v1.0/devices/{clean_id}")
                if isinstance(dev_info, dict) and dev_info.get("success") and isinstance(dev_info.get("result"), dict):
                    if not dev_info["result"].get("online", True):
                        print(f"[TUYA CMD OFFLINE] Enhet {clean_id} är OFFLINE i Tuya Cloud - kommandot kan inte levereras!")
                        return False
            except Exception as e_info:
                print(f"[TUYA INFO WARN] Kunde inte verifiera online-status före kommando ({clean_id}): {e_info}")

            if device_type == "thermostat":
                code_names = ["temp_set", "target_temp", "set_temp"]
                cmd_value = int(value * 10) if isinstance(value, float) and value < 50 else value
                for code_name in code_names:
                    commands = {"commands": [{"code": code_name, "value": cmd_value}]}
                    response = cloud.sendcommand(clean_id, commands)
                    print(f"[TUYA CMD ({clean_id} / {code_name})]: value={cmd_value}, response={response}")
                    if response and (response.get("success") is True or response.get("result") is True):
                        return True
            else:
                cmd_value = bool(value)
                # Om användaren valt ett specifikt uttag (t.ex. Uttag 1 eller Uttag 2 på Deltaco SH-P02E)
                if ch in ["switch_1", "switch_2"]:
                    commands = {"commands": [{"code": ch, "value": cmd_value}]}
                    response = cloud.sendcommand(clean_id, commands)
                    print(f"[TUYA CMD ({clean_id} / {ch})]: value={cmd_value}, response={response}")
                    if response and (response.get("success") is True or response.get("result") is True):
                        return True
                elif ch == "both":
                    # Styr BÅDA uttagen samtidigt i ett gemensamt anrop (med fallback till separata anrop)
                    commands = {"commands": [{"code": "switch_1", "value": cmd_value}, {"code": "switch_2", "value": cmd_value}]}
                    response = cloud.sendcommand(clean_id, commands)
                    print(f"[TUYA CMD ({clean_id} / switch_1+switch_2)]: value={cmd_value}, response={response}")
                    if response and (response.get("success") is True or response.get("result") is True):
                        return True
                    ok1 = cloud.sendcommand(clean_id, {"commands": [{"code": "switch_1", "value": cmd_value}]})
                    ok2 = cloud.sendcommand(clean_id, {"commands": [{"code": "switch_2", "value": cmd_value}]})
                    if (ok1 and ok1.get("success")) or (ok2 and ok2.get("success")):
                        return True
                else:
                    # 'auto': Testa först om enheten har både switch_1 och switch_2 (som Deltaco SH-P02E)
                    # genom att försöka skicka batch-kommando till switch_1 + switch_2, annars vanlig switch -> switch_1
                    both_cmd = {"commands": [{"code": "switch_1", "value": cmd_value}, {"code": "switch_2", "value": cmd_value}]}
                    res_both = cloud.sendcommand(clean_id, both_cmd)
                    if res_both and (res_both.get("success") is True or res_both.get("result") is True):
                        print(f"[TUYA CMD ({clean_id} / AUTO switch_1+switch_2)]: value={cmd_value}, response={res_both}")
                        return True

                    for code_name in ["switch", "switch_1", "switch_heat"]:
                        commands = {"commands": [{"code": code_name, "value": cmd_value}]}
                        response = cloud.sendcommand(clean_id, commands)
                        print(f"[TUYA CMD ({clean_id} / {code_name})]: value={cmd_value}, response={response}")
                        if response and (response.get("success") is True or response.get("result") is True):
                            return True
        except Exception as e:
            print(f"[WARNING] Försök {attempt+1} - [TUYA ERROR] Kunde inte skicka kommando till {clean_id}: {e}")
    print(f"[ERROR] Tuya kommando misslyckades för enhet {clean_id} efter flera försök.")
    return False

def get_tuya_device_status(device_id: str, device_type: str = "switch", include_battery: bool = False, include_power: bool = False, switch_channel: str = "auto"):
    clean_id, ch = _parse_device_channel(device_id, switch_channel)
    if not all([TUYA_API_KEY, TUYA_API_SECRET, clean_id]):
        default_state = "OFF" if device_type == "switch" else "N/A"
        if include_battery and include_power:
            return False, default_state, None, None, None
        if include_battery:
            return False, default_state, None, None
        return False, default_state, None

    print(f"[TUYA STATUS] device_id={clean_id} (channel={ch}) type={device_type}")
    for attempt in range(2):
        try:
            cloud = tinytuya.Cloud(
                apiRegion=TUYA_REGION,
                apiKey=TUYA_API_KEY,
                apiSecret=TUYA_API_SECRET,
                devId=clean_id
            )
            
            # 1. Kontrollera om enheten faktiskt är ONLINE i Tuya Cloud (inte bara cachad status)
            try:
                dev_info = cloud.cloudrequest(f"/v1.0/devices/{clean_id}")
                if isinstance(dev_info, dict) and dev_info.get("success") and isinstance(dev_info.get("result"), dict):
                    is_online = dev_info["result"].get("online", True)
                    if not is_online:
                        print(f"[TUYA OFFLINE] Enhet {clean_id} rapporterar online=False i Tuya Cloud!")
                        if include_battery and include_power:
                            return False, "OFFLINE", None, None, None
                        if include_battery:
                            return False, "OFFLINE", None, None
                        return False, "OFFLINE", None
            except Exception as e_info:
                print(f"[TUYA INFO WARN] Kunde inte verifiera online-flagga för {clean_id}: {e_info}")

            # 2. Hämta datapunkter (temperatur, switch-läge, batteri, effektmätning)
            status_data = cloud.getstatus(clean_id)
            print(f"[TUYA RAW RESPONSE] {clean_id}: {status_data}")

            if status_data and status_data.get("success"):
                result = status_data.get("result", [])
                current_state = "OFF" if device_type == "switch" else "N/A"
                measured_temp = None
                battery_level = None
                power_w = None

                dp_map = {}
                if isinstance(result, list):
                    for item in result:
                        c_name = str(item.get("code", "")).lower()
                        c_val = item.get("value")
                        dp_map[c_name] = c_val
                        print(f"[TUYA DP] code={item.get('code')} value={c_val}")

                    # Batterinivå
                    for b_code in ["va_battery", "battery_percentage", "battery_value", "battery", "residual_electricity"]:
                        if b_code in dp_map and dp_map[b_code] is not None:
                            try:
                                battery_level = int(float(dp_map[b_code]))
                            except Exception:
                                battery_level = str(dp_map[b_code])
                            break
                    if battery_level is None and "battery_state" in dp_map:
                        state_map = {"high": "Hög (100%)", "middle": "Medel (50%)", "low": "Låg ⚠️ (10%)"}
                        battery_level = state_map.get(str(dp_map["battery_state"]).lower(), str(dp_map["battery_state"]))

                    # Effektmätning (t.ex. Deltaco SH-P02E: cur_power i 0.1W, cur_voltage i 0.1V, cur_current i mA)
                    if "cur_power" in dp_map and dp_map["cur_power"] is not None:
                        try:
                            raw_p = float(dp_map["cur_power"])
                            # Om både spänning och ström rapporteras kan vi verifiera skalan (0.1W vs 1W) exakt
                            raw_v = float(dp_map.get("cur_voltage", 0) or 0)
                            raw_i = float(dp_map.get("cur_current", 0) or 0)
                            v_volts = raw_v / 10.0 if raw_v > 500 else raw_v
                            i_amps = raw_i / 1000.0 if raw_i > 0 else 0.0
                            est_w = v_volts * i_amps
                            if est_w > 5.0:
                                power_w = round(raw_p / 10.0, 1) if abs((raw_p / 10.0) - est_w) < abs(raw_p - est_w) else round(raw_p, 1)
                            else:
                                # Tuya-standard för SH-P02E och smartpluggar med cur_power är 0.1W (deciwatt)
                                power_w = round(raw_p / 10.0, 1)
                        except Exception:
                            pass
                    elif "power" in dp_map or "active_power" in dp_map:
                        try:
                            power_w = round(float(dp_map.get("power") or dp_map.get("active_power")), 1)
                        except Exception:
                            pass

                    # Temperatur / Termostat eller Switch-status
                    if device_type == "thermostat":
                        for t_code in ["va_temperature", "temp_current", "temperature", "room_temp", "sensor_temp"]:
                            if t_code in dp_map and dp_map[t_code] is not None:
                                try:
                                    v = float(dp_map[t_code])
                                    measured_temp = v / 10.0 if v > 50 else v
                                except Exception:
                                    pass
                                break
                        for s_code in dp_map:
                            if "temp_set" in s_code or "target" in s_code:
                                try:
                                    v = float(dp_map[s_code])
                                    current_state = f"{v / 10.0 if v > 50 else v:.1f}°C"
                                except Exception:
                                    current_state = str(dp_map[s_code])
                                break
                    else:
                        # Hantera enkel- och dubbeluttag (switch, switch_1, switch_2)
                        if ch == "switch_1" and "switch_1" in dp_map:
                            current_state = "ON" if dp_map["switch_1"] else "OFF"
                        elif ch == "switch_2" and "switch_2" in dp_map:
                            current_state = "ON" if dp_map["switch_2"] else "OFF"
                        elif "switch_1" in dp_map and "switch_2" in dp_map:
                            s1 = bool(dp_map["switch_1"])
                            s2 = bool(dp_map["switch_2"])
                            if ch == "both":
                                current_state = "ON" if (s1 or s2) else "OFF"
                            else:
                                current_state = "ON" if (s1 or s2) else "OFF"
                        elif "switch" in dp_map:
                            current_state = "ON" if dp_map["switch"] else "OFF"
                        elif "switch_1" in dp_map:
                            current_state = "ON" if dp_map["switch_1"] else "OFF"
                        else:
                            for k_code, k_val in dp_map.items():
                                if "switch" in k_code:
                                    current_state = "ON" if k_val else "OFF"
                                    break

                if include_battery and include_power:
                    return True, current_state, measured_temp, battery_level, power_w
                if include_battery:
                    return True, current_state, measured_temp, battery_level
                return True, current_state, measured_temp

        except Exception as e:
            print(f"[WARNING] Försök {attempt+1} - [TUYA STATUS ERROR ({clean_id})]: {e}")
    
    default_state = "OFF" if device_type == "switch" else "N/A"
    if include_battery and include_power:
        return False, default_state, None, None, None
    if include_battery:
        return False, default_state, None, None
    return False, default_state, None

# --- PULP OPTIMERINGSMOTOR MED LOKAL TOPPKAPNING ---
def generate_daily_schedule(
    prices_96, 
    temp_hourly=None,
    current_temp=15.0, 
    cold_threshold=5.0, 
    min_minutes_cold=15, 
    price_cap=50.0,
    required_runtime_hours=8,
    max_consecutive_off_hours=6,
    target_temp=20.0
):

    print(
        f"[PULP START] "
        f"price_cap={price_cap} | "
        f"runtime={required_runtime_hours}h | "
        f"max_off={max_consecutive_off_hours}h | "
        f"mål={target_temp:.1f}C | "
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

    # För utrymmen med låg måltemperatur (t.ex. källare < 16°C) tillåts något längre sammanhängande
    # avstängning vid måttlig kyla så att PuLP kan välja exakt de billigaste timmarna
    if target_temp is not None and float(target_temp) < 16.0 and hours_below_minus15 < 12:
        max_consecutive_off_hours = max(max_consecutive_off_hours, 5)

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
                    temp_forecast = get_hourly_temp_forecast(lat=lat, lon=lon, target_date=now, station=station)
                    house_data["temp_forecast_today"] = temp_forecast
                    config_updated = True

                req_runtime = calculate_required_runtime(temp_forecast, current_temp=current_temp, target_temp=house_target_temp)
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
                            max_consecutive_off_hours=max_off_h,
                            target_temp=house_target_temp
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
                            max_consecutive_off_hours=max_off_h,
                            target_temp=house_target_temp
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
                            temp_forecast_tom = get_hourly_temp_forecast(lat=lat, lon=lon, target_date=tomorrow_dt, station=station)
                            req_runtime_tom = calculate_required_runtime(temp_forecast_tom, current_temp=current_temp, target_temp=house_target_temp)
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
                                max_consecutive_off_hours=max_off_h_tom,
                                target_temp=house_target_temp
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
                    dev_channel = dev.get("switch_channel", "auto")
                    dev_price_cap = dev.get("price_cap", price_cap)
                    cold_threshold = dev.get("cold_temp_threshold", 0)
                    min_mins_cold = dev.get("min_minutes_cold", 15)
                    override = dev.get("force_override", "AUTO")

                    if not dev_id:
                        continue

                    thermo_id = dev.get("thermometer_device_id")
                    target_room_temp = float(dev.get("target_room_temp", 20.0))
                    measured_room_temp = None
                    thermo_active = False

                    # Beräkna enhetens eget drifttidsbehov utifrån dess måltemperatur (t.ex. 8°C i källare vs 20°C i hus)
                    dev_req_runtime = calculate_required_runtime(temp_forecast, current_temp=current_temp, target_temp=target_room_temp)
                    dev_schedule = daily_schedule
                    if dev_price_cap != price_cap or dev_req_runtime != req_runtime:
                        dev_schedule = generate_daily_schedule(
                            prices_today,
                            temp_hourly=temp_forecast,
                            current_temp=current_temp,
                            price_cap=dev_price_cap,
                            required_runtime_hours=dev_req_runtime,
                            max_consecutive_off_hours=max_off_h,
                            target_temp=target_room_temp
                        )

                    dev_hist = [
                        h for h in history 
                        if h.get("house") == house_data.get("name") and h.get("device") == dev_name
                    ]

                    if thermo_id:
                        thermo_active, _, measured_room_temp = get_tuya_device_status(thermo_id, device_type="thermostat")
                        
                        # Uppdaterad säkerhetsvakt: Fångar BÅDE givare som låst sig lågt (tvingar PÅ) 
                        # OCH givare som låst sig högt (tvingar AV trots kyla ute) i > 4 timmar (16 kvartar)
                        if thermo_active and measured_room_temp is not None and len(dev_hist) >= 16:
                            recent_16 = dev_hist[-16:]
                            recent_temps = [h.get("room_temp") for h in recent_16]
                            recent_on = sum(1 for h in recent_16 if h.get("status") == "ON")
                            if all(t == round(measured_room_temp, 1) for t in recent_temps) and (recent_on >= 4 or abs(current_temp - measured_room_temp) >= 5.0):
                                print(f"[THERMO STUCK WARN] Givare för {dev_name} har legat helt låst på {measured_room_temp}C i >4h. Ignorerar givare och kör på PuLP-schema!")
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
                                # Köldskydd endast om giltigt PuLP-schema saknas
                                if current_temp <= cold_threshold and min_mins_cold > 0 and now.minute < min_mins_cold:
                                    target_temp_val = t_high
                                    reason = f"[{dev_name}] Köldskydd aktivt (Reservläge) -> HÖG temp ({t_high}°C)"

                        cmd_ok = set_tuya_device(dev_id, target_temp_val, device_type="thermostat")
                        time.sleep(2)

                        connected, _, measured_dev_temp = get_tuya_device_status(dev_id, device_type="thermostat", include_battery=False)
                        final_room_temp = measured_dev_temp if measured_dev_temp is not None else measured_room_temp

                        if not cmd_ok or not connected:
                            final_status = "OFFLINE"
                            reason += f" [❌ Termostat OFFLINE - målvärde ({target_temp_val}°C) nådde inte fram!]"
                        else:
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
                    
                    elif dev_type == "sensor":
                        # Fristående temperaturgivare (ingen styrning, enbart mätning & loggning)
                        sensor_conn, _, sensor_temp = get_tuya_device_status(dev_id, device_type="thermostat")

                        if not sensor_conn:
                            final_status = "OFFLINE"
                            reason = f"[{dev_name}] Fristående termometer OFFLINE ⚠️"
                        elif sensor_temp is not None:
                            final_status = f"{sensor_temp:.1f}°C"
                            reason = f"[{dev_name}] Temperaturmätning: {sensor_temp:.1f}°C"
                        else:
                            final_status = "N/A"
                            reason = f"[{dev_name}] Ansluten men inget temperaturvärde hittades ⚠️"

                        log_entry = {
                            "timestamp": now.strftime("%Y-%m-%d %H:%M"),
                            "house": house_data.get('name'),
                            "device": dev_name,
                            "price": round(current_price, 2),
                            "status": final_status,
                            "temp": round(current_temp, 1),
                            "device_temp": round(sensor_temp, 1) if sensor_temp is not None else None,
                            "room_temp": round(sensor_temp, 1) if sensor_temp is not None else None,
                            "reason": reason
                        }
                        history.append(log_entry)

                    else: # Typ: Switch (inkl. dubbeluttag med effektmätning som Deltaco SH-P02E)
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
                                # Anti-kortcykling: Om förra kvarten tvingades PÅ pga undertemp, värm upp till minst target - 0.2°C
                                prev_under_temp = (len(dev_hist) > 0 and " < Mål (" in str(dev_hist[-1].get("reason", "")))
                                if measured_room_temp < target_room_temp - 0.5 or (prev_under_temp and measured_room_temp < target_room_temp - 0.2):
                                    should_be_on = True
                                    reason = f"[{dev_name}] Klimatreglering: Rumstemp ({measured_room_temp:.1f}°C) < Mål ({target_room_temp:.1f}°C) -> Tvingar PÅ"
                                elif measured_room_temp >= target_room_temp + 0.2:
                                    should_be_on = False
                                    reason = f"[{dev_name}] Klimatreglering: Måltemp nådd ({measured_room_temp:.1f}°C >= {target_room_temp:.1f}°C) -> Stänger AV"
                                else:
                                    reason += f" (Rumstemp OK: {measured_room_temp:.1f}°C, Mål: {target_room_temp:.1f}°C)"
                            elif thermo_id and not thermo_active:
                                reason += " [⚠️ Termometer offline/låst - kör på PuLP-schema]"

                            # Köldskydd går ENDAST in som reserv om både aktiv termometer OCH giltigt PuLP-schema saknas
                            if (not thermo_active or measured_room_temp is None) and (not dev_schedule or len(dev_schedule) != 96):
                                if current_temp <= cold_threshold and min_mins_cold > 0 and now.minute < min_mins_cold:
                                    should_be_on = True
                                    reason = f"[{dev_name}] Köldskydd aktivt (Reservläge utan schema, {current_temp}°C <= {cold_threshold}°C)"

                        cmd_ok = set_tuya_device(dev_id, should_be_on, device_type="switch", switch_channel=dev_channel)
                        time.sleep(2)

                        # Läs även av aktuell effekt (W) om switchen har effektmätning (t.ex. Deltaco SH-P02E)
                        _, _, _, _, measured_power_w = get_tuya_device_status(
                            dev_id,
                            device_type="switch",
                            include_battery=True,
                            include_power=True,
                            switch_channel=dev_channel
                        )

                        intended_str = "PÅ" if should_be_on else "AV"
                        if not cmd_ok:
                            final_status = "OFFLINE"
                            reason += f" [❌ Switch OFFLINE - kommandot ({intended_str}) nådde inte fram!]"
                        else:
                            final_status = "ON" if should_be_on else "OFF"
                            if measured_power_w is not None:
                                reason += f" [⚡ {measured_power_w:.1f} W]"

                        log_entry = {
                            "timestamp": now.strftime("%Y-%m-%d %H:%M"),
                            "house": house_data.get('name'),
                            "device": dev_name,
                            "price": round(current_price, 2),
                            "status": final_status,
                            "temp": round(current_temp, 1),
                            "device_temp": round(measured_room_temp, 1) if measured_room_temp is not None else None,
                            "room_temp": round(measured_room_temp, 1) if measured_room_temp is not None else None,
                            "power_w": measured_power_w,
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