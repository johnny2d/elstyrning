import streamlit as st
import os
import requests
from zoneinfo import ZoneInfo
sweden_tz = ZoneInfo("Europe/Stockholm")
import json
from datetime import datetime, time, timedelta
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

def check_password():
    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False

    if not st.session_state["password_correct"]:
        st.title("🔒 Logga in på Elstyrningen")
        pwd = st.text_input("Lösenord", type="password")
        if st.button("Logga in"):
            if pwd == st.secrets.get("APP_PASSWORD", "mitthemligalosenord"):
                st.session_state["password_correct"] = True
                st.rerun()
            else:
                st.error("Fel lösenord!")
        return False
    return True

if not check_password():
    st.stop()

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
    
    return {
        "houses": {
            "hus_as": {
                "name": "Huvudhuset (Ås)",
                "lat": 63.25,
                "lon": 14.56,
                "temperatur_nu_station": "ostersund",
                "zone": "SE2",
                "devices": [
                    {
                        "id": "dev_1",
                        "device_id": "",
                        "name": "Standard Element",
                        "type": "switch",
                        "min_on_blocks": 44,
                        "max_off_blocks": 12,
                        "price_cap": 50.0,
                        "cold_temp_threshold": 0,
                        "min_minutes_cold": 15,
                        "force_override": "AUTO"
                    }
                ]
            }
        }
    }, []

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
        st.success("Inställningarna sparades till Gist!")
    else:
        st.error(f"Ett fel uppstod vid sparande: {res.status_code}")

config, history = load_gist_data()

if "houses" not in config:
    config = {
        "houses": {
            "huvudkontor": {
                "name": "Huvudhuset",
                "lat": config.get("lat", 63.25),
                "lon": config.get("lon", 14.56),
                "temperatur_nu_station": config.get("temperatur_nu_station", "ostersund"),
                "zone": config.get("zone", "SE2"),
                "schedule_date": config.get("schedule_date"),
                "daily_schedule": config.get("daily_schedule"),
                "tomorrow_schedule_date": config.get("tomorrow_schedule_date"),
                "tomorrow_schedule": config.get("tomorrow_schedule"),
                "ai_latest_reasoning": config.get("ai_latest_reasoning"),
                "devices": [
                    {
                        "id": "dev_1",
                        "device_id": os.getenv("TUYA_DEVICE_ID", ""),
                        "name": "Standard Enhet",
                        "type": "switch",
                        "max_off_blocks": config.get("max_off_blocks", 12),
                        "min_on_blocks": config.get("min_on_blocks", 44),
                        "price_cap": config.get("price_cap", 50.0),
                        "cold_temp_threshold": config.get("cold_temp_threshold", 0),
                        "min_minutes_cold": config.get("min_minutes_cold", 15),
                        "force_override": config.get("force_override", "AUTO")
                    }
                ]
            }
        }
    }

st.title("⚡ Elstyrning & Flerbostadspanel")

