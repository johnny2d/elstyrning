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
    set_tuya_device,
    generate_daily_schedule, 
    get_nordpool_price, 
    get_outside_temp, 
    get_hourly_temp_forecast,
    get_ai_optimized_parameters,
    calculate_required_runtime
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
        st.caption("Ange husnamn (t.ex. 'Karlpers') eller 'admin'.")
        with st.form("login_form"):
            u_in = st.text_input("Användarnamn")
            p_in = st.text_input("Lösenord", type="password")
            if st.form_submit_button("Logga in", width="stretch", type="primary"):
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
selected_house_key = None

with st.sidebar:
    st.title("⚡ Elstyrning")
    if is_admin:
        st.subheader("👤 Inloggad: **Admin**")
        if houses:
            selected_house_key = st.selectbox("Aktivt Hus", list(houses.keys()), format_func=lambda k: f"{houses[k].get('name', k)} {'⏸️ (Pausat)' if houses[k].get('paused') else ''}")
            current_house = houses[selected_house_key]
        else:
            current_house = None
    else:
        selected_house_key = st.session_state.get("assigned_house_key")
        current_house = houses.get(selected_house_key)
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
            with st.spinner("Beräknar om scheman och hämtar ny väder- och prisdata för idag och imorgon..."):
                now = datetime.now(sweden_tz)
                today_str = now.strftime("%Y-%m-%d")
                tomorrow_dt = now + timedelta(days=1)
                tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")
                
                any_updated = False

                for h_key, h_data in houses.items():
                    if h_data.get("paused", False):
                        continue
                    
                    h_name = h_data.get("name", h_key)
                    try:
                        h_lat, h_lon = h_data.get("lat", 63.25), h_data.get("lon", 14.56)
                        h_zone = h_data.get("zone", "SE2")
                        h_station = h_data.get("temperatur_nu_station")
                        
                        curr_temp = get_outside_temp(station=h_station, lat=h_lat, lon=h_lon)
                        devs = h_data.get("devices", [])
                        ref = devs[0] if devs else {}
                        default_p_cap = ref.get("price_cap", 50.0)
                        house_target_temp = float(ref.get("target_room_temp", 20.0))

                        # --- 1. BERÄKNA IDAG ---
                        time.sleep(4)
                        prices_today = get_nordpool_price(zone=h_zone, target_date=now)
                        forecast_today = get_hourly_temp_forecast(lat=h_lat, lon=h_lon, target_date=now)
                        req_runtime_today = calculate_required_runtime(forecast_today, current_temp=curr_temp)
                        
                        ai_res_today = get_ai_optimized_parameters(
                            prices_today, 
                            forecast_today, 
                            current_temp=curr_temp,
                            target_temp=house_target_temp,
                            default_price_cap=default_p_cap, 
                            required_runtime_hours=req_runtime_today,
                            target_day_label=f"idag ({h_name})"
                        )
                        
                        sched_today = generate_daily_schedule(
                            prices_today, 
                            temp_hourly=forecast_today, 
                            current_temp=curr_temp, 
                            price_cap=ai_res_today["price_cap"],
                            required_runtime_hours=req_runtime_today,
                            max_consecutive_off_hours=ai_res_today["max_consecutive_off_hours"]
                        )

                        # Skriv över dagens schema och spara både priser och väderprognos i Gist
                        if sched_today and len(sched_today) == 96:
                            h_data.update({
                                "schedule_date": today_str, 
                                "daily_schedule": sched_today, 
                                "prices_today": prices_today,
                                "last_outside_temp": round(curr_temp, 1),
                                "temp_forecast_today": forecast_today,
                                "ai_latest_reasoning": ai_res_today["motivering"], 
                                "last_ai_update": now.strftime("%Y-%m-%d %H:%M"),
                                "last_ai_attempt": now.strftime("%Y-%m-%d %H:%M")
                            })
                            if devs:
                                ref["price_cap"] = ai_res_today["price_cap"]
                            any_updated = True

                        # --- 2. BERÄKNA IMORGON (om Nordpool-priserna har släppts) ---
                        prices_tom = get_nordpool_price(zone=h_zone, target_date=tomorrow_dt)
                        if prices_tom and len(set(prices_tom)) > 1:
                            time.sleep(4)
                            forecast_tom = get_hourly_temp_forecast(lat=h_lat, lon=h_lon, target_date=tomorrow_dt)
                            req_runtime_tom = calculate_required_runtime(forecast_tom, current_temp=curr_temp)

                            ai_res_tom = get_ai_optimized_parameters(
                                prices_tom, 
                                forecast_tom, 
                                current_temp=curr_temp,
                                target_temp=house_target_temp,
                                default_price_cap=default_p_cap, 
                                required_runtime_hours=req_runtime_tom,
                                target_day_label=f"imorgon ({h_name})"
                            )

                            sched_tom = generate_daily_schedule(
                                prices_tom, 
                                temp_hourly=forecast_tom, 
                                current_temp=curr_temp, 
                                price_cap=ai_res_tom["price_cap"],
                                required_runtime_hours=req_runtime_tom,
                                max_consecutive_off_hours=ai_res_tom["max_consecutive_off_hours"]
                            )

                            # Skriv över morgondagens schema och spara både priser och prognos i Gist
                            if sched_tom and len(sched_tom) == 96:
                                h_data.update({
                                    "tomorrow_schedule_date": tomorrow_str,
                                    "tomorrow_schedule": sched_tom,
                                    "prices_tomorrow": prices_tom,
                                    "tomorrow_temp_forecast": forecast_tom,
                                    "tomorrow_ai_reasoning": ai_res_tom["motivering"]
                                })
                                any_updated = True

                    except Exception as e:
                        st.error(f"Kunde inte uppdatera {h_name} (behåller föregående schema som fallback): {e}")

                if any_updated:
                    save_config(config)
                    load_gist_data.clear()
                    st.success("Nya PuLP-scheman, priser och väderprognoser (idag & imorgon) beräknade och sparade!")
                    time.sleep(1)
                    st.rerun()

    if st.button("🚪 Logga ut"):
        st.session_state["authenticated"] = False
        st.session_state["user_role"] = None
        st.session_state["assigned_house_key"] = None
        st.rerun()

