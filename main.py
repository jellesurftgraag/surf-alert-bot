import os, json, datetime as dt, requests, http.client
import statistics as stats

# -----------------------
# Instellingen
# -----------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SPOT = {"name": "Scheveningen Pier", "lat": 52.109, "lon": 4.276, "bearing": 270}
TZ = "Europe/Amsterdam"


# -----------------------
# Surf- en winddata ophalen
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
    waves = marine["hourly"]["wave_height"]
    periods = marine["hourly"]["swell_wave_period"]
    winds = wind["hourly"]["windspeed_10m"]
    dirs = wind["hourly"]["winddirection_10m"]
    start_date = dt.date.fromisoformat(hrs[0][:10])

    data = []
    for d in range(3):
        date = start_date + dt.timedelta(days=d)
        # Alleen uren 08–20
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
        min_w, max_w = min(ws) * 3.6, max(ws) * 3.6
        min_h, max_h = min(wv), max(wv)
        min_t, max_t = min(pr), max(pr)

        # Energie (kJ/m)
        energy = 0.49 * avg_wave**2 * avg_per

        # Wind classificatie
        def angle_diff(a, b): return abs((a - b + 180) % 360 - 180)
        if angle_diff(avg_dir, 270) <= 60:
            wind_type = "onshore"
        elif angle_diff(avg_dir, 90) <= 60:
            wind_type = "offshore"
        else:
            wind_type = "sideshore"

        # Vensters (3u)
        best_score, best_window = -999, "—"
        for h in range(8, 18):  # 08–20 => 3u blokken
            sel = [i for i, t in enumerate(hrs) if t.startswith(str(date)) and h <= int(t[11:13]) < h + 3]
            if not sel:
                continue
            H = stats.mean([waves[i] for i in sel])
            T = stats.mean([periods[i] for i in sel])
            W = stats.mean([winds[i] for i in sel]) * 3.6
            raw = H**2 * T
            penalty = max(0, W - 30) * 0.8
            score = raw - penalty
            if score > best_score:
                best_score, best_window = score, f"{h:02d}–{h+3:02d}u"

        # Kleur
        if avg_wave >= 1.1 and avg_per >= 7 and avg_wind * 3.6 < 30:
            color = "🟢"
        elif avg_wave >= 0.6 or avg_per >= 5 or avg_wind * 3.6 <= 30:
            color = "🟠"
        else:
            color = "🔴"

        data.append({
            "date": date,
            "color": color,
            "wind": f"{min_w:.0f}–{max_w:.0f} km/h {wind_type}",
            "swell": f"{min_h:.1f}–{max_h:.1f} m — periode {min_t:.0f}–{max_t:.0f} s (~{energy:.1f} kJ/m)",
            "best": best_window,
            "avg_wave": avg_wave,
            "avg_per": avg_per,
            "avg_wind": avg_wind * 3.6,
            "energy": energy,
        })
    return data


# -----------------------
# AI-tekst (alleen titel + korte samenvatting)
# -----------------------
def ai_text(day):
    prompt = (
        f"Data: {json.dumps(day, ensure_ascii=False)}\n\n"
        "Geef één korte titelregel (max 6 woorden) en één zin samenvatting van de surfdag in het Nederlands, "
        "in de stijl van een relaxte surfcoach. "
        "Geen cijfers herhalen, geen tijden, geen windrichtingen, alleen sfeer en kwaliteit."
    )

    body = json.dumps({
        "model": "llama-3.1-8b-instant",
        "messages": [
            {"role": "system", "content": "Je bent een Nederlandse surfcoach die kort en natuurlijk schrijft."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 100,
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
    lines = []
    for i, day in enumerate(summary):
        label = day["date"].strftime("%A %d %b")
        # ✅ Fix: datum eerst omzetten naar tekst
        day_serializable = {k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in day.items()}
        ai_part = ai_text(day_serializable)
        lines.append(
            f"📅 {label}\n"
            f"{day['color']} {ai_part}\n"
            f"Wind: {day['wind']}\n"
            f"Swell: {day['swell']}\n"
            f"👉 Beste moment: {day['best']}\n"
        )

        # Korte piek waarschuwing
        if day["color"] != "🟢" and day["avg_wave"] < 0.8:
            lines.append("⚠️ Korte piek — buiten deze uren vrij rommelig.\n")

    # Toekomstzinnen (dag +1 en +2)
    if len(summary) > 1:
        tomorrow = summary[1]
        lines.append(
            f"Morgen: {tomorrow['best']} lijkt oké rond {tomorrow['avg_wave']:.1f} m / {tomorrow['avg_per']:.1f} s — kans op {tomorrow['color']}."
        )
    if len(summary) > 2:
        overmorgen = summary[2]
        lines.append(
            f"Overmorgen: {overmorgen['avg_wave']:.1f} m / {overmorgen['avg_per']:.1f} s — waarschijnlijk {overmorgen['color']}."
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
    message = build_message(SPOT["name"], summary)
    send_telegram_message(message)
    print("✅ Surfbericht verzonden!")
