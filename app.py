import streamlit as st
import requests
import json
from main import main, generate_daily_schedule

# Kör huvudlogiken (hämtar priser, kollar dygnsbyte, kör optimering för morgondagen om klockan är > 14)
main()
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

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
tab1, tab2, tab3 = st.tabs(["📊 Övervakning & Graf", "⚙️ Inställningar", "📅 Schema & Priser"])

# --- TAB 1: HISTORIK OCH GRAFER ---
with tab1:
    st.subheader("Senaste dygnets pris och elementstatus")
    
    if history:
        df = pd.DataFrame(history)
        
        # Konvertera tid till datetime för snyggare visualisering
        df['time'] = pd.to_datetime(df['timestamp'])
        
        # Skapa en sifferkolumn för ON/OFF (1 = ON, 0 = OFF)
        df['status_num'] = df['status'].apply(lambda x: 1 if x == 'ON' else 0)
        
        # Visa nuvarande status
        latest = df.iloc[-1]
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Aktuellt pris", f"{latest.get('price', 0):.2f} öre/kWh")
        col2.metric("Elementstatus", "PÅ 🟢" if latest.get('status') == 'ON' else "AV 🔴")
        col3.metric("Utetemperatur", f"{latest.get('temp', 0):.1f} °C")
        col4.metric("Senast uppdaterad", latest['time'].strftime('%H:%M'))

       # --- PLOTLY GRAF MED PRIS OCH ELEMENTSTATUS ---
        fig = make_subplots(specs=[[{"secondary_y": True}]])

        # 1. Status som punkter/steg för att synas även vid glapp i datan
        # Vi lägger till färgade punkter för varje loggad datapunkt
        status_colors = df['status'].map({'ON': '#2ecc71', 'OFF': '#e74c3c'})
        
        fig.add_trace(
            go.Scatter(
                x=df['time'],
                y=df['status_num'],
                name="Elementstatus",
                mode="lines+markers",
                line=dict(shape="hv", color="rgba(150, 150, 150, 0.5)", width=2),
                marker=dict(size=10, color=status_colors, symbol="circle"),
                hovertemplate="Tid: %{x|%H:%M}<br>Status: %{customdata}<extra></extra>",
                customdata=df['status']
            ),
            secondary_y=True
        )

        # 2. Linje + Punkter för Elpris (Vänster axel)
        fig.add_trace(
            go.Scatter(
                x=df['time'],
                y=df['price'],
                name="Elpris (öre/kWh)",
                mode="lines+markers",
                line=dict(color="#29b6f6", width=2),
                marker=dict(size=6, color="#29b6f6"),
                hovertemplate="Tid: %{x|%H:%M}<br>Pris: %{y:.2f} öre/kWh<extra></extra>"
            ),
            secondary_y=False
        )

        # Anpassa layout för graferna
        fig.update_layout(
            hovermode="x unified",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(l=10, r=10, t=30, b=10),
            height=420
        )

        # Primär Y-axel (Elpris)
        fig.update_yaxes(title_text="Elpris (öre/kWh)", secondary_y=False)
        
        # Sekundär Y-axel (ON/OFF)
        fig.update_yaxes(
            title_text="Status",
            tickvals=[0, 1],
            ticktext=["AV 🔴", "PÅ 🟢"],
            range=[-0.2, 1.2],
            secondary_y=True
        )

        st.plotly_chart(fig, use_container_width=True)
        # Visa tabell över historik
        with st.expander("Visa rådata / logg"):
            st.dataframe(df[['timestamp', 'price', 'status', 'temp']].sort_values(by='timestamp', ascending=False), use_container_width=True)
    else:
        st.info("Ingen historik har loggats än. När styrskriptet körs första gången kommer datan att visas här!")

# --- TAB 2: INSTÄLLNINGAR ---
with tab2:
    st.subheader("1. Optimeringsparametrar (PuLP)")
    
    # Minsta antal timmar på per dygn (omvandlas till 15-minutersblock internt, dvs * 4)
    min_on_hours = st.slider(
        "Minsta totala körtid per dygn (timmar)",
        min_value=4, max_value=20, value=int(config.get("min_on_blocks", 32) / 4), step=1,
        help="Hur många timmar per dygn som värmen absolut minst måste vara igång."
    )
    
    # Max avstängning i rad (omvandlas till 15-minutersblock, dvs * 4)
    max_off_hours = st.slider(
        "Maximal avstängning i rad (timmar)",
        min_value=1, max_value=6, value=int(config.get("max_off_blocks", 12) / 4), step=1,
        help="För att undvika att huset blir för kallt: maximalt antal timmar värmen får vara avstängd i sträck."
    )

    st.divider()

    st.subheader("2. Köldskydd & Utetemperatur")
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
        # Omvandla timmar till 15-minutersblock (multiplicera med 4) för optimeraren
        new_config = config.copy() # Behåll befintliga fält om det finns några
        new_config.update({
            "min_on_blocks": min_on_hours * 4,
            "max_off_blocks": max_off_hours * 4,
            "cold_temp_threshold": cold_temp_threshold,
            "min_minutes_cold": min_minutes_cold,
            "force_override": force_override
        })
        save_config(new_config)

        # --- TAB 3: SCHEMA & PRISER (IDAG & IMORGON) ---