st.title(f"⚡ {current_house.get('name') if current_house else 'Elstyrning'}")


if is_admin:
    tab_dash, tab_settings, tab_sched, tab_admin = st.tabs(["🏠 Dashboard", "⚙️ Enhetsinställningar", "📅 Schema & Priser", "🛠️ System Admin"])
else:
    tab_dash, tab_settings, tab_sched = st.tabs(["🏠 Dashboard", "⚙️ Enhetsinställningar", "📅 Schema & Priser"])
    tab_admin = None

# --- CACHADE HJÄLPFUNKTIONER FÖR SNABB SIDLADDNING ---
@st.cache_data(ttl=3600)
def cached_nordpool_price(zone: str, date_str: str):
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=sweden_tz)
    return get_nordpool_price(zone=zone, target_date=dt)

@st.cache_data(ttl=30)
def safe_tuya_status(d_id, d_type):
    if not d_id:
        return False, "Ej konfigurerad", None, None
    try:
        res = get_tuya_device_status(d_id, device_type=d_type, include_battery=True)
    except TypeError:
        res = get_tuya_device_status(d_id, device_type=d_type)
    if isinstance(res, tuple) and len(res) == 4:
        return res
    elif isinstance(res, tuple) and len(res) == 3:
        return res[0], res[1], res[2], None
    return False, "OFF", None, None

