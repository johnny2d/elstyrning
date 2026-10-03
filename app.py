import streamlit as st
import requests
import json
import pandas as pd

st.set_page_config(page_title="Elstyrning Control Panel", page_icon="⚡", layout="wide")

# --- LÖSENORDSSKYDD ---
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

# --- GIST-FUNKTIONER ---
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
        "max_price_percent": 50,
        "max_price_cap": 250,
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

# Använd flikar för att dela upp Gränssnitt och Historik/Graf
tab1, tab2 = st.tabs(["📊 Övervakning & Graf", "⚙️ Inställningar"])

# --- TAB 1: HISTORIK OCH GRAFER ---
with tab1:
    st.subheader("Senaste dygnets pris och elementstatus")
    
    if history:
        df = pd.DataFrame(history)
        
        # Konvertera tid till datetime för snyggare visualisering
        df['time'] = pd.to_datetime(df['timestamp'])
        
        # Visa nuvarande status
        latest = df.iloc[-1]
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Aktuellt pris", f"{latest.get('price', 0):.2f} öre/kWh")
        col2.metric("Elementstatus", "PÅ 🟢" if latest.get('status') == 'ON' else "AV 🔴")
        col3.metric("Utetemperatur", f"{latest.get('temp', 0):.1f} °C")
        col4.metric("Senast uppdaterad", latest['time'].strftime('%H:%M'))

        # Rita priskurvan
        st.line_chart(df, x='time', y='price', color="#29b6f6")

        # Visa tabell över historik
        with st.expander("Visa rådata / logg"):
            st.dataframe(df[['timestamp', 'price', 'status', 'temp']].sort_values(by='timestamp', ascending=False), use_container_width=True)
    else:
        st.info("Ingen historik har loggats än. När styrskriptet körs första gången kommer datan att visas här!")

# --- TAB 2: INSTÄLLNINGAR ---
with tab2:
    st.subheader("1. Prisbaserad styrning")
    max_price_percent = st.slider(
        "Andel billigaste timmar/kvartar per dygn (%)",
        min_value=10, max_value=100, value=config.get("max_price_percent", 50), step=5
    )

    max_price_cap = st.number_input(
        "Absolut maxpris (öre/kWh) – Slå av om priset överstiger detta",
        min_value=0, max_value=1000, value=config.get("max_price_cap", 250)
    )

    st.divider()

    st.subheader("2. Koldskydd & Utetemperatur")
    cold_temp_threshold = st.number_input(
        "Gräns för kyla (°C) – Aktivera lägsta körtid när det är kallare än",
        min_value=-30, max_value=10, value=config.get("cold_temp_threshold", 0)
    )

    min_minutes_cold = st.select_slider(
        "Minst antal minuter körtid per timme vid kyla",
        options=[0, 15, 30, 45, 60],
        value=config.get("min_minutes_cold", 15)
    )

    st.divider()

    st.subheader("3. Manuell överstyrning")
    force_override = st.radio(
        "Driftläge",
        options=["AUTO", "PÅ (Tvinga igång)", "AV (Stäng av allt)"],
        index=["AUTO", "PÅ (Tvinga igång)", "AV (Stäng av allt)"].index(config.get("force_override", "AUTO"))
    )

    if st.button("💾 Spara inställningar", type="primary"):
        new_config = {
            "max_price_percent": max_price_percent,
            "max_price_cap": max_price_cap,
            "cold_temp_threshold": cold_temp_threshold,
            "min_minutes_cold": min_minutes_cold,
            "force_override": force_override
        }
        save_config(new_config)