with tab3:
    st.subheader("Planerat elschema & priser")
    
    sweden_tz = ZoneInfo("Europe/Stockholm")
    now = datetime.now(sweden_tz)
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_dt = now + timedelta(days=1)
    tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")

    # Hjälpfunktion för att hämta priser direkt i Streamlit för visning
    def fetch_prices_for_date(target_date):
        year = target_date.strftime("%Y")
        month_day = target_date.strftime("%m-%d")
        url = f"https://api.elprisetjustnu.se/api/v1/prices/{year}/{month_day}_SE2.json"
        try:
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                data = res.json()
                prices = [round(item["SEK_per_kWh"] * 100, 2) for item in data]
                if len(prices) == 24: # Fallback om timmar istället för kvartar
                    p96 = []
                    for p in prices:
                        p96.extend([p] * 4)
                    return p96
                return prices
        except:
            pass
        return [50.0] * 96

    # Visningsval: Idag eller Imorgon
    selected_day_tab = st.radio("Välj dag att granska:", ["Idag (" + today_str + ")", "Imorgon (" + tomorrow_str + ")"], horizontal=True)

    if "Idag" in selected_day_tab:
        st.markdown(f"### Dagens schema ({today_str})")
        prices = fetch_prices_for_date(now)
        schedule = config.get("daily_schedule")
        
        # Om schema saknas eller har fel längd, visa varning
        if not schedule or len(schedule) != 96:
            st.warning("Inget giltigt dygnschema sparat i Gist än. Visar råa priser.")
            schedule = [True] * 96

    else:
        st.markdown(f"### Morgondagens schema ({tomorrow_str})")
        prices = fetch_prices_for_date(tomorrow_dt)
        schedule = config.get("tomorrow_schedule")
        
        # Kolla om morgondagens schema redan ligger sparat i Gist
        if config.get("tomorrow_schedule_date") != tomorrow_str or not schedule:
            if now.hour >= 14:
                st.info("Klockan är efter 14:00. Hämtar priser och simulerar morgondagens optimering...")
                # Om vi vill visa en direkt uträknad simulering i gränssnittet
                from main import generate_daily_schedule # Importera din optimerare om den ligger i samma mapp
                max_off = config.get("max_off_blocks", 12)
                min_on = config.get("min_on_blocks", 32)
                schedule = generate_daily_schedule(prices, max_off_blocks=max_off, min_on_blocks=min_on)
            else:
                st.warning(f"Morgondagens priser och schema har inte genererats ännu (Nordpool släpper morgondagens priser runt kl. 13-14. Klockan är nu {now.strftime('%H:%M')}).")
                schedule = [True] * 96

    # Skapa tidsaxlar för 96 kvartar (00:00 till 23:45)
    times = []
    for h in range(24):
        for m in [0, 15, 30, 45]:
            times.append(f"{h:02d}:{m:02d}")

    df_view = pd.DataFrame({
        "Tid": times,
        "Pris (öre/kWh)": prices[:len(times)],
        "Status": ["PÅ 🟢" if s else "AV 🔴" for s in schedule[:len(times)]],
        "status_num": [1 if s else 0 for s in schedule[:len(times)]]
    })

    # Rita upp Plotly-graf för vald dag
    fig_view = make_subplots(specs=[[{"secondary_y": True}]])

    # Statuslinje
    status_colors = ['#2ecc71' if s else '#e74c3c' for s in schedule[:len(times)]]
    fig_view.add_trace(
        go.Scatter(
            x=df_view["Tid"], y=df_view["status_num"],
            name="Planerad Status", mode="lines+markers",
            line=dict(shape="hv", color="rgba(150, 150, 150, 0.4)", width=2),
            marker=dict(size=8, color=status_colors)
        ),
        secondary_y=True
    )

    # Prislinje
    fig_view.add_trace(
        go.Scatter(
            x=df_view["Tid"], y=df_view["Pris (öre/kWh)"],
            name="Elpris (öre/kWh)", mode="lines",
            line=dict(color="#29b6f6", width=2)
        ),
        secondary_y=False
    )

    fig_view.update_layout(
        hovermode="x unified",
        margin=dict(l=10, r=10, t=30, b=10),
        height=400,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )
    fig_view.update_yaxes(title_text="Elpris (öre/kWh)", secondary_y=False)
    fig_view.update_yaxes(title_text="Status", tickvals=[0, 1], ticktext=["AV", "PÅ"], range=[-0.2, 1.2], secondary_y=True)

    st.plotly_chart(fig_view, use_container_width=True)

    # Expander med tabell
    with st.expander("Visa tabell med samtliga kvartar"):
        st.dataframe(df_view[["Tid", "Pris (öre/kWh)", "Status"]], use_container_width=True)