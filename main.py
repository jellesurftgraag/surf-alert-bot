import os, json, datetime as dt, requests, http.client

# -----------------------
# Instellingen
# -----------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SPOT = {"name": "Scheveningen Pier", "lat": 52.109, "lon": 4.276}
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
    for d in range(3):
        date = start_date + dt.timedelta(days=d)
        idx = [i for i, t in enumerate(hours) if t.startswith(str(date))]
        if not idx:
            continue
        avg_wave = sum(waves[i] for i in idx) / len(idx)
        avg_period = sum(periods[i] for i in idx if periods[i]) / len(idx)
        data.append({
            "date": date.isoformat(),
            "wave_height_m": round(avg_wave, 2),
            "swell_period_s": round(avg_period, 1)
        })
    return data

# -----------------------
# Interpretatie via Groq (DEBUG-versie)
# -----------------------
def ai_interpretation(spot_name, summary):
    """Diagnose van de API-call — toont exact wat er naar Groq gaat en wat terugkomt."""
    prompt = (
        f"Spot: {spot_name}\n"
        f"Data: {json.dumps(summary, ensure_ascii=False)}\n\n"
        "Schrijf kort en duidelijk in het Nederlands (<750 tekens).\n"
        "Gebruik bullets. Beschrijf per dag hoe de surf is voor beginners, "
        "intermediates en longboarders. "
        "Noem het beste moment (ochtend/middag/avond) op basis van golfhoogte en swellperiode. "
        "Gebruik een vriendelijke toon zoals een surfcoach."
    )

    body = json.dumps({
    "model": "llama-3.1-70b-versatile",
        "messages": [
            {"role": "system", "content": "Je bent een surfcoach die kort en helder in het Nederlands schrijft."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 600
    })

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    print("➡️  Verbinding maken met Groq…")
    print("🔑 GROQ key aanwezig:", bool(GROQ_API_KEY))
    print("📦 Body-lengte:", len(body), "bytes")

    conn = http.client.HTTPSConnection("api.groq.com")
    conn.request("POST", "/openai/v1/chat/completions", body, headers)
    res = conn.getresponse()
    data = res.read().decode()
    print("🌐 HTTP status:", res.status)
    print("🧾 Response (eerste 300 tekens):", data[:300])

    if res.status != 200:
        return f"Geen forecast vandaag (HTTP {res.status})"
    try:
        response = json.loads(data)
        return response["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Geen forecast vandaag (parsefout: {e})"

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
