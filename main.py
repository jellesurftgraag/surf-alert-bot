import os, json, datetime as dt, requests

# -----------------------
# Instellingen
# -----------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SPOT = {
    "name": "Scheveningen Pier",
    "lat": 52.109,
    "lon": 4.276,
    "beach_bearing_deg": 270
}
TZ = "Europe/Amsterdam"

# -----------------------
# Surfdata ophalen
# -----------------------
def get_marine(lat, lon, days=2):
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
# Interpretatie via Groq (Llama 3)
# -----------------------
def ai_interpretation(spot_name, summary):
    """Laat Groq een korte surfanalyse maken in NL"""
    prompt = (
        f"Spot: {spot_name}\n"
        f"Data: {json.dumps(summary, ensure_ascii=False)}\n\n"
        "Schrijf kort en duidelijk in het Nederlands (<750 tekens).\n"
        "Gebruik bullets. Beschrijf per dag hoe de surf is voor beginners, "
        "intermediates en longboarders. "
        "Noem het beste moment (ochtend/middag/avond) op basis van golfhoogte en swellperiode. "
        "Gebruik een vriendelijke toon zoals een surfcoach."
    )

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama3-8b-8192",
        "messages": [
            {"role": "system", "content": "Je bent een surfcoach die kort en helder in het Nederlands schrijft."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 600
    }

    try:
        r = requests.request("POST", url, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        response = r.json()
        return response["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print("❌ Foutmelding:", getattr(e, "response", e))
        return f"Geen forecast vandaag (fout: {e})"

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
