import streamlit as st
import os
import requests
from zoneinfo import ZoneInfo
sweden_tz = ZoneInfo("Europe/Stockholm")
import json
from datetime import datetime, timedelta
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

from main import (
    get_tuya_device_status,
    main, 
    generate_daily_schedule, 
    get_nordpool_price, 
    get_outside_temp, 
    get_hourly_temp_forecast, 
    get_ai_optimized_parameters
)

main()

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
        "min_on_blocks": 44,
        "max_off_blocks": 12,
        "price_cap": 50.0,
        "cold_temp_threshold": 0,
        "min_minutes_cold": 15,
        "force_override": "AUTO"
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
        st.success("Inställningarna sparades!")
    else:
        st.error(f"Ett fel uppstod: {res.status_code}")

config, history = load_gist_data()

st.title("⚡ Elstyrning & Övervakning")

if config.get("ai_latest_reasoning"):
    st.info(f"🤖 **AI-Strategens analys:** {config.get('ai_latest_reasoning')}")

tab1, tab2, tab3 = st.tabs(["📊 Övervakning & Graf", "⚙️ Inställningar", "📅 Schema & Priser"])

with st.sidebar:
    st.write("### Kontroller")
    if st.button("🔄 Uppdatera sida"):
        st.rerun()
# --- TAB 1: HISTORIK OCH GRAFER ---
with tab1:
    st.subheader("Senaste dygnets pris, temperatur och elementstatus")
    
    if history:
        df = pd.DataFrame(history)
        df['time'] = pd.to_datetime(df['timestamp'])
        df['status_num'] = df['status'].apply(lambda x: 1 if x == 'ON' else 0)
        
        latest = df.iloc[-1]
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Aktuellt pris", f"{latest.get('price', 0):.2f} öre/kWh")
        col2.metric("Elementstatus", "PÅ 🟢" if latest.get('status') == 'ON' else "AV 🔴")
        col3.metric("Utetemperatur", f"{latest.get('temp', 0):.1f} °C")
        col4.metric("Senast uppdaterad", latest['time'].strftime('%H:%M'))

        # Skapa graf med dubbla y-axlar (vänster: pris, höger: status) och overlay för temperatur
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        status_colors = df['status'].map({'ON': '#2ecc71', 'OFF': '#e74c3c'})
        
        # 1. Status (höger axel)
        fig.add_trace(
            go.Scatter(
                x=df['time'], y=df['status_num'],
                name="Elementstatus", mode="lines+markers",
                line=dict(shape="hv", color="rgba(150, 150, 150, 0.3)", width=2),
                marker=dict(size=8, color=status_colors, symbol="circle"),
                hovertemplate="Tid: %{x|%H:%M}<br>Status: %{customdata}<extra></extra>",
                customdata=df['status']
            ),
            secondary_y=True
        )

        # 2. Elpris (vänster axel)
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

        # 3. Utetemperatur (vänster axel med egen linje)
        fig.add_trace(
            go.Scatter(
                x=df['time'], y=df['temp'],
                name="Utetemperatur (°C)", mode="lines",
                line=dict(color="#ffa726", width=2, dash="dot"),
                hovertemplate="Tid: %{x|%H:%M}<br>Temp: %{y:.1f} °C<extra></extra>"
            ),
            secondary_y=False
        )

        fig.update_layout(
            hovermode="x unified",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(l=10, r=10, t=30, b=10),
            height=440
        )

        fig.update_yaxes(title_text="Elpris (öre/kWh) & Temp (°C)", secondary_y=False)
        fig.update_yaxes(
            title_text="Status", tickvals=[0, 1],
            ticktext=["AV 🔴", "PÅ 🟢"], range=[-0.2, 1.2], secondary_y=True
        )

        st.plotly_chart(fig, width='stretch')
        with st.expander("Visa rådata / logg"):
            st.dataframe(df[['timestamp', 'price', 'status', 'temp']].sort_values(by='timestamp', ascending=False), width='stretch')
    else:
        st.info("Ingen historik har loggats än.")

  
    st.header("📊 Övervakning & Status")
    
    # Hämta och visa Tuya-status
    tuya_connected, tuya_msg = get_tuya_device_status()
    
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Aktivt Pristak", f"{config.get('price_cap', 50):.1f} öre")
    with col2:
        st.metric("Utetemperatur", f"{get_outside_temp():.1f} °C")
    with col3:
        st.metric("Tuya Relästatus", tuya_msg)

    if not tuya_connected:
        st.warning(f"Tuya-status: {tuya_msg}")

