import pytest
from datetime import datetime

# Importera beräkningslogiken från din main-fil
# (Tänk på att bryta ut beslutstagandet till en funktion om den inte redan är det)
def evaluate_heating_logic(current_price, current_temp, prices, config):
    """
    Rensad beräkningslogik för enhetstestning.
    Returnerar True (PÅ) eller False (AV).
    """
    override = config.get("force_override", "AUTO")
    
    if override == "PÅ (Tvinga igång)":
        return True
    elif override == "AV (Stäng av allt)":
        return False
    
    # AUTO-LÄGE
    max_percent = config.get("max_price_percent", 50)
    max_cap = config.get("max_price_cap", 250)
    cold_threshold = config.get("cold_temp_threshold", 0)
    min_mins_cold = config.get("min_minutes_cold", 15)
    
    # Beräkna gränssnitt för billigaste procenten
    sorted_prices = sorted(prices)
    cutoff_index = int(len(sorted_prices) * (max_percent / 100.0))
    price_limit = sorted_prices[min(cutoff_index, len(sorted_prices) - 1)]
    
    should_be_on = False
    
    # Regel A: Billigt nog och under maxpris
    if current_price <= price_limit and current_price <= max_cap:
        should_be_on = True
        
    # Regel B: Köldskydd (simulera minut 0 i timmen)
    current_minute = config.get("_simulated_minute", 0)
    if current_temp <= cold_threshold and min_mins_cold > 0:
        if current_minute < min_mins_cold:
            should_be_on = True
            
    return should_be_on


# --- ENHETSTESTER (UNIT TESTS) ---

@pytest.fixture
def sample_prices():
    # Enkel lista med 96 kvartar från 10 till 105 öre
    return [10.0 + i for i in range(96)]

def test_manual_override_on(sample_prices):
    config = {"force_override": "PÅ (Tvinga igång)"}
    # Även om priset är skyhögt ska den slå på
    assert evaluate_heating_logic(current_price=500.0, current_temp=10, prices=sample_prices, config=config) is True

def test_manual_override_off(sample_prices):
    config = {"force_override": "AV (Stäng av allt)"}
    # Även om priset är gratis ska den slå av
    assert evaluate_heating_logic(current_price=0.0, current_temp=10, prices=sample_prices, config=config) is False

def test_auto_cheap_price(sample_prices):
    config = {"force_override": "AUTO", "max_price_percent": 50, "max_price_cap": 250}
    # Lägsta priset (10 öre) ligger väl under 50%-gränsen
    assert evaluate_heating_logic(current_price=10.0, current_temp=10, prices=sample_prices, config=config) is True

def test_auto_expensive_price(sample_prices):
    config = {"force_override": "AUTO", "max_price_percent": 50, "max_price_cap": 250}
    # Högsta priset (105 öre) ligger över 50%-gränsen
    assert evaluate_heating_logic(current_price=105.0, current_temp=10, prices=sample_prices, config=config) is False

def test_max_price_cap_exceeded(sample_prices):
    config = {"force_override": "AUTO", "max_price_percent": 100, "max_price_cap": 50}
    # Även om max_price_percent är 100% är priset (60 öre) över pristaket (50 öre)
    assert evaluate_heating_logic(current_price=60.0, current_temp=10, prices=sample_prices, config=config) is False

def test_cold_protection_activated(sample_prices):
    config = {
        "force_override": "AUTO",
        "max_price_percent": 10,  # Snäv gräns så priset normalt ger OFF
        "max_price_cap": 250,
        "cold_temp_threshold": -5,
        "min_minutes_cold": 30,
        "_simulated_minute": 10   # Inom de första 30 min
    }
    # Priset är för högt (90 öre), men det är -10°C ute och minut 10 -> Köldskydd aktiveras!
    assert evaluate_heating_logic(current_price=90.0, current_temp=-10, prices=sample_prices, config=config) is True