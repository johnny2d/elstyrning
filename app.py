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

if "houses" not in config:
    config["houses"] = {}
if "admin_password" not in config:
    config["admin_password"] = st.secrets.get("APP_PASSWORD", "admin123")

# --- AUTHENTICATION & SESSION ---
if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False
    st.session_state["user_role"] = None
    st.session_state["assigned_house_key"] = None

def login_user(username_input, password_input):
    username_clean = username_input.strip()
    if username_clean.lower() == "admin":
        if password_input == config.get("admin_password", "admin123"):
            st.session_state["authenticated"] = True
            st.session_state["user_role"] = "admin"
            st.session_state["assigned_house_key"] = None
            return True, "Välkommen Admin!"
        return False, "Fel lösenord för Admin."

    houses = config.get("houses", {})
    for h_key, h_data in houses.items():
        h_name = h_data.get("name", h_key)
        if username_clean.lower() in [h_name.lower(), h_key.lower()]:
            if password_input == h_data.get("password", "hus123"):
                st.session_state["authenticated"] = True
                st.session_state["user_role"] = "house"
                st.session_state["assigned_house_key"] = h_key
                return True, f"Välkommen till {h_name}!"
            return False, f"Fel lösenord för {h_name}."

    return False, "Användarnamnet hittades inte."