# --- TAB 2: INSTÄLLNINGAR ---
with tab2:
    st.subheader("1. Optimeringsparametrar & AI")
    
    min_on_hours = st.slider("Minsta totala körtid per dygn (timmar)", min_value=4, max_value=24, value=int(config.get("min_on_blocks", 44) / 4), step=1)
    max_off_hours = st.slider("Maximal avstängning i rad (timmar)", min_value=1, max_value=6, value=int(config.get("max_off_blocks", 12) / 4), step=1)
    st.divider()
    st.subheader("2. Köldskydd & Utetemperatur")
    cold_temp_threshold = st.number_input("Gräns för kyla (°C)", min_value=-30, max_value=10, value=config.get("cold_temp_threshold", 0))
    min_minutes_cold = st.select_slider("Minst antal minuter körtid per timme vid kyla", options=[0, 15, 30, 45, 60], value=config.get("min_minutes_cold", 15))

    st.divider()
    st.subheader("3. Manuell överstyrning")
    force_override = st.radio("Driftläge", options=["AUTO", "PÅ (Tvinga igång)", "AV (Stäng av allt)"], index=["AUTO", "PÅ (Tvinga igång)", "AV (Stäng av allt)"].index(config.get("force_override", "AUTO")))

    if st.button("💾 Spara inställningar", type="primary"):
        new_config = config.copy()
        new_config.update({
            "min_on_blocks": min_on_hours * 4,
            "max_off_blocks": max_off_hours * 4,
            "cold_temp_threshold": cold_temp_threshold,
            "min_minutes_cold": min_minutes_cold,
            "force_override": force_override
        })

        try:
            now = datetime.now(sweden_tz)
            today_str = now.strftime("%Y-%m-%d")
            tomorrow_dt = now + timedelta(days=1)
            tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")
            current_temp = get_outside_temp()

            prices_today = get_nordpool_price(zone="SE2", target_date=now)
            prices_tomorrow = get_nordpool_price(zone="SE2", target_date=tomorrow_dt)
            temp_forecast = get_hourly_temp_forecast(target_date=now)

            p_cap_today, m_blocks_today, reasoning_today = get_ai_optimized_parameters(
                prices_today, temp_forecast, default_price_cap=50.0, default_min_on_blocks=new_config["min_on_blocks"], target_day_label="idag"
            )
            p_cap_tom, m_blocks_tom, reasoning_tom = get_ai_optimized_parameters(
                prices_tomorrow, temp_forecast, default_price_cap=50.0, default_min_on_blocks=new_config["min_on_blocks"], target_day_label="imorgon"
            )

            new_config["schedule_date"] = today_str
            new_config["daily_schedule"] = generate_daily_schedule(prices_today, max_off_blocks=new_config["max_off_blocks"], min_on_blocks=m_blocks_today, current_temp=current_temp, cold_threshold=cold_temp_threshold, min_minutes_cold=min_minutes_cold, price_cap=p_cap_today)
            new_config["tomorrow_schedule_date"] = tomorrow_str
            new_config["tomorrow_schedule"] = generate_daily_schedule(prices_tomorrow, max_off_blocks=new_config["max_off_blocks"], min_on_blocks=m_blocks_tom, current_temp=current_temp, cold_threshold=cold_temp_threshold, min_minutes_cold=min_minutes_cold, price_cap=p_cap_tom)
            new_config["price_cap"] = p_cap_today
            new_config["min_on_blocks"] = m_blocks_today
            new_config["ai_latest_reasoning"] = reasoning_today
        except Exception as e:
            st.warning(f"Kunde inte räkna om med AI direkt: {e}")

        save_config(new_config)
        st.cache_data.clear()
        st.rerun()

    st.divider()
    if st.button("🔄 Forcera omräkning med AI nu"):
        with st.spinner("Hämtar priser och väderprognos för idag och imorgon..."):
            try:
                now = datetime.now(sweden_tz)
                today_str = now.strftime("%Y-%m-%d")
                tomorrow_dt = now + timedelta(days=1)
                tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")
                current_temp = get_outside_temp()

                prices_today = get_nordpool_price(zone="SE2", target_date=now)
                prices_tomorrow = get_nordpool_price(zone="SE2", target_date=tomorrow_dt)
                temp_forecast = get_hourly_temp_forecast(target_date=now)

                max_off = config.get("max_off_blocks", 12)
                min_on = config.get("min_on_blocks", 44)
                cold_th = config.get("cold_temp_threshold", 0)
                min_mins = config.get("min_minutes_cold", 15)

                p_cap_today, m_blocks_today, reasoning_today = get_ai_optimized_parameters(
                    prices_today, temp_forecast, default_price_cap=50.0, default_min_on_blocks=min_on, target_day_label="idag"
                )
                p_cap_tom, m_blocks_tom, reasoning_tom = get_ai_optimized_parameters(
                    prices_tomorrow, temp_forecast, default_price_cap=50.0, default_min_on_blocks=min_on, target_day_label="imorgon"
                )

                updated_config = config.copy()
                updated_config["schedule_date"] = today_str
                updated_config["daily_schedule"] = generate_daily_schedule(prices_today, max_off_blocks=max_off, min_on_blocks=m_blocks_today, current_temp=current_temp, cold_threshold=cold_th, min_minutes_cold=min_mins, price_cap=p_cap_today)
                updated_config["tomorrow_schedule_date"] = tomorrow_str
                updated_config["tomorrow_schedule"] = generate_daily_schedule(prices_tomorrow, max_off_blocks=max_off, min_on_blocks=m_blocks_tom, current_temp=current_temp, cold_threshold=cold_th, min_minutes_cold=min_mins, price_cap=p_cap_tom)
                updated_config["price_cap"] = p_cap_today
                updated_config["min_on_blocks"] = m_blocks_today
                updated_config["ai_latest_reasoning"] = reasoning_today

                save_config(updated_config)
                st.cache_data.clear()
                st.success("Scheman och AI-analyser för både idag och imorgon har uppdaterats!")
                st.rerun()
            except Exception as e:
                st.error(f"Ett fel uppstod: {e}")
