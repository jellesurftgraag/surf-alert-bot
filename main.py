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
    waves = [h * 1.4 if h else None for h in marine["hourly"]["wave_height"]]
    periods = [(p + 0.5) if p else None for p in marine["hourly"]["swell_wave_period"]]
    winds = wind["hourly"]["windspeed_10m"]
    dirs = wind["hourly"]["winddirection_10m"]
    start_date = dt.date.fromisoformat(hrs[0][:10])

    data = []
    for d in range(3):
        date = start_date + dt.timedelta(days=d)
        idx = [i for i, t in enumerate(hrs) if t.startswith(str(date)) and 8 <= int(t[11:13]) < 20]
        if not idx:
            continue

        wv, pr, ws, wd = (
            [waves[i] for i in idx if waves[i] is not None],
            [periods[i] for i in idx if periods[i] is not None],
            [winds[i] for i in idx if winds[i] is not None],
            [dirs[i] for i in idx if dirs[i] is not None],
        )
        if not (wv and pr and ws and wd):
            continue

        avg_wave, avg_per, avg_wind, avg_dir = map(stats.mean, [wv, pr, ws, wd])
        min_w, max_w = min(ws), max(ws)
        min_h, max_h = min(wv), max(wv)
        min_t, max_t = min(pr), max(pr)
        energy = 0.49 * avg_wave**2 * avg_per

        def angle_diff(a, b): return abs((a - b + 180) % 360 - 180)
        if angle_diff(avg_dir, 270) <= 60:
            wind_type = "onshore"
        elif angle_diff(avg_dir, 90) <= 60:
            wind_type = "offshore"
        else:
            wind_type = "sideshore"

        # Puntensysteem
        def score_for_conditions(H, T, W, dir_type):
            score = 0
            # Hoogte
            if H >= 1.1: score += 1.0
            elif 0.8 <= H < 1.1: score += 0.5
            elif 0.6 <= H < 0.8: score += 0.3
            # Periode
            if T >= 7: score += 1.0
            elif 6 <= T < 7: score += 0.5
            elif 5 <= T < 6: score += 0.3
            # Windkracht
            if W <= 15: score += 1.0
            elif 16 <= W <= 25: score += 0.6
            elif 26 <= W <= 30: score += 0.3
            # Windrichting
            if dir_type == "offshore": score += 0.7
            elif dir_type == "onshore" and W > 20: score -= 0.3
            return score

        day_score = score_for_conditions(avg_wave, avg_per, avg_wind, wind_type)

        # Beste 3u-venster
        best_score, best_window = -999, "—"
        for h in range(8, 18, 3):
            sel = [i for i, t in enumerate(hrs) if t.startswith(str(date)) and h <= int(t[11:13]) < h + 3]
            if not sel:
                continue
            H = stats.mean([waves[i] for i in sel])
            T = stats.mean([periods[i] for i in sel])
            W = stats.mean([winds[i] for i in sel])
            s = score_for_conditions(H, T, W, wind_type)
            if s > best_score:
                best_score, best_window = s, f"{h:02d}–{h+3:02d}u"

        # Kleur op basis van beste venster
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
        })
    return data

# -----------------------
# AI-tekst (alleen vandaag)
# -----------------------
def ai_text(day):
    prompt = (
        f"Data: {json.dumps({**day, 'date': str(day['date'])}, ensure_ascii=False)}\n\n"
        "Je bent een relaxte Nederlandse surfcoach. Schrijf één korte, natuurlijke titel (max 6 woorden) "
        "over de surfdag, met een menselijke toon. Gebruik termen als clean, rommelig, matig, krachtig, goed venster. "
        "Geen cijfers, geen tijden, geen weerpraat. Zeg iets wat je tegen een surfer op het strand zou zeggen."
    )

    body = json.dumps({
        "model": "llama-3.1-8b-instant",
        "messages": [
            {"role": "system", "content": "Je bent een surfcoach die korte, natuurlijke surfbeschrijvingen geeft in spreektaal."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 60,
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

    if len(summary) > 1:
        t = summary[1]
        color_text = "goed" if t["color"] == "🟢" else "matig" if t["color"] == "🟠" else "slecht"
        lines.append(f"Morgen: rond {t['best']} lijkt het {color_text}, met ~{t['avg_wave']:.1f} m en {round(t['avg_per'])} s swell.")
    if len(summary) > 2:
        o = summary[2]
        color_text = "goed" if o["color"] == "🟢" else "matig" if o["color"] == "🟠" else "slecht"
        lines.append(f"Overmorgen: waarschijnlijk {color_text}, met ~{o['avg_wave']:.1f} m en {round(o['avg_per'])} s swell.")

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