# --- SIDOFÄLT: HUS-, ENHETSVÄLJARE OCH FORCERING ---
with st.sidebar:
    st.write("### 🏠 Välj Hus & Enhet")
    houses = config.get("houses", {})
    house_keys = list(houses.keys())
    
    if not house_keys:
        st.error("Inga hus hittades i konfigurationen.")
        st.stop()
        
    selected_house_key = st.selectbox(
        "Hus", 
        house_keys, 
        format_func=lambda k: houses[k].get("name", k)
    )
    current_house = houses[selected_house_key]
    
    devices = current_house.get("devices", [])
    if devices:
        device_names = [d.get("name", d.get("id")) for d in devices]
        selected_device_idx = st.selectbox(
            "Enhet", 
            range(len(devices)), 
            format_func=lambda i: device_names[i]
        )
        current_device = devices[selected_device_idx]
    else:
        st.info("💡 Huset har inga enheter än.")
        current_device = None

    st.divider()
    if st.button("🔄 Uppdatera sida"):
        st.rerun()

    st.write("### ⚡ AI-Styrning")
    if st.button("🚀 Forcera omräkning (Alla hus)", type="primary"):
        with st.spinner("Beräknar om scheman och AI-parametrar för samtliga hus..."):
            try:
                now = datetime.now(sweden_tz)
                today_str = now.strftime("%Y-%m-%d")
                tomorrow_dt = now + timedelta(days=1)
                tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")

                for h_key, h_data in houses.items():
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
                    # Lägg till paus innan nästa hus beräknas i forceringen
                    time.sleep(12)
                    h_data["schedule_date"] = today_str
                    h_data["daily_schedule"] = daily_sched
                    h_data["ai_latest_reasoning"] = reasoning_today
                    h_data["last_ai_update"] = now.strftime("%Y-%m-%d %H:%M")

                    if ref_dev:
                        ref_dev["price_cap"] = p_cap_today
                        ref_dev["min_on_blocks"] = m_blocks_today

                    try:
                        prices_tom = get_nordpool_price(zone=h_zone, target_date=tomorrow_dt)
                        if len(set(prices_tom)) > 1:
                            temp_forecast_tom = get_hourly_temp_forecast(lat=h_lat, lon=h_lon, target_date=tomorrow_dt)
                            p_cap_tom, m_blocks_tom, reasoning_tom = get_ai_optimized_parameters(
                                prices_tom, temp_forecast_tom, default_price_cap=def_cap, default_min_on_blocks=def_min_on, target_day_label=f"imorgon ({h_data.get('name')})"
                            )
                            tom_sched = generate_daily_schedule(
                                prices_tom, max_off_blocks=max_off, min_on_blocks=m_blocks_tom, current_temp=current_temp, price_cap=p_cap_tom
                            )
                            h_data["tomorrow_schedule_date"] = tomorrow_str
                            h_data["tomorrow_schedule"] = tom_sched
                    except Exception:
                        pass

                save_config(config)
                st.cache_data.clear()
                st.success("Alla hus och scheman har uppdaterats med nya AI-beräkningar!")
                st.rerun()
            except Exception as e:
                st.error(f"Ett fel uppstod vid AI-omräkning: {e}")

if current_house.get("ai_latest_reasoning"):
    st.info(f"🤖 **AI-Strategens analys ({current_house.get('name')}):** {current_house.get('ai_latest_reasoning')}")

tab_overview, tab1, tab2, tab3, tab4 = st.tabs(["🏠 Överblick Alla Hus", "📊 Övervakning & Graf", "⚙️ Inställningar", "📅 Schema & Priser", "🛠️ Admin Hus & Enheter"])

# --- TAB OVERVIEW: SAMMANSTÄLLNING FÖR ALLA HUS ---
with tab_overview:
    st.subheader("🏠 Övergripande status för alla fastigheter")
    
    if houses:
        for h_key, h_data in houses.items():
            with st.container():
                st.markdown(f"### 📍 {h_data.get('name', h_key)}")
                h_temp = get_outside_temp(station=h_data.get("temperatur_nu_station"), lat=h_data.get("lat", 63.25), lon=h_data.get("lon", 14.56))
                
                cols = st.columns(4)
                cols[0].metric("Utetemperatur", f"{h_temp:.1f} °C")
                cols[1].metric("Elområde", h_data.get("zone", "SE2"))
                cols[2].metric("Antal enheter", len(h_data.get("devices", [])))
                
                device_statuses = []
                for dev in h_data.get("devices", []):
                    dev_name = dev.get("name")
                    connected, status_msg, _ = get_tuya_device_status(dev.get("device_id"), device_type=dev.get("type", "switch")) if dev.get("device_id") else (False, "Ej konfigurerad", None)
                    device_statuses.append(f"**{dev_name}**: {status_msg}" + (" ⚠️" if not connected and dev.get("device_id") else ""))
                
                cols[3].markdown("**Enhetsstatus:**<br>" + ("<br>".join(device_statuses) if device_statuses else "<i>Inga enheter inlagda än</i>"), unsafe_allow_html=True)
                st.divider()
    else:
        st.info("Inga fastigheter registrerade.")

