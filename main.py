import os, json, datetime as dt, requests, http.client, statistics as stats

# -----------------------
# Instellingen
# -----------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SPOT = {"name": "Scheveningen Pier", "lat": 52.109, "lon": 4.276, "bearing": 270}
TZ = "Europe/Amsterdam"

DAGEN = ["Maandag", "Dinsdag", "Woensdag", "Donderdag", "Vrijdag", "Zaterdag", "Zondag"]
MAANDEN = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]

# -----------------------
# Data ophalen
# -----------------------
def get_marine(lat, lon, days=2):
    marine = requests.get(
        "https://marine-api.open-meteo.com/v1/marine",
        params={
            "latitude": lat,
            "longitude": lon,
            "timezone": TZ,
            "hourly": "wave_height,swell_wave_period",
            "forecast_days": days + 1,
        },
        timeout=30,
    ).json()
    wind = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "timezone": TZ,
            "hourly": "windspeed_10m,winddirection_10m",
            "forecast_days": days + 1,
        },
        timeout=30,
    ).json()
    return marine, wind

# -----------------------
# Samenvatten + beste venster
# -----------------------
def summarize_forecast(marine, wind):
    hrs = marine["hourly"]["time"]
    # Correcties richting “surfhoogte” en realistischer periode
    waves = [h * 1.4 if h is not None else None for h in marine["hourly"]["wave_height"]]
    periods = [(p + 1.0) if p is not None else None for p in marine["hourly"]["swell_wave_period"]]
    winds = wind["hourly"]["windspeed_10m"]   # km/u
    dirs = wind["hourly"]["winddirection_10m"]
    start_date = dt.date.fromisoformat(hrs[0][:10])

    data = []
    for d in range(3):
        date = start_date + dt.timedelta(days=d)
        # Alleen 08–20u meenemen
        idx = [i for i, t in enumerate(hrs) if t.startswith(str(date)) and 8 <= int(t[11:13]) < 20]
        if not idx:
            continue

        wv = [waves[i] for i in idx if waves[i] is not None]
        pr = [periods[i] for i in idx if periods[i] is not None]
        ws = [winds[i] for i in idx if winds[i] is not None]
        wd = [dirs[i] for i in idx if dirs[i] is not None]
        if not (wv and pr and ws and wd):
            continue

        avg_wave, avg_per, avg_wind, avg_dir = map(stats.mean, [wv, pr, ws, wd])
        min_w, max_w = min(ws), max(ws)
        min_h, max_h = min(wv), max(wv)
        min_t, max_t = min(pr), max(pr)
        energy = 0.49 * avg_wave**2 * avg_per  # kJ/m

        # Windrichting t.o.v. kustlijn
        def angle_diff(a, b):
            return abs((a - b + 180) % 360 - 180)

        if angle_diff(avg_dir, 270) <= 60:
            wind_type = "onshore"
        elif angle_diff(avg_dir, 90) <= 60:
            wind_type = "offshore"
        else:
            wind_type = "sideshore"

        # Puntensysteem (dag + venster gebruiken dezelfde logica)
        def score_for_conditions(H, T, W, dir_type):
            score = 0.0
            # Hoogte
            if H >= 1.1:
                score += 1.0
            elif 0.8 <= H < 1.1:
                score += 0.5
            elif 0.6 <= H < 0.8:
                score += 0.3
            # Periode
            if T >= 7:
                score += 1.0
            elif 6 <= T < 7:
                score += 0.5
            elif 5 <= T < 6:
                score += 0.3
            # Windkracht
            if W <= 15:
                score += 1.0
            elif 16 <= W <= 25:
                score += 0.6
            elif 26 <= W <= 30:
                score += 0.3
            # Windrichting
            if dir_type == "offshore":
                score += 0.7
            elif dir_type == "onshore" and W > 20:
                score -= 0.3
            return score

        # Dag-score (voor context, maar kleur baseren we op beste venster)
        day_score = score_for_conditions(avg_wave, avg_per, avg_wind, wind_type)

        # Beste glijdend 3u-venster (08–20u, start elk heel uur)
        best_score, best_window = -999.0, "—"
        for h in range(8, 18):  # 8–19 start → laatste venster 19–22 maar we hebben data tot <20
            sel = [
                i for i, t in enumerate(hrs)
                if t.startswith(str(date)) and h <= int(t[11:13]) < h + 3
            ]
            if not sel:
                continue
            H = stats.mean([waves[i] for i in sel if waves[i] is not None])
            T = stats.mean([periods[i] for i in sel if periods[i] is not None])
            W = stats.mean([winds[i] for i in sel if winds[i] is not None])
            s = score_for_conditions(H, T, W, wind_type)
            if s > best_score:
                best_score = s
                best_window = f"{h:02d}–{h+3:02d}u"

        # Kleur op basis van beste venster (dag-score vooral voor gevoel)
        if best_score >= 2.3 and energy >= 2.5:
            color = "🟢"
        elif best_score >= 1.0:
            color = "🟠"
        else:
            color = "🔴"

        data.append({
            "date": date,
            "color": color,
            "wind": f"{min_w:.0f}–{max_w:.0f} km/h {wind_type}",
            "swell": f"{min_h:.1f}–{max_h:.1f} m — periode {round(min_t)}–{round(max_t)} s (~{energy:.1f} kJ/m)",
            "best": best_window,
            "avg_wave": avg_wave,
            "avg_per": avg_per,
            "avg_wind": avg_wind,
            "energy": energy,
            "wind_type": wind_type,
            "day_score": day_score,
            "best_score": best_score,
        })
    return data

