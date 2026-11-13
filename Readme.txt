======================================================================

⚡ AUTONOMOUS AI HOME HEATING OPTIMIZER (SE)

======================================================================

A self-hosted, subscription-free autonomous agent system that optimizes 

direct electric heating and floor heating by leveraging spot-price 

volatility (Nordpool), weather forecasts (SMHI/MET), and AI-driven 

strategy (Google Gemini) combined with linear programming (PuLP).



Hardware integration is handled via Tuya Cloud (WiFi sockets & thermostats).

Persistence and configuration are stored via GitHub Gists (Zero database cost).

Frontend UI is hosted on Streamlit Cloud (Zero hosting cost).



----------------------------------------------------------------------

🤖 W ANT TO SET THIS UP? (AI-GUIDED SETUP)

----------------------------------------------------------------------

If you have cloned or downloaded this repository and want to set up your 

own instance, you don't need to read a massive manual. 



Copy the prompt below, open your favorite AI coding assistant (like 

ChatGPT, Claude, or Gemini), and paste it along with access to this repo:



---

"I want to deploy and configure this Autonomous AI Home Heating Optimizer 

project. Please guide me step-by-step through the entire setup process. 



Here is what we need to configure:

1. Setting up a GitHub Repository and a GitHub Gist (for state & config storage).

2. Configuring Streamlit Cloud for the web dashboard UI.

3. Setting up automated backend runs (via GitHub Actions or external cron jobs).

4. Connecting Tuya Cloud IoT hardware (WiFi switches & smart thermostats).

5. Setting up API keys (Google Gemini API and Tuya Cloud Developer credentials).



Please start by asking me for my target electricity price zone (e.g., SE2) 

and my Tuya device details, and then walk me through the configuration 

file (`secrets.toml`) and environment variables step-by-step."

---



----------------------------------------------------------------------

📂 REPOSITORY STRUCTURE

----------------------------------------------------------------------

- `app.py`       : Streamlit control panel UI (Dashboard, Settings, Schedule & Admin).

- `main.py`      : Backend optimization engine, AI strategy (Gemini), PuLP solver, and Tuya controller.

- `requirements.txt` : Python dependencies (`streamlit`, `pandas`, `plotly`, `pulp`, `tinytuya`, `requests`).