# --- TAB 1: HISTORIK OCH GRAFER ---
with tab1:
    if not current_device:
        st.warning(f"🏠 Huset **{current_house.get('name')}** har inga styrsystem eller enheter inlagda än.")
        st.info("👉 Gå till fliken **🛠️ Admin Hus & Enheter** längst till höger för att lägga till din första termostat eller reläomkopplare!")
    else:
        is_thermostat = current_device.get("type") == "thermostat"
        st.subheader(f"Övervakning för {current_house.get('name')} – {current_device.get('name')} ({'Termostat' if is_thermostat else 'Switch'})")
        
        if history:
            df = pd.DataFrame(history)
            
            if 'house' in df.columns and 'device' in df.columns:
                df = df[(df['house'] == current_house.get('name')) & (df['device'] == current_device.get('name'))]
            
            if not df.empty:
                df['time'] = pd.to_datetime(df['timestamp'])
                
                if is_thermostat:
                    def parse_status_val(val):
                        if pd.isna(val):
                            return float(current_device.get("target_temp_low", 6.0))
                        val_str = str(val).replace('°C', '').strip()
                        try:
                            return float(val_str)
                        except ValueError:
                            if val_str.upper() == 'ON':
                                return float(current_device.get("target_temp_high", 10.0))
                            elif val_str.upper() == 'OFF':
                                return float(current_device.get("target_temp_low", 6.0))
                            return float(current_device.get("target_temp_low", 6.0))

                    df['status_val'] = df['status'].apply(parse_status_val)
                else:
                    df['status_num'] = df['status'].apply(lambda x: 1 if str(x).upper() == 'ON' else 0)
                
                latest = df.iloc[-1]
                
                hist_snitt = df['price'].mean()
                if is_thermostat:
                    t_high = float(current_device.get("target_temp_high", 10.0))
                    hist_on_mask = df['status_val'] >= t_high
                else:
                    hist_on_mask = df['status'].astype(str).str.upper() == 'ON'
                    
                hist_on_snitt = df.loc[hist_on_mask, 'price'].mean() if hist_on_mask.any() else 0.0
                hist_diff = hist_snitt - hist_on_snitt
                hist_effekt = ((hist_snitt - hist_on_snitt) / hist_snitt * 100) if hist_snitt > 0 else 0.0

                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Aktuellt pris", f"{latest.get('price', 0):.2f} öre/kWh")
                col2.metric("Måltemperatur / Status", latest.get('status', 'N/A'))
                col3.metric("Uppmätt enhetstemp", f"{latest.get('device_temp', latest.get('temp', 0)):.1f} °C")
                col4.metric("Senast uppdaterad", latest.get('time', datetime.now()).strftime('%H:%M'))

                kpi1, kpi2, kpi3 = st.columns(3)
                kpi1.metric("Historiskt snittpris", f"{hist_snitt:.1f} öre")
                kpi2.metric(
                    "Snittpris vid aktiv drift", 
                    f"{hist_on_snitt:.1f} öre", 
                    delta=f"-{hist_diff:.1f} öre" if hist_diff >= 0 else f"+{abs(hist_diff):.1f} öre",
                    delta_color="normal" if hist_diff >= 0 else "inverse"
                )
                kpi3.metric("Uppmätt optimeringseffekt", f"{hist_effekt:.1f}% billigare")

                st.divider()

                fig = make_subplots(specs=[[{"secondary_y": True}]])
                
                if is_thermostat:
                    fig.add_trace(
                        go.Scatter(
                            x=df['time'], y=df['status_val'],
                            name="Måltemperatur (°C)", mode="lines+markers",
                            line=dict(shape="hv", color="#2ecc71", width=2),
                            marker=dict(size=6, color="#2980b9")
                        ),
                        secondary_y=True
                    )
                else:
                    status_colors = df['status'].apply(lambda x: '#2ecc71' if str(x).upper() == 'ON' else '#e74c3c')
                    fig.add_trace(
                        go.Scatter(
                            x=df['time'], y=df['status_num'],
                            name="Enhetsstatus", mode="lines+markers",
                            line=dict(shape="hv", color="rgba(150, 150, 150, 0.3)", width=2),
                            marker=dict(size=8, color=status_colors, symbol="circle"),
                            hovertemplate="Tid: %{x|%H:%M}<br>Status: %{customdata}<extra></extra>",
                            customdata=df['status']
                        ),
                        secondary_y=True
                    )

                fig.add_trace(
                    go.Scatter(
                        x=df['time'], y=df['price'],
                        name="Elpris (öre/kWh)", mode="lines+markers",
                        line=dict(color="#29b6f6", width=2),
                        marker=dict(size=5, color="#29b6f6"),
                        hovertemplate="Tid: %{x|%H:%M}<br>Pris: %{y:.2f} öre/kWh<extra></extra>"
                    ),
                    secondary_y=False
                )

                fig.add_trace(
                    go.Scatter(
                        x=df['time'], y=df.get('device_temp', df['temp']),
                        name="Uppmätt enhetstemp (°C)", mode="lines",
                        line=dict(color="#e74c3c", width=2, dash="dash"),
                        hovertemplate="Tid: %{x|%H:%M}<br>Enhetstemp: %{y:.1f} °C<extra></extra>"
                    ),
                    secondary_y=False
                )

                fig.update_layout(
                    hovermode="x unified",
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                    margin=dict(l=10, r=10, t=30, b=10),
                    height=440
                )

                fig.update_yaxes(title_text="Elpris (öre) & Temp (°C)", secondary_y=False)
                if is_thermostat:
                    fig.update_yaxes(title_text="Måltemperatur (°C)", secondary_y=True)
                else:
                    fig.update_yaxes(title_text="Status", tickvals=[0, 1], ticktext=["AV 🔴", "PÅ 🟢"], range=[-0.2, 1.2], secondary_y=True)

                st.plotly_chart(fig, width='stretch')
                
                with st.expander("Visa rådata / logg med orsaker"):
                    cols_to_show = [c for c in ['timestamp', 'price', 'status', 'temp', 'device_temp', 'reason'] if c in df.columns]
                    st.dataframe(df[cols_to_show].sort_values(by='timestamp', ascending=False), width='stretch')
            else:
                st.info("Ingen historik matchar det valda huset och enheten än.")
        else:
            st.info("Ingen historik har loggats än.")

        st.divider()
        tuya_connected, tuya_msg, device_measured_temp = get_tuya_device_status(current_device.get("device_id"), device_type=current_device.get("type", "switch")) if current_device.get("device_id") else (False, "Ej konfigurerad", None)
        outside_temp = get_outside_temp(station=current_house.get("temperatur_nu_station"), lat=current_house.get("lat", 63.25), lon=current_house.get("lon", 14.56))
        
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Aktivt Pristak", f"{current_device.get('price_cap', 50):.1f} öre")
        with col2:
            st.metric("Uppmätt Givatemperatur", f"{device_measured_temp:.1f} °C" if device_measured_temp is not None else f"{outside_temp:.1f} °C (Ute)")
        with col3:
            st.metric("Tuya Status / Måltemp", tuya_msg)

        if not tuya_connected and current_device.get("device_id"):
            st.warning(f"⚠️ Varning: Kunde inte nå Tuya moln-API för enhet '{current_device.get('name')}'.")

