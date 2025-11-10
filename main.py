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
        "hourly": "wave_height,swell_wave_period,windspeed_10m",
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
    winds = marine["hourly"]["windspeed_10m"]
    start_date = dt.date.fromisoformat(hours[0][:10])
    data = []

    for d in range(3):  # vandaag + 2 dagen
        date = start_date + dt.timedelta(days=d)
        idx = [i for i, t in enumerate(hours) if t.startswith(str(date))]
        if not idx:
            continue
        avg_wave = sum(waves[i] for i in idx) / len(idx)
        avg_period = sum(periods[i] for i in idx if periods[i]) / len(idx)
        avg_wind = sum(winds[i] for i in idx) / len(idx)
        data.append({
            "date": date.isoformat(),
            "wave_height_m": round(avg_wave, 2),
            "swell_period_s": round(avg_period, 1),
            "wind_kmh": round(avg_wind * 3.6, 1)  # m/s → km/h
        })
    return data

# -----------------------
# Interpretatie via Groq
# -----------------------
def ai_interpretation(spot_name, summary):
    """Laat Groq een compacte surfupdate maken in jouw gewenste format."""
    prompt = (
        f"Spot: {spot_name}\n"
        f"Data: {json.dumps(summary, ensure_ascii=False)}\n\n"
        "Maak een surfbericht in exact dit format:\n\n"
        "📅 [Weekdag dd mmm]\n"
        "[🔴/🟠/🟢] + een korte titelregel met de algemene indruk.\n"
        "Wind: [xx–xx km/h + richting]\n"
        "Swell: [hoogte m–m — periode s]\n"
        "👉 Beste moment: [tijdvak + korte uitleg]\n\n"
        "🔍 Samenvatting: [één zin met evaluatie, bijv. 'Clean en krachtig bij laag tij.']\n\n"
        "Gebruik emoji’s en markdown.\n"
        "Baseer kleur en toon op deze richtlijnen:\n"
        "🔴 Slecht: golfhoogte <0.6 m of >2.5 m, swellperiode <5 s, of harde aanlandige wind >30 km/h.\n"
        "🟠 Matig: golfhoogte 0.6–1.1 m, swellperiode 5–6 s, of matige wind 15–30 km/h.\n"
        "🟢 Goed: golfhoogte ≥1.1 m, swellperiode ≥7 s, en zwakke tot offshore wind <15 km/h.\n"
        "Wind telt pas echt mee boven 30 km/h. Schrijf kort (max 450 tekens) als een relaxte Nederlandse surfcoach."
    )

    body = json.dumps({
        "model": "llama-3.1-8b-instant",
        "messages": [
            {"role": "system", "content": "Je bent een Nederlandse surfcoach die realistische, beknopte forecasts schrijft."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 400
    })

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    conn = http.client.HTTPSConnection("api.groq.com")
    conn.request("POST", "/openai/v1/chat/completions", body, headers)
    res = conn.getresponse()
    data = res.read().decode()

    if res.status != 200:
        return f"Geen forecast vandaag (HTTP {res.status})"
    try:
        response = json.loads(data)
        return response["choices"][0]["message"]["content"].strip()
    except Exception:
        return "Geen forecast vandaag (parsefout)"

# -----------------------
# Telegram bericht sturen
# -----------------------
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    requests.post(url, json=payload, timeout=15).raise_for_status()

# -----------------------
# Main flow
# -----------------------
if __name__ == "__main__":
    marine = get_marine(SPOT["lat"], SPOT["lon"], days=2)
    summary = summarize_forecast(marine)
    message = ai_interpretation(SPOT["name"], summary)
    send_telegram_message(message)