# --- FUNKTION FÖR ATT RITA GRAF ---
def render_el_chart(times_ext, prices_ext, temp_ext, schedule_data, is_thermostat, current_device, chart_key, history_df=None):
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    
    # 1. Elpriskurva (Vänster axel - yaxis1)
    fig.add_trace(
        go.Scatter(
            x=times_ext, 
            y=prices_ext, 
            name="Elpris (öre/kWh)", 
            line=dict(color="#29b6f6", width=2, shape="hv")
        ),
        secondary_y=False
    )

    # 2. Utetemperaturkurva (Höger axel - yaxis2)
    fig.add_trace(
        go.Scatter(
            x=times_ext, 
            y=temp_ext, 
            name="Utetemperatur (°C)", 
            line=dict(color="#ffa726", width=2, dash="dot", shape="hv")
        ),
        secondary_y=True
    )

    # 3. Faktisk Rumstemperatur från historik (mappad mot de 96 kvartarna)
    if history_df is not None and not history_df.empty:
        temp_col = 'room_temp' if 'room_temp' in history_df.columns else ('device_temp' if 'device_temp' in history_df.columns else None)
        if temp_col:
            room_temps_96 = [None] * 96
            for _, row in history_df.iterrows():
                val = row[temp_col]
                if pd.notnull(val) and val is not None:
                    try:
                        time_part = row['timestamp'].split()[1]
                        h, m = map(int, time_part.split(':'))
                        q_idx = (h * 4) + (m // 15)
                        if 0 <= q_idx < 96:
                            room_temps_96[q_idx] = float(val)
                    except Exception:
                        pass
            
            room_temps_ext = room_temps_96 + [room_temps_96[-1] if room_temps_96[-1] is not None else None]
            
            if any(v is not None for v in room_temps_ext):
                fig.add_trace(
                    go.Scatter(
                        x=times_ext,
                        y=room_temps_ext,
                        name="Faktisk Rumstemp (°C)",
                        mode="lines+markers",
                        line=dict(color="#ab47bc", width=2),
                        connectgaps=True
                    ),
                    secondary_y=True
                )

    # 4. Statuspluppar och övergångslinjer
    if current_device:
        if not schedule_data or not isinstance(schedule_data, list) or len(schedule_data) < 96:
            schedule_data = [False] * 96

        if is_thermostat:
            t_high = float(current_device.get("target_temp_high", 10.0))
            t_low = float(current_device.get("target_temp_low", 6.0))
            vals = [t_high if s else t_low for s in schedule_data[:96]]
            vals_ext = vals + [vals[-1]]
            fig.add_trace(
                go.Scatter(
                    x=times_ext, y=vals_ext, name="Måltemp (°C)", 
                    mode="lines+markers", line=dict(shape="hv", color="#2ecc71")
                ),
                secondary_y=True
            )
        else:
            on_vals = [None] * len(times_ext)
            off_vals = [None] * len(times_ext)
            trans_x = []
            trans_y = []

            for i in range(len(times_ext)):
                is_on_current = (i < 96 and schedule_data[i])
                is_on_prev = (i > 0 and schedule_data[min(i-1, 95)])
                
                # Ritar plupp vid kvartens start OCH vid kvartens slut (övergången)
                if is_on_current or is_on_prev:
                    on_vals[i] = 1.0
                if (i < 96 and not schedule_data[i]) or (i > 0 and not schedule_data[min(i-1, 95)]):
                    off_vals[i] = 0.0

                if i > 0 and (schedule_data[min(i, 95)] != schedule_data[min(i-1, 95)]):
                    trans_x.extend([times_ext[i], times_ext[i], None])
                    trans_y.extend([0.0, 1.0, None])

            fig.add_trace(
                go.Scatter(
                    x=trans_x, y=trans_y, name="Övergång", mode="lines",
                    line=dict(color="rgba(255, 75, 75, 0.5)", width=1.5, dash="dot"),
                    hoverinfo="skip", yaxis="y3"
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=times_ext, y=on_vals, name="PÅ 🟢", mode="markers",
                    marker=dict(size=9, color="#2ecc71", symbol="circle"), yaxis="y3"
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=times_ext, y=off_vals, name="AV 🔴", mode="markers",
                    marker=dict(size=9, color="#e74c3c", symbol="circle"), yaxis="y3"
                )
            )

    fig.update_layout(
        hovermode="x unified", 
        height=420, 
        margin=dict(l=10, r=10, t=30, b=10),
        yaxis2=dict(
            title="Temperatur (°C)",
            anchor="x",
            overlaying="y",
            side="right",
            showgrid=False,
            tickformat=".0f"
        ),
        yaxis3=dict(
            title="",
            anchor="x",
            overlaying="y",
            side="right",
            range=[-0.05, 1.05],
            showticklabels=False,
            showgrid=False
        )
    )
    
    fig.update_yaxes(title_text="Elpris (öre/kWh)", secondary_y=False)
    st.plotly_chart(fig, width="stretch", key=chart_key)

# --- TAB 1: DASHBOARD ---
with tab_dash:
    offline_devices = []
    if current_house:
        for dev in current_house.get("devices", []):
            if dev.get("device_id"):
                tuya_conn, _, _, _ = safe_tuya_status(dev.get("device_id"), dev.get("type", "switch"))
                if not tuya_conn:
                    offline_devices.append(dev.get("name"))

    if offline_devices:
        st.error(f"⚠️ **Varning:** Ingen kontakt med följande Tuya-enhet(er): **{', '.join(offline_devices)}**. Kontrollera nätverk eller strömförsörjning!")

    st.subheader("📍 Realtidsöversikt & Ekonomi")
    
    now = datetime.now(sweden_tz)
    today_str = now.strftime("%Y-%m-%d")
    house_zone = current_house.get("zone", "SE2") if current_house else "SE2"
    
    # Läs sparade priser direkt från Gist (eller ramminnes-cachen om nytt dygn/ny zon)
    today_prices = current_house.get("prices_today") if (current_house and current_house.get("schedule_date") == today_str) else None
    if not today_prices or len(today_prices) != 96:
        today_prices = cached_nordpool_price(house_zone, today_str)
    
    # Läs senast sparad utetemperatur direkt från Gist (eller historiken) utan att anropa externt API
    outside_temp = current_house.get("last_outside_temp") if current_house else None
    if outside_temp is None and history and current_house:
        for h_item in reversed(history):
            if h_item.get("house") == current_house.get("name") and h_item.get("temp") is not None:
                outside_temp = float(h_item.get("temp"))
                break
    if outside_temp is None and current_house:
        tf_saved = current_house.get("temp_forecast_today")
        outside_temp = float(tf_saved[now.hour]) if (tf_saved and len(tf_saved) == 24) else 0.0
    elif outside_temp is None:
        outside_temp = 0.0
    
    avg_day_price = 0.0
    active_saving_pct = 0.0
    avg_active_price = 0.0
    
    if today_prices and len(today_prices) > 0:
        avg_day_price = sum(today_prices) / len(today_prices)
        
        if current_house and current_house.get("daily_schedule"):
            sched = current_house["daily_schedule"]
            if isinstance(sched, list) and len(sched) > 0:
                active_prices = [today_prices[i] for i in range(min(len(today_prices), len(sched))) if sched[i]]
            else:
                active_prices = []
            if active_prices:
                avg_active_price = sum(active_prices) / len(active_prices)
                if avg_day_price > 0:
                    active_saving_pct = max(0.0, ((avg_day_price - avg_active_price) / avg_day_price) * 100.0)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Utetemperatur", f"{outside_temp:.1f} °C")
    c2.metric("Elpris snitt (idag)", f"{avg_day_price:.1f} öre/kWh")
    c3.metric("Planerat inköpspris", f"{avg_active_price:.1f} öre/kWh")
    c4.metric("Planerad besparing", f"{active_saving_pct:.1f} %")

    st.divider()
    st.markdown("### 📈 Dagens Elpris, Utetemperatur & Driftstatus (Kvart för Kvart)")
    
    if current_house and today_prices and len(today_prices) == 96:
        schedule_today = current_house.get("daily_schedule", [False]*96)
        
        # Läs sparad 24h-prognos direkt från Gist (inga externa API-anrop vid sidladdning/husbyte)
        temp_forecast_today = current_house.get("temp_forecast_today")
        if not temp_forecast_today or len(temp_forecast_today) < 24:
            temp_forecast_today = [outside_temp] * 24
        
        temp_96_today = []
        for t_val in temp_forecast_today:
            temp_96_today.extend([t_val] * 4)
        while len(temp_96_today) < 96:
            temp_96_today.append(temp_96_today[-1] if temp_96_today else 0.0)

        times_96 = [f"{h:02d}:{m:02d}" for h in range(24) for m in [0, 15, 30, 45]]
        times_ext = times_96 + ["24:00"]
        prices_ext = list(today_prices) + [today_prices[-1]]
        temp_ext = list(temp_96_today) + [temp_96_today[-1]]

        is_therm = current_device.get("type") == "thermostat" if current_device else False

        device_history_df = pd.DataFrame()
        if history and current_house and current_device:
            df_h = pd.DataFrame(history)
            if 'house' in df_h.columns and 'device' in df_h.columns:
                device_history_df = df_h[(df_h['house'] == current_house.get('name')) & 
                                         (df_h['device'] == current_device.get('name')) & 
                                         (df_h['timestamp'].str.startswith(today_str))]

        render_el_chart(times_ext, prices_ext, temp_ext, schedule_today, is_therm, current_device, "chart_dashboard", history_df=device_history_df)

    st.divider()
    st.markdown("### 🎛️ Husets Enheter")
    
    if current_house:
        devs = current_house.get("devices", [])
        if devs:
            dev_cols = st.columns(len(devs) if len(devs) <= 3 else 3)
            for idx, dev in enumerate(devs):
                tuya_conn, tuya_msg, dev_temp, dev_batt = safe_tuya_status(
                    dev.get("device_id"), 
                    dev.get("type", "switch")
                )

                thermo_id = dev.get("thermometer_device_id")
                thermo_conn, _, room_temp, thermo_batt = safe_tuya_status(
                    thermo_id, 
                    "thermostat"
                ) if thermo_id else (False, "", None, None)

                # Hjälpfunktion för att formatera batteritext med ikon
                def format_battery(batt_val):
                    if batt_val is None:
                        return ""
                    if isinstance(batt_val, int):
                        icon = "🪫" if batt_val <= 20 else "🔋"
                        warn = " ⚠️" if batt_val <= 20 else ""
                        return f" | {icon} **Batteri:** `{batt_val}%`{warn}"
                    return f" | 🔋 **Batteri:** `{batt_val}`"

                with dev_cols[idx % 3]:
                    if not tuya_conn:
                        st.markdown(
                            f"""
                            <div style="border: 2px solid #ff4b4b; border-radius: 8px; padding: 15px; background-color: rgba(255, 75, 75, 0.05);">
                                <h4>🔌 {dev.get('name')} <span style="color: #ff4b4b; font-size: 0.8em;">(OFFLINE ⚠️)</span></h4>
                                <p style="margin: 0; color: #ff4b4b;"><b>Ingen kontakt med Tuya Cloud!</b></p>
                            </div>
                            """,
                            unsafe_allow_html=True
                        )
                        st.markdown("<br>", unsafe_allow_html=True)
                    else:
                        with st.container(border=True):
                            st.markdown(f"#### 🔌 {dev.get('name')}")
                            is_term = dev.get("type") == "thermostat"
                            st.caption(f"Typ: {'Termostat' if is_term else 'Relä / Switch'}")
                            
                            st.markdown(f"**Status:** `{tuya_msg}`")
                            
                            if is_term and dev_temp is not None:
                                st.markdown(f"🌡️ **Givartemp:** `{dev_temp:.1f} °C`{format_battery(dev_batt)}")

                            elif thermo_id:
                                if thermo_conn and room_temp is not None:
                                    st.markdown(
                                        f"🌡️ **Rumstemp:** `{room_temp:.1f} °C` "
                                        f"(Mål: {dev.get('target_room_temp', 20.0)}°C)"
                                        f"{format_battery(thermo_batt)}"
                                    )
                                elif thermo_conn:
                                    st.markdown(
                                        f"🌡️ **Rumstemp:** `Ansluten men ingen temperatur hittades ⚠️`{format_battery(thermo_batt)}"
                                    )
                                else:
                                    st.markdown(
                                        "🌡️ **Rumstemp:** `OFFLINE ⚠️`"
                                    )

                            else:
                                st.markdown(
                                    f"**Måltemp:** `{dev.get('target_room_temp', 20.0)} °C`"
                                )

                            st.markdown(
                                f"**Driftläge:** `{dev.get('force_override', 'AUTO')}`"
                            )

    st.divider()
    with st.expander("📊 Visa rådata / Historiklogg per kvart"):
        if history and current_house and current_device:
            df = pd.DataFrame(history)
            if 'house' in df.columns and 'device' in df.columns:
                df = df[(df['house'] == current_house.get('name')) & (df['device'] == current_device.get('name'))]
            if not df.empty:
                cols_to_show = [c for c in ['timestamp', 'price', 'status', 'temp', 'room_temp', 'device_temp', 'reason'] if c in df.columns]
                st.dataframe(df[cols_to_show].sort_values(by='timestamp', ascending=False), width="stretch")
            else:
                st.info("Ingen historik för denna enhet ännu.")
        else:
            st.info("Ingen historik registrerad ännu.")

# --- TAB 2: ENHETSINSTÄLLNINGAR & HUSETS INSTÄLLNINGAR ---
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
            st.info("🤖 **Automatisk optimering & rumsklimat:** Systemet kombinerar prisoptimeringen med önskad rumstemperatur.")
            target_r_temp = st.number_input("Önskad rumstemperatur [°C]", min_value=5.0, max_value=30.0, value=float(current_device.get("target_room_temp", 20.0)), step=0.5)
            thermo_id_input = st.text_input("Tuya Device ID för separat rumstermometer (Valfritt)", value=current_device.get("thermometer_device_id", ""))

        force = st.radio("Driftläge", ["AUTO", "PÅ (Tvinga igång)", "AV (Stäng av allt)"], index=["AUTO", "PÅ (Tvinga igång)", "AV (Stäng av allt)"].index(current_device.get("force_override", "AUTO")))

        if st.button("💾 Spara enhetsinställningar", type="primary"):
            if is_thermostat:
                current_device.update({"target_temp_high": t_high, "target_temp_low": t_low, "force_override": force})
            else:
                current_device.update({
                    "target_room_temp": target_r_temp, 
                    "thermometer_device_id": thermo_id_input.strip(),
                    "force_override": force
                })
            save_config(config)

            dev_id = current_device.get("device_id")
            if dev_id:
                if is_thermostat:
                    target_val = t_high if force == "PÅ (Tvinga igång)" else (t_low if force == "AV (Stäng av allt)" else t_low)
                    set_tuya_device(dev_id, target_val, device_type="thermostat")
                else:
                    switch_val = True if force == "PÅ (Tvinga igång)" else (False if force == "AV (Stäng av allt)" else False)
                    if force != "AUTO":
                        set_tuya_device(dev_id, switch_val, device_type="switch")

            load_gist_data.clear()
            st.success("Inställningarna sparades och skickades till enheten!")
            time.sleep(1)
            st.rerun()

    st.divider()
    st.subheader("🏠 Husets inställningar & Paus")
    
    if current_house and selected_house_key:
        pause_key_settings = f"pause_house_{selected_house_key}"
        pause_key_admin = f"admin_pause_{selected_house_key}"

        paused_state = st.checkbox(
            "Pausa huset helt (hoppa över optimering, AI och styrning)", 
            value=current_house.get("paused", False), 
            key=pause_key_settings
        )
        if paused_state != current_house.get("paused", False):
            current_house["paused"] = paused_state
            if pause_key_admin in st.session_state:
                st.session_state[pause_key_admin] = paused_state
            save_config(config)
            load_gist_data.clear()
            time.sleep(0.5)
            st.rerun()

    st.markdown("<br>", unsafe_allow_html=True)
    new_h_pw_input = st.text_input("Nytt lösenord för huset", type="password")
    if st.button("Uppdatera husets lösenord"):
        if new_h_pw_input:
            current_house["password"] = new_h_pw_input
            save_config(config)
            load_gist_data.clear()
            st.success("Lösenordet har uppdaterats!")

# --- TAB 3: SCHEMA & PRISER ---
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
        target_date_str = today_str if not is_tomorrow else tomorrow_str
        
        if current_house:
            if not is_tomorrow and current_house.get("ai_latest_reasoning"):
                st.info(f"🤖 **AI-Strategens analys (Idag):** {current_house.get('ai_latest_reasoning')}")
            elif is_tomorrow and current_house.get("tomorrow_ai_reasoning"):
                st.info(f"🤖 **AI-Strategens analys (Imorgon):** {current_house.get('tomorrow_ai_reasoning')}")
        
        house_zone = current_house.get("zone", "SE2")
        
        # Läs sparade priser direkt från Gist (eller hämta 1 gång via cachen om de saknas)
        if not is_tomorrow:
            prices = current_house.get("prices_today") if current_house.get("schedule_date") == today_str else None
        else:
            prices = current_house.get("prices_tomorrow") if current_house.get("tomorrow_schedule_date") == tomorrow_str else None
            
        if not prices or len(prices) != 96:
            prices = cached_nordpool_price(house_zone, target_date_str)
        
        prices_has_arrived = True
        if is_tomorrow:
            if not prices or len(set(prices)) <= 1:
                prices_has_arrived = False

        if is_tomorrow and not prices_has_arrived:
            st.info(f"⏳ **Morgondagens elprisprognos har inte publicerats ännu.**\n\nNordpool brukar publicera morgondagens priser dagligen runt kl. **13:00–14:00**. Vänligen återkom efter det för att se schemat för {tomorrow_str}.")
        else:
            # Läs sparad prognos direkt från Gist
            if not is_tomorrow:
                temp_forecast = current_house.get("temp_forecast_today")
            else:
                temp_forecast = current_house.get("tomorrow_temp_forecast")

            if not temp_forecast or len(temp_forecast) < 24:
                fallback_t = current_house.get("last_outside_temp", 0.0) if current_house else 0.0
                temp_forecast = [fallback_t if fallback_t is not None else 0.0] * 24
            
            temp_96 = []
            for t_val in temp_forecast:
                temp_96.extend([t_val] * 4)
            while len(temp_96) < 96:
                temp_96.append(temp_96[-1] if temp_96 else 0.0)

            if not is_tomorrow:
                schedule = current_house.get("daily_schedule", [False]*96)
            else:
                schedule = current_house.get("tomorrow_schedule")
                if not schedule or current_house.get("tomorrow_schedule_date") != tomorrow_str:
                    curr_temp = current_house.get("last_outside_temp")
                    if curr_temp is None:
                        curr_temp = get_outside_temp(station=current_house.get("temperatur_nu_station"), lat=current_house.get("lat", 63.25), lon=current_house.get("lon", 14.56))
                    temp_forecast = get_hourly_temp_forecast(lat=current_house.get("lat", 63.25), lon=current_house.get("lon", 14.56), target_date=tomorrow_dt)
                    req_runtime_tom = calculate_required_runtime(temp_forecast, current_temp=curr_temp)
                    schedule = generate_daily_schedule(
                        prices, 
                        temp_hourly=temp_forecast, 
                        current_temp=curr_temp, 
                        price_cap=50.0,
                        required_runtime_hours=req_runtime_tom
                    )
                    
                    current_house["tomorrow_schedule"] = schedule
                    current_house["tomorrow_schedule_date"] = tomorrow_str
                    current_house["prices_tomorrow"] = prices
                    current_house["tomorrow_temp_forecast"] = temp_forecast
                    save_config(config)
                    load_gist_data.clear()

            times = [f"{h:02d}:{m:02d}" for h in range(24) for m in [0, 15, 30, 45]]
            times_extended = times + ["24:00"]
            prices_extended = list(prices[:len(times)]) + [prices[len(times)-1] if len(prices) >= len(times) else 0]
            temp_extended = list(temp_96[:len(times)]) + [temp_96[len(times)-1] if len(temp_96) >= len(times) else 0]

            render_el_chart(times_extended, prices_extended, temp_extended, schedule, is_thermostat, current_device, "chart_schedule")

            sched_avg = sum(prices[:96]) / len(prices[:96]) if prices else 0.0
            if prices and schedule:
                active_p_sched = [
                    prices[i]
                    for i in range(min(len(prices), len(schedule)))
                    if schedule[i]
                ]

                sched_active_avg = (
                    sum(active_p_sched) / len(active_p_sched)
                    if active_p_sched else sched_avg
                )
            else:
                active_p_sched = []
                sched_active_avg = sched_avg            
                
            sched_savings_pct = max(0.0, ((sched_avg - sched_active_avg) / sched_avg) * 100.0) if sched_avg > 0 else 0.0

            m1, m2, m3 = st.columns(3)
            m1.metric("Dagens medelpris (El)", f"{sched_avg:.2f} öre/kWh")
            m2.metric("Planerat inköpspris", f"{sched_active_avg:.2f} öre/kWh")
            m3.metric("Sparat mot medelpris", f"{sched_savings_pct:.1f} %")

# --- TAB 4: SYSTEM ADMIN ---
if is_admin and tab_admin:
    with tab_admin:
        st.subheader("🛠️ Systemadministration & Lösenordshantering")
        
        st.write("### 🔑 Hantera Lösenord")
        admin_pw_input = st.text_input("Ändra Admin-lösenord", value=config.get("admin_password", "admin123"))
        if st.button("💾 Uppdatera Admin-lösenord"):
            config["admin_password"] = admin_pw_input
            save_config(config)
            load_gist_data.clear()
            st.success("Admin-lösenordet har uppdaterats!")

        st.divider()
        st.write("### 🏠 Husinställningar, Elområde & Pausläge")
        zones_list = ["SE1", "SE2", "SE3", "SE4"]
        for h_k, h_v in houses.items():
            col_a, col_b, col_z, col_c = st.columns([2, 2, 1.5, 1])
            with col_a:
                st.write(f"**{h_v.get('name', h_k)}** {'⏸️' if h_v.get('paused') else ''}")
            with col_b:
                new_h_pw = st.text_input(f"Lösenord för {h_v.get('name')}", value=h_v.get("password", "hus123"), key=f"pw_{h_k}")
                if new_h_pw != h_v.get("password"):
                    h_v["password"] = new_h_pw
                    save_config(config)
                    load_gist_data.clear()
                    st.success(f"Lösenord för {h_v.get('name')} sparades!")
            with col_z:
                curr_z = h_v.get("zone", "SE2")
                z_idx = zones_list.index(curr_z) if curr_z in zones_list else 1
                new_h_zone = st.selectbox(f"Zon ({h_v.get('name')})", zones_list, index=z_idx, key=f"zone_{h_k}")
                if new_h_zone != curr_z:
                    h_v["zone"] = new_h_zone
                    save_config(config)
                    load_gist_data.clear()
                    st.success(f"Elområde ändrat till {new_h_zone}!")
                    time.sleep(0.5)
                    st.rerun()
            with col_c:
                admin_paused = st.checkbox("Pausat", value=h_v.get("paused", False), key=f"admin_pause_{h_k}")
                if admin_paused != h_v.get("paused", False):
                    h_v["paused"] = admin_paused
                    settings_key = f"pause_house_{h_k}"
                    if settings_key in st.session_state:
                        st.session_state[settings_key] = admin_paused
                    save_config(config)
                    load_gist_data.clear()
                    time.sleep(0.5)
                    st.rerun()

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
                    "paused": False,
                    "devices": []
                }
                save_config(config)
                load_gist_data.clear()
                st.success(f"Huset '{new_h_name}' har skapats!")
                time.sleep(0.5)
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
                        "target_room_temp": 20.0,
                        "thermometer_device_id": "",
                        "force_override": "AUTO"
                    }
                    if dev_type == "thermostat":
                        new_dev.update({"target_temp_high": 10.0, "target_temp_low": 6.0})
                    current_house.setdefault("devices", []).append(new_dev)
                    save_config(config)
                    load_gist_data.clear()
                    st.success(f"Enhet '{dev_name}' tillagd!")
                    time.sleep(0.5)
                    st.rerun()

        st.divider()
        st.write("### 🌡️ Koppla termometer till switch")
        st.caption("Välj hus och switch för att koppla en separat Tuya-termometer samt ställa in måltemperatur.")
        
        if houses:
            admin_house_key = st.selectbox(
                "Välj hus för koppling", 
                list(houses.keys()), 
                format_func=lambda k: houses[k].get("name", k), 
                key="admin_link_house"
            )
            target_house = houses[admin_house_key]
            house_devs = target_house.get("devices", [])
            
            switch_devs = [d for d in house_devs if d.get("type", "switch") == "switch"]
            
            if switch_devs:
                switch_names = {d.get("id"): d.get("name") for d in switch_devs}
                selected_sw_id = st.selectbox(
                    "Välj switch", 
                    list(switch_names.keys()), 
                    format_func=lambda sid: switch_names[sid], 
                    key="admin_link_switch"
                )
                
                selected_dev = next((d for d in house_devs if d.get("id") == selected_sw_id), None)
                
                if selected_dev:
                    current_thermo = selected_dev.get("thermometer_device_id", "")
                    current_target = float(selected_dev.get("target_room_temp", 20.0))
                    
                    with st.form("link_thermometer_form"):
                        new_thermo_id = st.text_input(
                            "Tuya Device ID för rumstermometer", 
                            value=current_thermo, 
                            placeholder="T.ex. eb83d... eller liknande Tuya ID"
                        )
                        new_target_temp = st.number_input(
                            "Önskad rumstemperatur [°C]", 
                            min_value=5.0, 
                            max_value=30.0, 
                            value=current_target, 
                            step=0.5
                        )
                        
                        if st.form_submit_button("💾 Spara termometerkoppling", type="primary"):
                            selected_dev["thermometer_device_id"] = new_thermo_id.strip()
                            selected_dev["target_room_temp"] = new_target_temp
                            save_config(config)
                            load_gist_data.clear()
                            st.success(f"Termometer kopplad till '{selected_dev.get('name')}'!")
                            time.sleep(0.5)
                            st.rerun()
            else:
                st.info("Det finns inga switch-enheter inlagda i det här huset än. Lägg till en enhet i sektionen ovanför först.")