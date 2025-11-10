import os, json, datetime as dt, requests
from openai import OpenAI

# -----------------------
# Instellingen
# -----------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # jouw chat-ID (zie uitleg hieronder)
SPOT = {
    "name": "Scheveningen Pier",
    "lat": 52.109,
    "lon": 4.276,
    "beach_bearing_deg": 270
}
TZ = "Europe/Amsterdam"

client_oa = OpenAI(api_key=OPENAI_API_KEY)

# -----------------------
# Data ophalen
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
# Samenvatten en AI-analyse
# -----------------------
def summarize_forecast(marine):
    hours = marine["hourly"]["time"]
    waves = marine["hourly"]["wave_height"]
    periods = marine["hourly"]["swell_wave_period"]
    now = dt.datetime.now().astimezone().isoformat()

    data = []
    start_date = dt.date.fromisoformat(hours[0][:10])

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

def ai_interpretation(spot_name, summary):
    instructions = (
        "Je bent een surfcoach. Schrijf kort en duidelijk in het Nederlands, "
        "maximaal 750 tekens. Gebruik bullets. "
        "Beoordeel voor elke dag hoe de surf is in Scheveningen, "
        "voor beginners, intermediates en longboarders. "
        "Zeg wanneer het waarschijnlijk het beste moment van de dag is (ochtend/middag/avond)."
    )
    user = f"Spot: {spot_name}. Data: {json.dumps(summary, ensure_ascii=False)}"
    resp = client_oa.responses.create(
        model="gpt-4o-mini",
        instructions=instructions,
        input=user
    )
    return resp.output_text.strip()

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
    print("Surfbericht verzonden!")
