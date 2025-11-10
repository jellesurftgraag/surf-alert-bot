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

# NL datumlabels
WEEKDAYS_NL = ["maandag","dinsdag","woensdag","donderdag","vrijdag","zaterdag","zondag"]
MONTHS_ABBR_NL = ["jan","feb","mrt","apr","mei","jun","jul","aug","sep","okt","nov","dec"]

def format_date_nl(d: dt.date) -> str:
    wd = WEEKDAYS_NL[d.weekday()].capitalize()
    return f"{wd} {d.day:02d} {MONTHS_ABBR_NL[d.month-1]}"

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

        # Kleur (regels zoals afgesproken)
        if avg_wave >= 1.1 and avg_per >= 7 and avg_wind * 3.6 < 30:
            color = "🟢"
        elif avg_wave < 0.6 or avg_per < 5 or (wind_type == "onshore" and avg_wind * 3.6 > 30):
            color = "🔴"
        else:
            color = "🟠"

        data.append({
            "date": date,
            "date_label": format_date_nl(date),
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
# AI-tekst (alleen titel + korte samenvatting, strikt JSON)
# -----------------------
def ai_text(day_dict):
    # Verwacht: day_dict heeft alleen JSON-serializable types
    day_json = json.dumps(day_dict, ensure_ascii=False)
    prompt = (
        "Je krijgt dagdata als JSON onder 'data'. "
        "Geef uitsluitend een JSON-object terug met precies deze velden: "
        '{"title": "...", "summary": "..."}. '
        "In het Nederlands, kort en natuurlijk. "
        "Geen extra tekst, geen markdown, geen labels.\n\n"
        f"data = {day_json}"
    )

    body = json.dumps({
        "model": "llama-3.1-8b-instant",
        "messages": [
            {"role": "system", "content": "Schrijf beknopt als Nederlandse surfcoach. Antwoord ALLEEN met JSON met velden 'title' en 'summary'."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 120,
    })

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    conn = http.client.HTTPSConnection("api.groq.com")
    conn.request("POST", "/openai/v1/chat/completions", body, headers)
    res = conn.getresponse()
    data = res.read().decode()

    # Fallbacks bij fout of ongeldige JSON
    if res.status != 200:
        return ("Rustig met momenten", "Korte sessie in het beste venster; buiten die uren matig.")
    try:
        content = json.loads(data)["choices"][0]["message"]["content"].strip()
        # soms levert een model ```json ... ```
        if content.startswith("```"):
            content = content.strip("` \n")
            if content.lower().startswith("json"):
                content = content[4:].lstrip()
        obj = json.loads(content)
        title = str(obj.get("title", "")).strip() or "Rustig met momenten"
        summary = str(obj.get("summary", "")).strip() or "Korte sessie in het beste venster; buiten die uren matig."
        return (title, summary)
    except Exception:
        return ("Rustig met momenten", "Korte sessie in het beste venster; buiten die uren matig.")

# -----------------------
# Bericht samenstellen
# -----------------------
def build_message(spot, summary):
    lines = []
    for i, day in enumerate(summary):
        label = day["date_label"]
        # datum JSON-safe maken voor de AI
        day_serializable = {k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in day.items()}
        title, summary_line = ai_text(day_serializable)

        lines.append(
            f"📅 {label}\n"
            f"{day['color']} {title}\n"
            f"Wind: {day['wind']}\n"
            f"Swell: {day['swell']}\n"
            f"👉 Beste moment: {day['best']}\n\n"
            f"🔍 Samenvatting: {summary_line}\n"
        )

        # Korte piek waarschuwing (behouden zoals afgesproken)
        if day["color"] != "🟢" and day["avg_wave"] < 0.8:
            lines.append("⚠️ Korte piek — buiten deze uren vrij rommelig.\n")

    # Toekomstzinnen (dag +1 en +2) — volledig regel-based
    if len(summary) > 1:
        t = summary[1]
        lines.append(
            f"Morgen: {t['best']} lijkt oké rond {t['avg_wave']:.1f} m / {t['avg_per']:.1f} s — kans op {t['color']}."
        )
    if len(summary) > 2:
        o = summary[2]
        lines.append(
            f"Overmorgen: {o['avg_wave']:.1f} m / {o['avg_per']:.1f} s — waarschijnlijk {o['color']}."
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