if not st.session_state["authenticated"]:
    st.markdown("<br><br>", unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.title("🔒 Logga in på Elstyrningen")
        st.caption("Ange husnamn (t.ex. 'Stugan') eller 'admin'.")
        with st.form("login_form"):
            u_in = st.text_input("Användarnamn")
            p_in = st.text_input("Lösenord", type="password")
            if st.form_submit_button("Logga in", use_container_width=True, type="primary"):
                ok, msg = login_user(u_in, p_in)
                if ok:
                    st.success(msg)
                    time.sleep(0.5)
                    st.rerun()
                else:
                    st.error(msg)
    st.stop()

is_admin = st.session_state["user_role"] == "admin"
houses = config.get("houses", {})

with st.sidebar:
    st.title("⚡ Elstyrning")
    if is_admin:
        st.subheader("👤 Inloggad: **Admin**")
        if houses:
            selected_house_key = st.selectbox("Aktivt Hus", list(houses.keys()), format_func=lambda k: houses[k].get("name", k))
            current_house = houses[selected_house_key]
        else:
            current_house = None
    else:
        current_house = houses.get(st.session_state["assigned_house_key"])
        if current_house:
            st.subheader(f"🏠 **{current_house.get('name')}**")

    current_device = None
    if current_house:
        devices = current_house.get("devices", [])
        if devices:
            device_names = [d.get("name", d.get("id")) for d in devices]
            selected_device_idx = st.selectbox("Välj Enhet", range(len(devices)), format_func=lambda i: device_names[i])
            current_device = devices[selected_device_idx]

    st.divider()
    if is_admin:
        st.write("### ⚡ Admin-verktyg")
        if st.button("🚀 Forcera AI-omräkning", type="primary"):
            with st.spinner("Beräknar om scheman..."):
                try:
                    now = datetime.now(sweden_tz)
                    today_str = now.strftime("%Y-%m-%d")
                    for h_key, h_data in houses.items():
                        time.sleep(12)
                        h_lat, h_lon = h_data.get("lat", 63.25), h_data.get("lon", 14.56)
                        h_zone, h_station = h_data.get("zone", "SE2"), h_data.get("temperatur_nu_station")
                        
                        curr_temp = get_outside_temp(station=h_station, lat=h_lat, lon=h_lon)
                        prices = get_nordpool_price(zone=h_zone, target_date=now)
                        forecast = get_hourly_temp_forecast(lat=h_lat, lon=h_lon, target_date=now)
                        
                        devs = h_data.get("devices", [])
                        ref = devs[0] if devs else {}
                        p_cap, m_blocks, reasoning = get_ai_optimized_parameters(prices, forecast, default_price_cap=ref.get("price_cap", 50.0), default_min_on_blocks=ref.get("min_on_blocks", 44), target_day_label=f"idag ({h_data.get('name')})")
                        sched = generate_daily_schedule(prices, max_off_blocks=ref.get("max_off_blocks", 12), min_on_blocks=m_blocks, current_temp=curr_temp, price_cap=p_cap)
                        
                        h_data.update({"schedule_date": today_str, "daily_schedule": sched, "ai_latest_reasoning": reasoning, "last_ai_update": now.strftime("%Y-%m-%d %H:%M")})
                    save_config(config)
                    st.cache_data.clear()
                    st.success("Klart!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Fel: {e}")

    if st.button("🚪 Logga ut"):
        st.session_state["authenticated"] = False
        st.session_state["user_role"] = None
        st.session_state["assigned_house_key"] = None
        st.rerun()

st.title(f"⚡ {current_house.get('name') if current_house else 'Elstyrning'}")

if current_house and current_house.get("ai_latest_reasoning"):
    st.info(f"🤖 **AI-Strategens analys:** {current_house.get('ai_latest_reasoning')}")

if is_admin:
    tab_dash, tab_hist, tab_settings, tab_sched, tab_admin = st.tabs(["🏠 Dashboard", "📊 Övervakning & Graf", "⚙️ Enhetsinställningar", "📅 Schema & Priser", "🛠️ System Admin"])
else:
    tab_dash, tab_hist, tab_settings, tab_sched = st.tabs(["🏠 Dashboard", "📊 Övervakning & Graf", "⚙️ Enhetsinställningar", "📅 Schema & Priser"])
    tab_admin = None

# --- TAB 1: DASHBOARD (MED SNITTPRIS OCH PROCENTBESPARING) ---
with tab_dash:
    st.subheader("📍 Realtidsöversikt & Ekonomi")
    
    outside_temp = get_outside_temp(station=current_house.get("temperatur_nu_station"), lat=current_house.get("lat", 63.25), lon=current_house.get("lon", 14.56)) if current_house else 0.0
    
    # Hämta dagens priser och dagschema för beräkning av snitt och besparing %
    now = datetime.now(sweden_tz)
    house_zone = current_house.get("zone", "SE2") if current_house else "SE2"
    today_prices = get_nordpool_price(zone=house_zone, target_date=now)
    
    avg_day_price = 0.0
    active_saving_pct = 0.0
    if today_prices and len(today_prices) > 0:
        avg_day_price = sum(today_prices) / len(today_prices)
        
        if current_house and "daily_schedule" in current_house:
            sched = current_house["daily_schedule"]
            active_prices = [today_prices[i] for i in range(min(len(today_prices), len(sched))) if sched[i]]
            if active_prices:
                avg_active_price = sum(active_prices) / len(active_prices)
                if avg_day_price > 0:
                    active_saving_pct = max(0.0, ((avg_day_price - avg_active_price) / avg_day_price) * 100.0)

    # Beräkna även veckans besparing i kronor
    week_savings_kr = 0.0
    if history and current_house:
        df_h = pd.DataFrame(history)
        if 'house' in df_h.columns and current_house.get('name') in df_h['house'].values:
            df_h = df_h[df_h['house'] == current_house.get('name')]
            if 'timestamp' in df_h.columns:
                df_h['dt'] = pd.to_datetime(df_h['timestamp'])
                week_ago = datetime.now() - timedelta(days=7)
                df_week = df_h[df_h['dt'] >= week_ago]
                if not df_week.empty and 'price' in df_week.columns:
                    w_avg = df_week['price'].mean()
                    active_mask = df_week['status'].astype(str).str.upper().isin(['ON', 'TRUE']) | (df_week['status'].astype(str).str.contains('°C'))
                    active_p = df_week.loc[active_mask, 'price']
                    if not active_p.empty:
                        saved_ore = w_avg - active_p.mean()
                        est_kwh = len(active_p) * 0.5 * 2.0
                        week_savings_kr = max(0.0, (saved_ore * est_kwh) / 100.0)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Utetemperatur", f"{outside_temp:.1f} °C")
    c2.metric("Elpris snitt (idag)", f"{avg_day_price:.1f} öre/kWh")
    c3.metric("Besparing (idag)", f"{active_saving_pct:.1f} %")
    c4.metric("Besparing (7 dgr)", f"{week_savings_kr:.2f} kr")
    c5.metric("Aktivt hus", current_house.get('name', 'Inget') if current_house else "-")

    st.divider()
    st.markdown("### 🎛️ Husets Enheter (Paneler)")
    
    if current_house:
        devs = current_house.get("devices", [])
        if devs:
            dev_cols = st.columns(len(devs) if len(devs) <= 3 else 3)
            for idx, dev in enumerate(devs):
                with dev_cols[idx % 3]:
                    with st.container(border=True):
                        st.markdown(f"#### 🔌 {dev.get('name')}")
                        is_term = dev.get("type") == "thermostat"
                        st.caption(f"Typ: {'Termostat' if is_term else 'Relä / Switch'}")
                        
                        tuya_conn, tuya_msg, dev_temp = get_tuya_device_status(
                            dev.get("device_id"), 
                            device_type=dev.get("type", "switch")
                        ) if dev.get("device_id") else (False, "Ej konfigurerad", None)
                        
                        st.markdown(f"**Status:** `{tuya_msg}`")
                        if dev_temp is not None:
                            st.markdown(f"**Givartemp:** `{dev_temp:.1f} °C`")
                        st.markdown(f"**Driftläge:** `{dev.get('force_override', 'AUTO')}`")
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
                    status_labels = df['status'].apply(lambda x: 'PÅ 🟢' if str(x).upper() == 'ON' else 'AV 🔴')
                    fig.add_trace(
                        go.Scatter(
                            x=df['time'], y=df['status_num'], name="Status", mode="lines+markers",
                            line=dict(shape="hv", color="rgba(150, 150, 150, 0.2)", width=2),
                            marker=dict(size=8, color=status_colors, symbol="circle"),
                            customdata=status_labels,
                            hovertemplate="Tid: %{x|%H:%M}<br>Status: %{customdata}<extra></extra>"
                        ),
                        secondary_y=True
                    )

                fig.add_trace(go.Scatter(x=df['time'], y=df['price'], name="Elpris (öre/kWh)", line=dict(color="#29b6f6")), secondary_y=False)
                fig.update_layout(hovermode="x unified", height=400, margin=dict(l=10, r=10, t=30, b=10))
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("Ingen historik för denna enhet ännu.")
        else:
            st.info("Ingen historik registrerad ännu.")

# --- TAB 3: ENHETSINSTÄLLNINGAR & HUSETS LÖSENORD ---
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

    st.divider()
    st.subheader("🔑 Ändra lösenord för detta hus")
    new_h_pw_input = st.text_input("Nytt lösenord för huset", type="password")
    if st.button("Uppdatera husets lösenord"):
        if new_h_pw_input:
            current_house["password"] = new_h_pw_input
            save_config(config)
            st.success("Lösenordet har uppdaterats!")

# --- TAB 4: SCHEMA & PRISER ---
with tab_sched:
    st.subheader(f"📅 Planerat elschema & temperaturprofil för {current_house.get('name') if current_house else 'Hus'}")
    
    if current_device:
        is_thermostat = current_device.get("type") == "thermostat"
        now = datetime.now(sweden_tz)
        today_str = now.strftime("%Y-%m-%d")
        tomorrow_dt = now + timedelta(days=1)
        tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")

        selected_day = st.radio("Välj dag att granska:", [f"Idag ({today_str})", f"Imorgon ({tomorrow_str})"], horizontal=True)
        is_tomorrow = "Imorgon" in selected_day
        target_dt = now if not is_tomorrow else tomorrow_dt
        
        house_zone = current_house.get("zone", "SE2")
        prices = get_nordpool_price(zone=house_zone, target_date=target_dt)
        
        prices_has_arrived = True
        if is_tomorrow:
            if not prices or len(set(prices)) <= 1:
                prices_has_arrived = False

        if is_tomorrow and not prices_has_arrived:
            st.info(f"⏳ **Morgondagens elprisprognos har inte publicerats ännu.**\n\nNordpool brukar publicera morgondagens priser dagligen runt kl. **13:00–14:00**. Vänligen återkom efter det för att se schemat för {tomorrow_str}.")
        else:
            temp_forecast = get_hourly_temp_forecast(lat=current_house.get("lat", 63.25), lon=current_house.get("lon", 14.56), target_date=target_dt)
            temp_96 = []
            for t_val in temp_forecast:
                temp_96.extend([t_val] * 4)
            while len(temp_96) < 96:
                temp_96.append(temp_96[-1] if temp_96 else 0.0)

            if not is_tomorrow:
                schedule = current_house.get("daily_schedule", [True]*96)
            else:
                schedule = current_house.get("tomorrow_schedule")
                if not schedule or current_house.get("tomorrow_schedule_date") != tomorrow_str:
                    max_off = current_device.get("max_off_blocks", 12)
                    min_on = current_device.get("min_on_blocks", 44)
                    price_cap = current_device.get("price_cap", 50.0)
                    curr_temp = get_outside_temp(station=current_house.get("temperatur_nu_station"), lat=current_house.get("lat", 63.25), lon=current_house.get("lon", 14.56))
                    schedule = generate_daily_schedule(prices, max_off_blocks=max_off, min_on_blocks=min_on, current_temp=curr_temp, price_cap=price_cap)
                    
                    current_house["tomorrow_schedule"] = schedule
                    current_house["tomorrow_schedule_date"] = tomorrow_str
                    save_config(config)

            times = [f"{h:02d}:{m:02d}" for h in range(24) for m in [0, 15, 30, 45]]
            times_extended = times + ["24:00"]

            fig_s = make_subplots(specs=[[{"secondary_y": True}]])
            if is_thermostat:
                t_high = float(current_device.get("target_temp_high", 10.0))
                t_low = float(current_device.get("target_temp_low", 6.0))
                vals = [t_high if s else t_low for s in schedule[:len(times)]]
                vals_extended = vals + [vals[-1]]
                fig_s.add_trace(go.Scatter(x=times_extended, y=vals_extended, name="Planerad Måltemp (°C)", mode="lines+markers", line=dict(shape="hv", color="#2ecc71")), secondary_y=True)
            else:
                vals = [1 if s else 0 for s in schedule[:len(times)]]
                vals_extended = vals + [vals[-1]]
                
                status_colors = ['#2ecc71' if s else '#e74c3c' for s in schedule[:len(times)]]
                status_colors_extended = status_colors + [status_colors[-1]]
                
                status_labels = ['PÅ 🟢' if s else 'AV 🔴' for s in schedule[:len(times)]]
                status_labels_extended = status_labels + [status_labels[-1]]
                
                fig_s.add_trace(
                    go.Scatter(
                        x=times_extended, y=vals_extended,
                        name="Status", mode="lines+markers",
                        line=dict(shape="hv", color="rgba(150, 150, 150, 0.2)", width=2),
                        marker=dict(size=8, color=status_colors_extended, symbol="circle"),
                        customdata=status_labels_extended,
                        hovertemplate="Tid: %{x}<br>Status: %{customdata}<extra></extra>"
                    ),
                    secondary_y=True
                )

            prices_extended = list(prices[:len(times)]) + [prices[len(times)-1] if len(prices) >= len(times) else 0]
            fig_s.add_trace(go.Scatter(x=times_extended, y=prices_extended, name="Elpris (öre/kWh)", line=dict(color="#29b6f6")), secondary_y=False)
            
            fig_s.update_layout(hovermode="x unified", height=400, margin=dict(l=10, r=10, t=30, b=10))
            fig_s.update_yaxes(title_text="Elpris (öre/kWh)", secondary_y=False)
            if is_thermostat:
                fig_s.update_yaxes(title_text="Måltemperatur (°C)", secondary_y=True)
            else:
                fig_s.update_yaxes(title_text="Status", tickvals=[0, 1], ticktext=["AV 🔴", "PÅ 🟢"], range=[-0.2, 1.2], secondary_y=True)

            st.plotly_chart(fig_s, use_container_width=True)

# --- TAB 5: SYSTEM ADMIN ---
if is_admin and tab_admin:
    with tab_admin:
        st.subheader("🛠️ Systemadministration & Lösenordshantering")
        
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
        new_h_name = st.text_input("Husets Namn / Användarnamn")
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