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

print("[DEBUG] 1. app.py har startat")

st.set_page_config(page_title="Elstyrning Control Panel", page_icon="⚡", layout="wide")

st.write("🔍 **Felsökningsläge aktivt:** Startar appen...")

# 1. Kolla secrets
try:
    if hasattr(st, "secrets"):
        for key in ["GEMINI_API_KEY", "GIST_ID", "GIST_TOKEN", "TUYA_API_KEY", "TUYA_API_SECRET", "APP_PASSWORD"]:
            if key in st.secrets:
                os.environ[key] = st.secrets[key]
    print("[DEBUG] 2. Secrets inlästa utan problem.")
    st.write("✅ Secrets inlästa.")
except Exception as e:
    st.error(f"❌ Fel vid inläsning av secrets: {e}")

# 2. Testa importer från main.py
try:
    from main import (
        get_tuya_device_status,
        generate_daily_schedule, 
        get_nordpool_price, 
        get_outside_temp, 
        get_hourly_temp_forecast
    )
    print("[DEBUG] 3. Importer från main.py lyckades.")
    st.write("✅ Importer från main.py OK.")
except Exception as e:
    st.error(f"❌ Fel vid import från main.py: {e}")
    st.stop()

# 3. Inloggningskontroll
def check_password():
    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False

    if not st.session_state["password_correct"]:
        st.title("🔒 Logga in på Elstyrningen")
        pwd = st.text_input("Lösenord", type="password")
        if st.button("Logga in"):
            entered_pwd = pwd if pwd else ""
            correct_pwd = st.secrets.get("APP_PASSWORD", "mitthemligalosenord")
            if entered_pwd == correct_pwd:
                st.session_state["password_correct"] = True
                st.rerun()
            else:
                st.error("Fel lösenord!")
        return False
    return True

if not check_password():
    st.stop()

print("[DEBUG] 4. Inloggning passerad.")

# 4. Hämta Gist-data med felhantering
GIST_ID = st.secrets.get("GIST_ID")
GITHUB_TOKEN = st.secrets.get("GIST_TOKEN")

headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json"
}

@st.cache_data(ttl=15)
def load_gist_data_debug():
    try:
        print("[DEBUG] 5. Gör anrop till GitHub Gist...")
        res = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=headers, timeout=10)
        print(f"[DEBUG] 6. Gist svarade med statuskod: {res.status_code}")
        if res.status_code == 200:
            files = res.json()["files"]
            config = json.loads(files["config.json"]["content"]) if "config.json" in files else {}
            history = json.loads(files["history.json"]["content"]) if "history.json" in files else []
            return config, history
        else:
            return {"error": f"Statuskod {res.status_code}"}, []
    except Exception as e:
        print(f"[DEBUG] Kritiskt fel vid Gist-hämtning: {e}")
        return {"error": str(e)}, []

config, history = load_gist_data_debug()

if isinstance(config, dict) and "error" in config:
    st.error(f"Kunde inte ladda Gist-data: {config['error']}")
    st.stop()

st.write("✅ Gist-data laddad utan problem! Appen rullar vidare...")