# -----------------------
# AI-tekst (gekoppeld aan kleur + data)
# -----------------------
def ai_text(day):
    """
    Korte, natuurlijke surfbeschrijving op basis van de berekende data.
    Toon is gekoppeld aan de kleur (rood/oranje/groen).
    """

    kleur = day["color"]
    wave = round(day["avg_wave"], 1)
    period = round(day["avg_per"])
    wind = round(day["avg_wind"])
    dir = day.get("wind_type", "onbekend")
    energy = round(day["energy"], 1)

    prompt = f"""
Je bent een ervaren Nederlandse surfcoach die korte, nuchtere updates schrijft over de surf aan de Noordzee (Scheveningen).
Je krijgt meetdata en een kleurbeoordeling van een algoritme. Schrijf op basis daarvan één korte, natuurlijke zin
(max 12 woorden) over hoe de surfdag aanvoelt.

### Data
- Golfhoogte: {wave} m
- Periode: {period} s
- Windsnelheid: {wind} km/u
- Windrichting: {dir}
- Energie: {energy} kJ/m
- Eindkleur: {kleur}

### Richtlijnen
- Gebruik de kleur als sentiment:
  - 🟢 = goed, clean, krachtig, mooi venster, duidelijk surfbaar
  - 🟠 = oké, surfbaar maar rommelig of matig
  - 🔴 = slecht, weinig kracht, korte swell of te veel wind
- Schrijf alsof je een maat appt over het surfweer.
- Nuchtere spreektaal, geen poëzie.
- Gebruik woorden als clean, rommelig, blown out, prima, matig, krachtig, klein, deining.
- Geen cijfers, geen tijden, geen opsommingen.
- Maximaal één zin.
- Geef alleen de zin, zonder uitleg of aanhalingstekens.
"""

    body = json.dumps({
        "model": "llama-3.1-8b-instant",
        "messages": [
            {
                "role": "system",
                "content": "Je schrijft korte, realistische surfupdates in het Nederlands, in natuurlijke spreektaal."
            },
            {"role": "user", "content": prompt.strip()},
        ],
        "temperature": 0.7,
        "max_tokens": 50,
    })

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    conn = http.client.HTTPSConnection("api.groq.com")
    conn.request("POST", "/openai/v1/chat/completions", body, headers)
    res = conn.getresponse()
    data = res.read().decode()

    if res.status != 200:
        return "Geen titel beschikbaar."
    try:
        return json.loads(data)["choices"][0]["message"]["content"].strip()
    except Exception:
        return "Geen titel beschikbaar."

# -----------------------
# Bericht samenstellen
# -----------------------
def build_message(spot, summary):
    today = summary[0]
    d = today["date"]
    label = f"{DAGEN[d.weekday()]} {d.day} {MAANDEN[d.month-1]}"
    ai_part = ai_text(today)

    lines = [
        f"📅 {label}",
        f"{today['color']} {ai_part}",
        f"Wind: {today['wind']}",
        f"Swell: {today['swell']}",
        f"👉 Beste moment: {today['best']}",
        "",
    ]

    # Mapping van kleur naar korte kwalificatie
    def color_word(c):
        if c == "🟢":
            return "goed"
        if c == "🟠":
            return "oké"
        return "slecht"

    if len(summary) > 1:
        t = summary[1]
        lines.append(
            f"{t['color']} Morgen: rond {t['best']} lijkt het {color_word(t['color'])}, "
            f"met ~{t['avg_wave']:.1f} m en {round(t['avg_per'])} s swell."
        )
    if len(summary) > 2:
        o = summary[2]
        lines.append(
            f"{o['color']} Overmorgen: waarschijnlijk {color_word(o['color'])}, "
            f"met ~{o['avg_wave']:.1f} m en {round(o['avg_per'])} s swell."
        )

    return "\n".join(lines)

# -----------------------
# Telegram bericht sturen
# -----------------------
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    requests.post(url, json=payload, timeout=20).raise_for_status()

# -----------------------
# Main
# -----------------------
if __name__ == "__main__":
    marine, wind = get_marine(SPOT["lat"], SPOT["lon"], days=2)
    summary = summarize_forecast(marine, wind)
    if not summary:
        send_telegram_message("Geen surfdata beschikbaar vandaag 🌊")
    else:
        message = build_message(SPOT["name"], summary)
        send_telegram_message(message)
    print("✅ Surfbericht verzonden!")