# --- TAB 2: INSTÄLLNINGAR ---
with tab2:
    if not current_device:
        st.info("Inga enheter finns registrerade för detta hus ännu.")
    else:
        st.subheader(f"Inställningar för enhet: {current_device.get('name')} ({current_device.get('type', 'switch').capitalize()})")
        
        is_thermostat = current_device.get("type") == "thermostat"
        
        if is_thermostat:
            st.write("### 🌡️ Termostatinställningar (Börvärden)")
            target_temp_high = st.number_input("Hög temperatur (Komfort / Aktiv när billigt) [°C]", min_value=5.0, max_value=30.0, value=float(current_device.get("target_temp_high", 10.0)), step=0.5)
            target_temp_low = st.number_input("Låg temperatur (Eco / Grundskydd när dyrt) [°C]", min_value=5.0, max_value=30.0, value=float(current_device.get("target_temp_low", 6.0)), step=0.5)
            
            st.divider()
            min_on_hours = 12
            max_off_hours = 4
            cold_temp_threshold = 0
            min_minutes_cold = 15
        else:
            min_on_hours = st.slider("Minsta totala körtid per dygn (timmar)", min_value=4, max_value=24, value=int(current_device.get("min_on_blocks", 44) / 4), step=1)
            max_off_hours = st.slider("Maximal avstängning i rad (timmar)", min_value=1, max_value=6, value=int(current_device.get("max_off_blocks", 12) / 4), step=1)
            
            st.divider()
            cold_temp_threshold = st.number_input("Gräns för kyla (°C)", min_value=-30, max_value=10, value=current_device.get("cold_temp_threshold", 0))
            min_minutes_cold = st.select_slider("Minst antal minuter körtid per timme vid kyla", options=[0, 15, 30, 45, 60], value=current_device.get("min_minutes_cold", 15))

        st.divider()
        force_override = st.radio("Driftläge", options=["AUTO", "PÅ (Tvinga igång)", "AV (Stäng av allt)"], index=["AUTO", "PÅ (Tvinga igång)", "AV (Stäng av allt)"].index(current_device.get("force_override", "AUTO")))

        if st.button("💾 Spara enhetsinställningar", type="primary"):
            if is_thermostat:
                current_device.update({
                    "target_temp_high": target_temp_high,
                    "target_temp_low": target_temp_low,
                    "force_override": force_override
                })
            else:
                current_device.update({
                    "min_on_blocks": min_on_hours * 4,
                    "max_off_blocks": max_off_hours * 4,
                    "cold_temp_threshold": cold_temp_threshold,
                    "min_minutes_cold": min_minutes_cold,
                    "force_override": force_override
                })
            save_config(config)
            st.cache_data.clear()
            st.rerun()

