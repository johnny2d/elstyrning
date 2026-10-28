import streamlit as st
import os
import requests
from zoneinfo import ZoneInfo
sweden_tz = ZoneInfo("Europe/Stockholm")
import json
from datetime import datetime, timedelta
import time
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# Secret environment injection
if hasattr(st, "secrets"):
    if "GEMINI_API_KEY" in st.secrets:
        os.environ["GEMINI_API_KEY"] = st.secrets["GEMINI_API_KEY"]
    if "GIST_ID" in st.secrets:
        os.environ["GIST_ID"] = st.secrets["GIST_ID"]
    if "GIST_TOKEN" in st.secrets:
        os.environ["GIST_TOKEN"] = st.secrets["GIST_TOKEN"]
    if "TUYA_API_KEY" in st.secrets:
        os.environ["TUYA_API_KEY"] = st.secrets["TUYA_API_KEY"]
    if "TUYA_API_SECRET" in st.secrets:
        os.environ["TUYA_API_SECRET"] = st.secrets["TUYA_API_SECRET"]

from main import (
    get_tuya_device_status,
    generate_daily_schedule, 
    get_nordpool_price, 
    get_outside_temp, 
    get_hourly_temp_forecast,
    get_ai_optimized_parameters
)

st.set_page_config(page_title="Elstyrning Control Panel", page_icon="⚡", layout="wide")

GIST_ID = st.secrets.get("GIST_ID")
GITHUB_TOKEN = st.secrets.get("GIST_TOKEN")

headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json"
}

@st.cache_data(ttl=15)
def load_gist_data():
    try:
        res = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=headers)
        if res.status_code == 200:
            files = res.json()["files"]
            config = json.loads(files["config.json"]["content"]) if "config.json" in files else {}
            history = json.loads(files["history.json"]["content"]) if "history.json" in files else []
            return config, history
    except Exception as e:
        st.error(f"Kunde inte ladda data från Gist: {e}")
    
    return {"houses": {}, "admin_password": "admin"}, []

def save_config(config_data):
    payload = {
        "files": {
            "config.json": {
                "content": json.dumps(config_data, indent=2)
            }
        }
    }
    res = requests.patch(f"https://api.github.com/gists/{GIST_ID}", headers=headers, json=payload)
    if res.status_code == 200:
        st.success("Inställningarna sparades!")
    else:
        st.error(f"Ett fel uppstod vid sparande: {res.status_code}")

config, history = load_gist_data()

# Säkerställ grundläggande nycklar i config
if "houses" not in config:
    config["houses"] = {}
if "admin_password" not in config:
    config["admin_password"] = st.secrets.get("APP_PASSWORD", "admin123")

# --- AUTHENTICATION & SESSION MANAGEMENT ---
if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False
    st.session_state["user_role"] = None  # 'admin' eller 'house'
    st.session_state["assigned_house_key"] = None

def login_user(username_input, password_input):
    username_clean = username_input.strip()
    
    # 1. Kontrollera om det är Admin-inloggning
    if username_clean.lower() == "admin":
        if password_input == config.get("admin_password", "admin123"):
            st.session_state["authenticated"] = True
            st.session_state["user_role"] = "admin"
            st.session_state["assigned_house_key"] = None
            return True, "Välkommen Admin!"
        return False, "Fel lösenord för Admin."

    # 2. Kontrollera om det är inloggning för ett specifikt hus
    houses = config.get("houses", {})
    for h_key, h_data in houses.items():
        h_name = h_data.get("name", h_key)
        # Matcha antingen på visningsnamn eller hus-ID
        if username_clean.lower() in [h_name.lower(), h_key.lower()]:
            h_password = h_data.get("password", "hus123")
            if password_input == h_password:
                st.session_state["authenticated"] = True
                st.session_state["user_role"] = "house"
                st.session_state["assigned_house_key"] = h_key
                return True, f"Välkommen till {h_name}!"
            return False, f"Fel lösenord för {h_name}."

    return False, "Användarnamnet (huset) hittades inte."