# --- TAB 3: SCHEMA & PRISER ---
with tab3:
    st.subheader("Planerat elschema, pris och temperaturprognos")
    
    now = datetime.now(sweden_tz)
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_dt = now + timedelta(days=1)
    tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")

    selected_day_tab = st.radio("Välj dag att granska:", ["Idag (" + today_str + ")", "Imorgon (" + tomorrow_str + ")"], horizontal=True)

    target_dt = now if "Idag" in selected_day_tab else tomorrow_dt
    prices = get_nordpool_price(zone="SE2", target_date=target_dt)
    temp_forecast = get_hourly_temp_forecast(target_date=target_dt) # Skicka med target_date!

    # Mappa 24 timmars temperatur till 96 kvartar för grafen
    temp_96 = []
    for t_val in temp_forecast:
        temp_96.extend([t_val] * 4)
    while len(temp_96) < 96:
        temp_96.append(temp_96[-1] if temp_96 else 0.0)

    if "Idag" in selected_day_tab:
        schedule = config.get("daily_schedule")
        if not schedule or len(schedule) != 96:
            schedule = [True] * 96
    else:
        schedule = config.get("tomorrow_schedule")
        if config.get("tomorrow_schedule_date") != tomorrow_str or not schedule:
            max_off = config.get("max_off_blocks", 12)
            min_on = config.get("min_on_blocks", 44)
            price_cap = config.get("price_cap", 50.0)
            schedule = generate_daily_schedule(prices, max_off_blocks=max_off, min_on_blocks=min_on, price_cap=price_cap)

    times = []
    for h in range(24):
        for m in [0, 15, 30, 45]:
            times.append(f"{h:02d}:{m:02d}")

    df_view = pd.DataFrame({
        "Tid": times,
        "Pris (öre/kWh)": prices[:len(times)],
        "Temperatur (°C)": temp_96[:len(times)],
        "Status": ["PÅ 🟢" if s else "AV 🔴" for s in schedule[:len(times)]],
        "status_num": [1 if s else 0 for s in schedule[:len(times)]]
    })

    fig_view = make_subplots(specs=[[{"secondary_y": True}]])
    status_colors = ['#2ecc71' if s else '#e74c3c' for s in schedule[:len(times)]]
    
    # Status (höger axel)
    fig_view.add_trace(
        go.Scatter(
            x=df_view["Tid"], y=df_view["status_num"],
            name="Planerad Status", mode="lines+markers",
            line=dict(shape="hv", color="rgba(150, 150, 150, 0.3)", width=2),
            marker=dict(size=8, color=status_colors)
        ),
        secondary_y=True
    )

    # Elpris (vänster axel)
    fig_view.add_trace(
        go.Scatter(
            x=df_view["Tid"], y=df_view["Pris (öre/kWh)"],
            name="Elpris (öre/kWh)", mode="lines",
            line=dict(color="#29b6f6", width=2)
        ),
        secondary_y=False
    )

    # Temperaturprognos (vänster axel)
    fig_view.add_trace(
        go.Scatter(
            x=df_view["Tid"], y=df_view["Temperatur (°C)"],
            name="Prognos Temp (°C)", mode="lines",
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
    fig_view.update_yaxes(title_text="Elpris (öre/kWh) & Temp (°C)", secondary_y=False)
    fig_view.update_yaxes(title_text="Status", tickvals=[0, 1], ticktext=["AV", "PÅ"], range=[-0.2, 1.2], secondary_y=True)

    st.plotly_chart(fig_view, width='stretch')

    with st.expander("Visa tabell med samtliga kvartar"):
        st.dataframe(df_view[["Tid", "Pris (öre/kWh)", "Temperatur (°C)", "Status"]], width='stretch')