# --- TAB 3: SCHEMA & PRISER ---
with tab3:
    st.subheader(f"📅 Planerat elschema & temperaturprofil för {current_house.get('name')}")
    
    house_devices = current_house.get("devices", [])
    if len(house_devices) > 1:
        dev_names = [d.get("name", d.get("id")) for d in house_devices]
        selected_tab3_dev_idx = st.selectbox(
            "Välj enhet att granska i schema:", 
            range(len(house_devices)), 
            format_func=lambda i: dev_names[i],
            key="tab3_device_selector"
        )
        target_device = house_devices[selected_tab3_dev_idx]
    else:
        target_device = current_device if current_device else (house_devices[0] if house_devices else {})

    if not target_device:
        st.warning("Ingen enhet inlagd att visa schema för ännu.")
    else:
        is_thermostat = target_device.get("type") == "thermostat"
        st.markdown(f"**Visar profil för enhet:** `{target_device.get('name')}` ({'Termostat' if is_thermostat else 'Switch'})")
        
        now = datetime.now(sweden_tz)
        today_str = now.strftime("%Y-%m-%d")
        tomorrow_dt = now + timedelta(days=1)
        tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")

        selected_day_tab = st.radio("Välj dag att granska:", ["Idag (" + today_str + ")", "Imorgon (" + tomorrow_str + ")"], horizontal=True, key="tab3_day_radio")

        is_tomorrow = "Imorgon" in selected_day_tab
        target_dt = now if not is_tomorrow else tomorrow_dt
        house_zone = current_house.get("zone", "SE2")
        
        prices = get_nordpool_price(zone=house_zone, target_date=target_dt)
        
        prices_has_arrived = True
        if is_tomorrow:
            if not prices or len(set(prices)) <= 1:
                prices_has_arrived = False

        if is_tomorrow and not prices_has_arrived:
            st.info(f"⏳ **Morgondagens elprisprognos har inte kommit ännu.**\n\nElpriset Just Nu brukar publicera morgondagens priser dagligen runt kl. **13:00–14:00**. Vänligen återkom efter det för att se det optimerade schemat och prognosen för {tomorrow_str}.")
        else:
            temp_forecast = get_hourly_temp_forecast(lat=current_house.get("lat", 63.25), lon=current_house.get("lon", 14.56), target_date=target_dt)

            temp_96 = []
            for t_val in temp_forecast:
                temp_96.extend([t_val] * 4)
            while len(temp_96) < 96:
                temp_96.append(temp_96[-1] if temp_96 else 0.0)

            if not is_tomorrow:
                schedule = current_house.get("daily_schedule")
                if not schedule or len(schedule) != 96:
                    schedule = [True] * 96
            else:
                schedule = current_house.get("tomorrow_schedule")
                if current_house.get("tomorrow_schedule_date") != tomorrow_str or not schedule:
                    max_off = target_device.get("max_off_blocks", 12)
                    min_on = target_device.get("min_on_blocks", 44)
                    price_cap = target_device.get("price_cap", 50.0)
                    current_temp_val = get_outside_temp(station=current_house.get("temperatur_nu_station"), lat=current_house.get("lat", 63.25), lon=current_house.get("lon", 14.56))
                    schedule = generate_daily_schedule(prices, max_off_blocks=max_off, min_on_blocks=min_on, current_temp=current_temp_val, price_cap=price_cap)

            times = []
            for h in range(24):
                for m in [0, 15, 30, 45]:
                    times.append(f"{h:02d}:{m:02d}")

            if is_thermostat:
                t_high = float(target_device.get("target_temp_high", 10.0))
                t_low = float(target_device.get("target_temp_low", 6.0))
                status_vals = [t_high if s else t_low for s in schedule[:len(times)]]
                status_labels = [f"{v}°C" for v in status_vals]
                df_view = pd.DataFrame({
                    "Tid": times,
                    "Pris (öre/kWh)": prices[:len(times)],
                    "Utetemperatur (°C)": temp_96[:len(times)],
                    "Måltemperatur": status_labels,
                    "status_num": status_vals
                })
            else:
                df_view = pd.DataFrame({
                    "Tid": times,
                    "Pris (öre/kWh)": prices[:len(times)],
                    "Utetemperatur (°C)": temp_96[:len(times)],
                    "Status": ["PÅ 🟢" if s else "AV 🔴" for s in schedule[:len(times)]],
                    "status_num": [1 if s else 0 for s in schedule[:len(times)]]
                })

            dygn_snittpris = df_view["Pris (öre/kWh)"].mean()
            if is_thermostat:
                pa_mask = df_view["status_num"] >= float(target_device.get("target_temp_high", 10.0))
            else:
                pa_mask = df_view["status_num"] == 1

            if pa_mask.any():
                valda_snittpris = df_view.loc[pa_mask, "Pris (öre/kWh)"].mean()
                besparing_pct = ((dygn_snittpris - valda_snittpris) / dygn_snittpris) * 100 if dygn_snittpris > 0 else 0.0
            else:
                valda_snittpris = 0.0
                besparing_pct = 0.0

            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric(label="Dygnets snittpris", value=f"{dygn_snittpris:.1f} öre")
            with col2:
                diff = dygn_snittpris - valda_snittpris
                st.metric(
                    label="Snittpris vid aktiv drifttid",
                    value=f"{valda_snittpris:.1f} öre",
                    delta=f"-{diff:.1f} öre" if diff >= 0 else f"+{abs(diff):.1f} öre",
                    delta_color="normal" if diff >= 0 else "inverse"
                )
            with col3:
                st.metric(label="Optimeringseffekt", value=f"{besparing_pct:.1f}% billigare")

            st.divider()

            fig_view = make_subplots(specs=[[{"secondary_y": True}]])
            
            if is_thermostat:
                fig_view.add_trace(
                    go.Scatter(
                        x=df_view["Tid"], y=df_view["status_num"],
                        name="Estimerad Måltemperatur (°C)", mode="lines+markers",
                        line=dict(shape="hv", color="#2ecc71", width=2),
                        marker=dict(size=6, color="#2980b9")
                    ),
                    secondary_y=True
                )
            else:
                status_colors = ['#2ecc71' if s else '#e74c3c' for s in schedule[:len(times)]]
                fig_view.add_trace(
                    go.Scatter(
                        x=df_view["Tid"], y=df_view["status_num"],
                        name="Planerad Status", mode="lines+markers",
                        line=dict(shape="hv", color="rgba(150, 150, 150, 0.3)", width=2),
                        marker=dict(size=8, color=status_colors)
                    ),
                    secondary_y=True
                )

            fig_view.add_trace(
                go.Scatter(
                    x=df_view["Tid"], y=df_view["Pris (öre/kWh)"],
                    name="Elpris (öre/kWh)", mode="lines",
                    line=dict(color="#29b6f6", width=2)
                ),
                secondary_y=False
            )

            fig_view.add_trace(
                go.Scatter(
                    x=df_view["Tid"], y=df_view["Utetemperatur (°C)"],
                    name="Prognos Utetemp (°C)", mode="lines",
                    line=dict(color="#ffa726", width=2, dash="dot")
                ),
                secondary_y=False
            )

            fig_view.update_layout(
                hovermode="x unified",
                margin=dict(l=10, r=10, t=30, b=10),
                height=420,
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
            )
            fig_view.update_yaxes(title_text="Elpris (öre) & Utetemp (°C)", secondary_y=False)
            if is_thermostat:
                fig_view.update_yaxes(title_text="Måltemperatur (°C)", secondary_y=True)
            else:
                fig_view.update_yaxes(title_text="Status", tickvals=[0, 1], ticktext=["AV", "PÅ"], range=[-0.2, 1.2], secondary_y=True)

            st.plotly_chart(fig_view, width='stretch')

            with st.expander("Visa tabell med samtliga kvartar"):
                cols_to_display = ["Tid", "Pris (öre/kWh)", "Utetemperatur (°C)", "Måltemperatur"] if is_thermostat else ["Tid", "Pris (öre/kWh)", "Utetemperatur (°C)", "Status"]
                st.dataframe(df_view[cols_to_display], width='stretch')