# --- INLOGGNINGSVY ---
if not st.session_state["authenticated"]:
    st.markdown("<br><br>", unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.title("🔒 Logga in på Elstyrningen")
        st.caption("Ange husnamn (t.ex. 'Stugan' eller 'Huvudhuset (Ås)') eller 'admin' för systemadministration.")
        
        with st.form("login_form"):
            username_input = st.text_input("Användarnamn (Husnamn eller 'admin')")
            password_input = st.text_input("Lösenord", type="password")
            submit_btn = st.form_submit_button("Logga in", use_container_width=True, type="primary")
            
            if submit_btn:
                success, msg = login_user(username_input, password_input)
                if success:
                    st.success(msg)
                    time.sleep(0.5)
                    st.rerun()
                else:
                    st.error(msg)
    st.stop()

# --- SYSTEM HUVUDVY (INLOGGAD) ---
is_admin = st.session_state["user_role"] == "admin"
houses = config.get("houses", {})

# Sidofält & Utloggning
with st.sidebar:
    st.title("⚡ Elstyrning")
    if is_admin:
        st.subheader("👤 Inloggad som: **System Admin**")
        if houses:
            house_keys = list(houses.keys())
            selected_house_key = st.selectbox("Aktivt Hus (Admin-vy)", house_keys, format_func=lambda k: houses[k].get("name", k))
            current_house = houses[selected_house_key]
        else:
            st.warning("Inga hus registrerade.")
            current_house = None
    else:
        assigned_key = st.session_state["assigned_house_key"]
        current_house = houses.get(assigned_key)
        if current_house:
            st.subheader(f"🏠 **{current_house.get('name')}**")
        else:
            st.error("Husdata saknas.")
            st.stop()

    # Enhetsväljare i sidofältet
    current_device = None
    if current_house:
        devices = current_house.get("devices", [])
        if devices:
            device_names = [d.get("name", d.get("id")) for d in devices]
            selected_device_idx = st.selectbox("Välj Enhet", range(len(devices)), format_func=lambda i: device_names[i])
            current_device = devices[selected_device_idx]
        else:
            st.info("Inga enheter inlagda för detta hus.")

    st.divider()
    
    # Admin Forcering
    if is_admin:
        st.write("### ⚡ Admin-verktyg")
        if st.button("🚀 Forcera AI-omräkning (Alla hus)", type="primary"):
            with st.spinner("Beräknar om scheman för alla hus med paus mellan anrop..."):
                try:
                    now = datetime.now(sweden_tz)
                    today_str = now.strftime("%Y-%m-%d")
                    tomorrow_dt = now + timedelta(days=1)
                    tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")

                    for h_key, h_data in houses.items():
                        time.sleep(12) # Fördröjning för Gemini Rate Limit
                        h_lat = h_data.get("lat", 63.25)
                        h_lon = h_data.get("lon", 14.56)
                        h_zone = h_data.get("zone", "SE2")
                        h_station = h_data.get("temperatur_nu_station")

                        current_temp = get_outside_temp(station=h_station, lat=h_lat, lon=h_lon)
                        prices_today = get_nordpool_price(zone=h_zone, target_date=now)
                        temp_forecast = get_hourly_temp_forecast(lat=h_lat, lon=h_lon, target_date=now)

                        devs = h_data.get("devices", [])
                        ref_dev = devs[0] if devs else {}
                        def_cap = ref_dev.get("price_cap", 50.0)
                        def_min_on = ref_dev.get("min_on_blocks", 44)
                        max_off = ref_dev.get("max_off_blocks", 12)

                        p_cap_today, m_blocks_today, reasoning_today = get_ai_optimized_parameters(
                            prices_today, temp_forecast, default_price_cap=def_cap, default_min_on_blocks=def_min_on, target_day_label=f"idag ({h_data.get('name')})"
                        )
                        daily_sched = generate_daily_schedule(
                            prices_today, max_off_blocks=max_off, min_on_blocks=m_blocks_today, current_temp=current_temp, price_cap=p_cap_today
                        )

                        h_data["schedule_date"] = today_str
                        h_data["daily_schedule"] = daily_sched
                        h_data["ai_latest_reasoning"] = reasoning_today
                        h_data["last_ai_update"] = now.strftime("%Y-%m-%d %H:%M")

                    save_config(config)
                    st.cache_data.clear()
                    st.success("AI-omräkning klar!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Ett fel uppstod: {e}")

    if st.button("🚪 Logga ut"):
        st.session_state["authenticated"] = False
        st.session_state["user_role"] = None
        st.session_state["assigned_house_key"] = None
        st.rerun()

# --- HUVUDLAYOUT & FLIKAR ---
st.title(f"⚡ {current_house.get('name') if current_house else 'Elstyrning'}")

if current_house and current_house.get("ai_latest_reasoning"):
    st.info(f"🤖 **AI-Strategens analys:** {current_house.get('ai_latest_reasoning')}")

# Definiera flikar beroende på roll
if is_admin:
    tab_dash, tab_hist, tab_settings, tab_sched, tab_admin = st.tabs([
        "🏠 Dashboard", "📊 Övervakning & Graf", "⚙️ Enhetsinställningar", "📅 Schema & Priser", "🛠️ System Admin"
    ])
else:
    tab_dash, tab_hist, tab_settings, tab_sched = st.tabs([
        "🏠 Dashboard", "📊 Övervakning & Graf", "⚙️ Enhetsinställningar", "📅 Schema & Priser"
    ])
    tab_admin = None

# --- TAB 1: DASHBOARD & REALTIDSDATA ---
with tab_dash:
    st.subheader("📍 Realtidsöversikt & Ekonomi")
    
    outside_temp = get_outside_temp(
        station=current_house.get("temperatur_nu_station"), 
        lat=current_house.get("lat", 63.25), 
        lon=current_house.get("lon", 14.56)
    )
    
    # Beräkna veckans besparing baserat på historik för detta hus
    week_savings_kr = 0.0
    if history:
        df_h = pd.DataFrame(history)
        if 'house' in df_h.columns and current_house.get('name') in df_h['house'].values:
            df_h = df_h[df_h['house'] == current_house.get('name')]
            if 'timestamp' in df_h.columns:
                df_h['dt'] = pd.to_datetime(df_h['timestamp'])
                week_ago = datetime.now() - timedelta(days=7)
                df_week = df_h[df_h['dt'] >= week_ago]
                if not df_week.empty and 'price' in df_week.columns:
                    avg_price = df_week['price'].mean()
                    active_mask = df_week['status'].astype(str).str.upper().isin(['ON', 'TRUE']) | (df_week['status'].astype(str).str.contains('°C'))
                    active_prices = df_week.loc[active_mask, 'price']
                    if not active_prices.empty:
                        saved_ore_per_kwh = avg_price - active_prices.mean()
                        # Anta schablonförbrukning på ca 2 kW per aktiv timme
                        estimated_kwh = len(active_prices) * 0.5 * 2.0
                        week_savings_kr = max(0.0, (saved_ore_per_kwh * estimated_kwh) / 100.0)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Utetemperatur", f"{outside_temp:.1f} °C")
    col2.metric("Elområde", current_house.get("zone", "SE2"))
    col3.metric("Antal enheter", len(current_house.get("devices", [])))
    col4.metric("Estimerad besparing (7 dgr)", f"{week_savings_kr:.2f} kr")

    st.divider()
    st.write("### 🔌 Husets Enheter")
    
    devs = current_house.get("devices", [])
    if devs:
        dev_cols = st.columns(len(devs) if len(devs) <= 3 else 3)
        for idx, dev in enumerate(devs):
            with dev_cols[idx % 3]:
                st.markdown(f"#### {dev.get('name')}")
                st.caption(f"Typ: {'Termostat' if dev.get('type') == 'thermostat' else 'Relä/Switch'}")
                
                tuya_conn, tuya_msg, dev_temp = get_tuya_device_status(
                    dev.get("device_id"), 
                    device_type=dev.get("type", "switch")
                ) if dev.get("device_id") else (False, "Ej konfigurerad", None)
                
                st.write(f"**Status:** {tuya_msg}")
                if dev_temp is not None:
                    st.write(f"**Givartemperatur:** {dev_temp:.1f} °C")
                st.write(f"**Driftläge:** {dev.get('force_override', 'AUTO')}")
                st.divider()
    else:
        st.info("Inga enheter registrerade för detta hus ännu.")

# --- TAB 2: HISTORIK & GRAFER ---

with tab_hist:
    if not current_device:
        st.info("Välj en enhet i sidofältet för att visa historik.")
    else:
        is_thermostat = current_device.get("type") == "thermostat"
        st.subheader(f"Övervakning för {current_device.get('name')} ({'Termostat' if is_thermostat else 'Switch'})")
        
        if history:
            df = pd.DataFrame(history)
            if 'house' in df.columns and 'device' in df.columns:
                df = df[(df['house'] == current_house.get('name')) & (df['device'] == current_device.get('name'))]
            
            if not df.empty:
                df['time'] = pd.to_datetime(df['timestamp'])
                
                if is_thermostat:
                    def parse_status_val(val):
                        if pd.isna(val): return float(current_device.get("target_temp_low", 6.0))
                        val_str = str(val).replace('°C', '').strip()
                        try: return float(val_str)
                        except ValueError:
                            return float(current_device.get("target_temp_high", 10.0)) if val_str.upper() == 'ON' else float(current_device.get("target_temp_low", 6.0))
                    df['status_val'] = df['status'].apply(parse_status_val)
                else:
                    df['status_num'] = df['status'].apply(lambda x: 1 if str(x).upper() == 'ON' else 0)

                latest = df.iloc[-1]
                hist_snitt = df['price'].mean()
                if is_thermostat:
                    hist_on_mask = df['status_val'] >= float(current_device.get("target_temp_high", 10.0))
                else:
                    hist_on_mask = df['status'].astype(str).str.upper() == 'ON'
                
                hist_on_snitt = df.loc[hist_on_mask, 'price'].mean() if hist_on_mask.any() else 0.0
                hist_diff = hist_snitt - hist_on_snitt
                hist_effekt = ((hist_snitt - hist_on_snitt) / hist_snitt * 100) if hist_snitt > 0 else 0.0

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Aktuellt pris", f"{latest.get('price', 0):.2f} öre/kWh")
                c2.metric("Måltemp / Status", latest.get('status', 'N/A'))
                c3.metric("Uppmätt enhetstemp", f"{latest.get('device_temp', latest.get('temp', 0)):.1f} °C")
                c4.metric("Senast uppdaterad", latest.get('time', datetime.now()).strftime('%H:%M'))

                fig = make_subplots(specs=[[{"secondary_y": True}]])
                if is_thermostat:
                    fig.add_trace(go.Scatter(x=df['time'], y=df['status_val'], name="Måltemperatur (°C)", mode="lines+markers", line=dict(shape="hv", color="#2ecc71")), secondary_y=True)
                else:
                    status_colors = df['status'].apply(lambda x: '#2ecc71' if str(x).upper() == 'ON' else '#e74c3c')
                    fig.add_trace(go.Scatter(x=df['time'], y=df['status_num'], name="Status", mode="lines+markers", line=dict(shape="hv", color="rgba(150,150,150,0.3)"), marker=dict(color=status_colors)), secondary_y=True)

                fig.add_trace(go.Scatter(x=df['time'], y=df['price'], name="Elpris (öre/kWh)", line=dict(color="#29b6f6")), secondary_y=False)
                fig.update_layout(hovermode="x unified", height=400, margin=dict(l=10, r=10, t=30, b=10))
                st.plotly_chart(fig, width='stretch')
            else:
                st.info("Ingen historik för denna enhet ännu.")
        else:
            st.info("Ingen historik registrerad ännu.")

# --- TAB 3: ENHETSINSTÄLLNINGAR ---
with tab_settings:
    if not current_device:
        st.info("Ingen enhet vald.")
    else:
        st.subheader(f"⚙️ Inställningar för {current_device.get('name')}")
        is_thermostat = current_device.get("type") == "thermostat"

        if is_thermostat:
            t_high = st.number_input("Hög temperatur (Komfort) [°C]", min_value=5.0, max_value=30.0, value=float(current_device.get("target_temp_high", 10.0)), step=0.5)
            t_low = st.number_input("Låg temperatur (Eco) [°C]", min_value=5.0, max_value=30.0, value=float(current_device.get("target_temp_low", 6.0)), step=0.5)
        else:
            min_on = st.slider("Minsta körtid per dygn (timmar)", 4, 24, int(current_device.get("min_on_blocks", 44) / 4))
            max_off = st.slider("Max avstängning i rad (timmar)", 1, 6, int(current_device.get("max_off_blocks", 12) / 4))

        force = st.radio("Driftläge", ["AUTO", "PÅ (Tvinga igång)", "AV (Stäng av allt)"], index=["AUTO", "PÅ (Tvinga igång)", "AV (Stäng av allt)"].index(current_device.get("force_override", "AUTO")))

        if st.button("💾 Spara enhetsinställningar", type="primary"):
            if is_thermostat:
                current_device.update({"target_temp_high": t_high, "target_temp_low": t_low, "force_override": force})
            else:
                current_device.update({"min_on_blocks": min_on * 4, "max_off_blocks": max_off * 4, "force_override": force})
            save_config(config)
            st.cache_data.clear()
            st.rerun()

# --- TAB 4: SCHEMA & PRISER ---
with tab_sched:
    st.subheader(f"📅 Schema för {current_house.get('name')}")
    if current_device:
        is_thermostat = current_device.get("type") == "thermostat"
        now = datetime.now(sweden_tz)
        today_str = now.strftime("%Y-%m-%d")
        prices = get_nordpool_price(zone=current_house.get("zone", "SE2"), target_date=now)
        temp_forecast = get_hourly_temp_forecast(lat=current_house.get("lat", 63.25), lon=current_house.get("lon", 14.56), target_date=now)

        schedule = current_house.get("daily_schedule", [True]*96)
        times = [f"{h:02d}:{m:02d}" for h in range(24) for m in [0, 15, 30, 45]]

        fig_s = make_subplots(specs=[[{"secondary_y": True}]])
        if is_thermostat:
            t_high = float(current_device.get("target_temp_high", 10.0))
            t_low = float(current_device.get("target_temp_low", 6.0))
            vals = [t_high if s else t_low for s in schedule[:96]]
            fig_s.add_trace(go.Scatter(x=times, y=vals, name="Planerad Måltemp (°C)", mode="lines", line=dict(shape="hv", color="#2ecc71")), secondary_y=True)
        else:
            vals = [1 if s else 0 for s in schedule[:96]]
            fig_s.add_trace(go.Scatter(x=times, y=vals, name="Planerad Status", mode="lines", line=dict(shape="hv", color="#2ecc71")), secondary_y=True)

        fig_s.add_trace(go.Scatter(x=times, y=prices[:96], name="Elpris (öre/kWh)", line=dict(color="#29b6f6")), secondary_y=False)
        fig_s.update_layout(hovermode="x unified", height=400, margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig_s, width='stretch')

# --- TAB 5: SYSTEM ADMIN (ENBART SYNLIG FÖR ADMIN) ---
if is_admin and tab_admin:
    with tab_admin:
        st.subheader("🛠️ Systemadministration & Lösenordshantering")
        
        # 1. Lösenordshantering
        st.write("### 🔑 Hantera Lösenord")
        admin_pw_input = st.text_input("Ändra Admin-lösenord", value=config.get("admin_password", "admin123"))
        if st.button("💾 Uppdatera Admin-lösenord"):
            config["admin_password"] = admin_pw_input
            save_config(config)
            st.success("Admin-lösenordet har uppdaterats!")

        st.divider()
        st.write("### 🏠 Huslösenord")
        for h_k, h_v in houses.items():
            col_a, col_b = st.columns([2, 2])
            with col_a:
                st.write(f"**{h_v.get('name', h_k)}** (Användarnamn: `{h_v.get('name', h_k)}`)")
            with col_b:
                new_h_pw = st.text_input(f"Lösenord för {h_v.get('name')}", value=h_v.get("password", "hus123"), key=f"pw_{h_k}")
                if new_h_pw != h_v.get("password"):
                    h_v["password"] = new_h_pw
                    save_config(config)
                    st.success(f"Lösenord för {h_v.get('name')} sparades!")

        st.divider()
        st.write("### ➕ Lägg till nytt hus")
        new_h_id = st.text_input("Hus-ID (t.ex. 'stugan_2')")
        new_h_name = st.text_input("Husets Namn / Användarnamn (t.ex. 'Fjällstugan')")
        new_h_pw = st.text_input("Inledande lösenord", value="hus123")
        new_h_lat = st.number_input("Latitud", value=63.30)
        new_h_lon = st.number_input("Longitud", value=14.70)
        new_h_stat = st.text_input("Temperatur.nu station", value="ronnofors")
        new_h_zone = st.selectbox("Elområde", ["SE1", "SE2", "SE3", "SE4"], index=1)

        if st.button("Skapa Hus"):
            if new_h_id and new_h_name:
                houses[new_h_id] = {
                    "name": new_h_name,
                    "password": new_h_pw,
                    "lat": new_h_lat,
                    "lon": new_h_lon,
                    "temperatur_nu_station": new_h_stat,
                    "zone": new_h_zone,
                    "devices": []
                }
                save_config(config)
                st.success(f"Huset '{new_h_name}' har skapats!")
                st.rerun()

        st.divider()
        st.write("### 🔌 Hantera Enheter för valt hus")
        if current_house:
            dev_name = st.text_input("Enhetsnamn")
            dev_tuya_id = st.text_input("Tuya Device ID")
            dev_type = st.selectbox("Typ", ["switch", "thermostat"])
            if st.button("Lägg till Enhet"):
                if dev_name and dev_tuya_id:
                    new_dev = {
                        "id": f"dev_{len(current_house.get('devices', []))+1}",
                        "device_id": dev_tuya_id,
                        "name": dev_name,
                        "type": dev_type,
                        "force_override": "AUTO"
                    }
                    if dev_type == "thermostat":
                        new_dev.update({"target_temp_high": 10.0, "target_temp_low": 6.0})
                    else:
                        new_dev.update({"min_on_blocks": 44, "max_off_blocks": 12, "price_cap": 50.0})
                    current_house.setdefault("devices", []).append(new_dev)
                    save_config(config)
                    st.success(f"Enhet '{dev_name}' tillagd!")
                    st.rerun()