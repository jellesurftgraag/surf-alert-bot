import os, json, datetime as dt, requests
import google.generativeai as genai

# -----------------------
# Instellingen
# -----------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SPOT = {
    "name": "Scheveningen Pier",
    "lat": 52.109,
    "lon": 4.276,
    "beach_bearing_deg": 270
}
TZ = "Europe/Amsterdam"

# Configureer Gemini
genai.configure(api_key=GEMINI_API_KEY)

# -----------------------
# Surfdata ophalen
# -----------------------
def get_marine(lat, lon, days=2):
    """Haalt golfhoogte en swellperiode op voor vandaag + 2 dagen"""
    base = "https://marine-api.open-meteo.com/v1/marine"
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": TZ,
        "hourly": "wave_height,swell_wave_period",
        "forecast_days": days + 1
    }
    r = requests.get(base, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

# -----------------------
# Data samenvatten
# -----------------------
def summarize_forecast(marine):
    hours = marine["hourly"]["time"]
    waves = marine["hourly"]["wave_height"]
    periods = marine["hourly"]["swell_wave_period"]
    start_date = dt.date.fromisoformat(hours[0][:10])
    data = []

    for d in range(3):  # vandaag + 2 dagen
        date = start_date + dt.timedelta(days=d)
        sel = [i for i, t in enumerate(hours) if t.startswith(str(date))]
        if not sel:
            continue
        avg_wave = sum(waves[i] for i in sel) / len(sel)
        avg_period = sum(periods[i] for i in sel if periods[i]) / len(sel)
        data.append({
            "date": date.isoformat(),
            "wave_height_m": round(avg_wave, 2),
            "swell_period_s": round(avg_period, 1)
        })
    return data

# -----------------------
# Interpretatie met Gemini
# -----------------------
def ai_interpretation(spot_name, summary):
    """Laat Gemini een korte surfanalyse maken in NL"""
    prompt = (
        f"Spot: {spot_name}\n"
        f"Data: {json.dumps(summary, ensure_ascii=False)}\n\n"
        "Schrijf kort en duidelijk in het Nederlands (<750 tekens).\n"
        "Gebruik bullets. Beschrijf per dag hoe de surf is voor beginners, "
        "intermediates en longboarders. "
        "Noem het beste moment (ochtend/middag/avond) op basis van golfhoogte en swellperiode. "
        "Gebruik een vriendelijke toon zoals een surfcoach."
    )

    model = genai.GenerativeModel("gemini-1.0-pro")
    response = model.generate_content(prompt)
    return response.text.strip()

# -----------------------
# Telegram bericht sturen
# -----------------------
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    r = requests.post(url, json=payload)
    r.raise_for_status()

# -----------------------
# Main flow
# -----------------------
if __name__ == "__main__":
    marine = get_marine(SPOT["lat"], SPOT["lon"], days=2)
    summary = summarize_forecast(marine)
    message = ai_interpretation(SPOT["name"], summary)
    send_telegram_message(message)
    print("✅ Surfbericht verzonden!")