# --- TAB 4: ADMIN / CRUD ---
with tab4:
    st.subheader("🛠️ Administrera Hus och Enheter")
    
    st.write("### Lägg till nytt hus")
    new_house_key = st.text_input("Unikt hus-ID (t.ex. 'stugan')", key="new_h_key")
    new_house_name = st.text_input("Visningsnamn (t.ex. 'Stugan')", key="new_h_name")
    new_lat = st.number_input("Latitud", value=63.30, key="new_h_lat")
    new_lon = st.number_input("Longitud", value=14.70, key="new_h_lon")
    new_station = st.text_input("Temperatur.nu station (t.ex. 'ronnofors')", key="new_h_stat")
    new_zone = st.selectbox("Elområde", options=["SE1", "SE2", "SE3", "SE4"], index=1, key="new_h_zone")
    
    if st.button("Lägg till hus"):
        if new_house_key and new_house_name:
            if new_house_key in houses:
                st.error("Ett hus med det ID:t finns redan!")
            else:
                houses[new_house_key] = {
                    "name": new_house_name,
                    "lat": new_lat,
                    "lon": new_lon,
                    "temperatur_nu_station": new_station,
                    "zone": new_zone,
                    "devices": []
                }
                save_config(config)
                st.success(f"Hus '{new_house_name}' tillagt!")
                st.rerun()
        else:
            st.warning("Fyll i både ID och namn.")

    st.divider()
    st.write("### Ta bort ett helt hus")
    if len(houses) > 1:
        house_to_delete = st.selectbox("Välj hus att radera", options=list(houses.keys()), format_func=lambda k: houses[k].get("name", k), key="del_h_select")
        if st.button("🗑️ Ta bort markerat hus", type="secondary"):
            removed_name = houses[house_to_delete].get("name", house_to_delete)
            del houses[house_to_delete]
            save_config(config)
            st.success(f"Huset '{removed_name}' har raderats.")
            st.rerun()
    else:
        st.info("Du måste ha minst ett hus kvar i systemet.")

    st.divider()
    st.write(f"### Hantera enheter i vald plats: {current_house.get('name')}")
    
    dev_name = st.text_input("Enhetens namn (t.ex. 'Badrum Golvvärme')", key="new_dev_name")
    dev_tuya_id = st.text_input("Tuya Device ID", key="new_dev_tuya")
    dev_type = st.selectbox("Enhetstyp", options=["switch", "thermostat"], format_func=lambda x: "Relä / Switch (Av/På)" if x == "switch" else "Golvvärmetermostat (Termostat)", key="new_dev_type")
    
    if st.button("Lägg till enhet"):
        if dev_name and dev_tuya_id:
            new_dev = {
                "id": f"dev_{len(current_house.get('devices', [])) + 1}",
                "device_id": dev_tuya_id,
                "name": dev_name,
                "type": dev_type,
                "force_override": "AUTO"
            }
            if dev_type == "thermostat":
                new_dev["target_temp_high"] = 10.0
                new_dev["target_temp_low"] = 6.0
            else:
                new_dev.update({
                    "min_on_blocks": 44,
                    "max_off_blocks": 12,
                    "price_cap": 50.0,
                    "cold_temp_threshold": 0,
                    "min_minutes_cold": 15
                })

            if "devices" not in current_house:
                current_house["devices"] = []
            current_house["devices"].append(new_dev)
            save_config(config)
            st.success(f"Enheten '{dev_name}' har lagts till!")
            st.rerun()
        else:
            st.warning("Ange både namn och Tuya Device ID.")

    st.divider()
    st.write("### Radera enhet")
    if current_house.get("devices"):
        dev_to_delete = st.selectbox("Välj enhet att ta bort", options=range(len(current_house["devices"])), format_func=lambda i: current_house["devices"][i]["name"], key="del_dev_select")
        if st.button("🗑️ Ta bort markerad enhet", type="secondary", key="del_dev_btn"):
            removed = current_house["devices"].pop(dev_to_delete)
            save_config(config)
            st.success(f"Enheten '{removed['name']}' har tagits bort.")
            st.rerun()
    else:
        st.info("Inga enheter att ta bort i detta hus.")