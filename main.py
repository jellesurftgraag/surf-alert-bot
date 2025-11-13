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
# Helper: conditie-score
# -----------------------
def score_for_conditions(H, T, W, dir_type):
    """Geeft een surf-score voor één set condities."""
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
        score += 1.0  # offshore extra belonen
    elif dir_type == "onshore" and W > 20:
        score -= 0.3
    return score

# -----------------------
# Samenvatten + uur-scores + clusters
# -----------------------
def summarize_forecast(marine, wind):
    hrs = marine["hourly"]["time"]
    # Correcties voor realistischer surfgevoel
    waves = [h * 1.4 if h is not None else None for h in marine["hourly"]["wave_height"]]
    periods = [(p + 1.0) if p is not None else None for p in marine["hourly"]["swell_wave_period"]]
    winds = wind["hourly"]["windspeed_10m"]   # km/u
    dirs = wind["hourly"]["winddirection_10m"]
    start_date = dt.date.fromisoformat(hrs[0][:10])

    data = []
    for d in range(3):
        date = start_date + dt.timedelta(days=d)
        # indices 08–20u
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

        # Windrichting t.o.v. kustlijn (voor dag-type)
        def angle_diff(a, b):
            return abs((a - b + 180) % 360 - 180)

        if angle_diff(avg_dir, 270) <= 60:
            day_wind_type = "onshore"
        elif angle_diff(avg_dir, 90) <= 60:
            day_wind_type = "offshore"
        else:
            day_wind_type = "sideshore"

        # Dag-score (referentie)
        day_score = score_for_conditions(avg_wave, avg_per, avg_wind, day_wind_type)

        # Uur-scores + lokale windrichtingen
        hourly_scores = {}
        for h in range(8, 20):
            h_idx = [i for i, t in enumerate(hrs) if t.startswith(str(date)) and int(t[11:13]) == h]
            if not h_idx:
                continue
            Hw = [waves[i] for i in h_idx if waves[i] is not None]
            Tp = [periods[i] for i in h_idx if periods[i] is not None]
            Wv = [winds[i] for i in h_idx if winds[i] is not None]
            Dv = [dirs[i] for i in h_idx if dirs[i] is not None]
            if not (Hw and Tp and Wv and Dv):
                continue
            h_wave = stats.mean(Hw)
            h_per = stats.mean(Tp)
            h_wind = stats.mean(Wv)
            h_dir = stats.mean(Dv)

            # richting per uur
            if angle_diff(h_dir, 270) <= 60:
                h_type = "onshore"
            elif angle_diff(h_dir, 90) <= 60:
                h_type = "offshore"
            else:
                h_type = "sideshore"

            s = score_for_conditions(h_wave, h_per, h_wind, h_type)
            hourly_scores[h] = s

        # drempel voor "goed" uur (combinatie absolute + relatieve drempel)
        if hourly_scores:
            max_hour_score = max(hourly_scores.values())
        else:
            max_hour_score = day_score
        base_threshold = 1.0
        rel_threshold = 0.7 * max(day_score, 0.0001)
        threshold = max(base_threshold, rel_threshold)

        # clusters bouwen van goede uren
        good_hours = sorted([h for h, s in hourly_scores.items() if s >= threshold])
        clusters = []
        if good_hours:
            start = prev = good_hours[0]
            scores_cluster = [hourly_scores[start]]
            for h in good_hours[1:]:
                if h == prev + 1:
                    scores_cluster.append(hourly_scores[h])
                    prev = h
                else:
                    clusters.append({
                        "start": start,
                        "end": prev + 1,
                        "score": stats.mean(scores_cluster),
                    })
                    start = prev = h
                    scores_cluster = [hourly_scores[h]]
            clusters.append({
                "start": start,
                "end": prev + 1,
                "score": stats.mean(scores_cluster),
            })

        # beste cluster-score voor kleur
        if clusters:
            best_cluster_score = max(c["score"] for c in clusters)
        else:
            best_cluster_score = day_score

        # Kleur op basis van beste cluster + energie
        if best_cluster_score >= 2.3 and energy >= 2.5:
            color = "🟢"
        elif best_cluster_score >= 1.0:
            color = "🟠"
        else:
            color = "🔴"

        data.append({
            "date": date,
            "color": color,
            "wind": f"{min_w:.0f}–{max_w:.0f} km/h {day_wind_type}",
            "swell": f"{min_h:.1f}–{max_h:.1f} m — periode {round(min_t)}–{round(max_t)} s (~{energy:.1f} kJ/m)",
            "avg_wave": avg_wave,
            "avg_per": avg_per,
            "avg_wind": avg_wind,
            "energy": energy,
            "wind_type": day_wind_type,
            "day_score": day_score,
            "best_cluster_score": best_cluster_score,
            "clusters": clusters,
        })
    return data

# -----------------------
# Best-moment tekst uit clusters
# -----------------------
def best_moment_text(day):
    clusters = day.get("clusters") or []
    if not clusters:
        return "Geen duidelijk goed moment vandaag."

    # Sorteer clusters op score (hoogste eerst)
    clusters_sorted = sorted(clusters, key=lambda c: c["score"], reverse=True)
    top = clusters_sorted[0]
    moments = [f"{top['start']:02d}–{top['end']:02d}u"]

    # eventueel tweede goede cluster als die bijna zo goed is
    if len(clusters_sorted) > 1:
        second = clusters_sorted[1]
        if second["score"] >= 0.85 * top["score"]:
            moments.append(f"{second['start']:02d}–{second['end']:02d}u")

    if len(moments) == 1:
        return moments[0]
    else:
        return " en ".join(moments)

# -----------------------
# AI-tekst (gekoppeld aan kleur + labels)
# -----------------------
def ai_text(day):
    kleur = day["color"]
    wave = round(day["avg_wave"], 1)
    period = round(day["avg_per"])
    wind = round(day["avg_wind"])
    dir_type = day.get("wind_type", "onbekend")
    energy = round(day["energy"], 1)

    # Labels
    if wave < 0.6:
        wave_quality = "klein"
    elif wave < 0.9:
        wave_quality = "oké"
    elif wave < 1.3:
        wave_quality = "redelijk"
    else:
        wave_quality = "groot"

    if period < 5:
        period_quality = "kort"
    elif period < 6:
        period_quality = "redelijk"
    elif period < 8:
        period_quality = "lang"
    else:
        period_quality = "mooi lang"

    if wind > 30:
        wind_quality = "hard"
    elif wind > 20:
        wind_quality = "matig"
    elif wind > 12:
        wind_quality = "prima"
    else:
        wind_quality = "zacht"

    if dir_type == "offshore":
        wind_style = "clean"
    elif dir_type == "onshore":
        wind_style = "rommelig"
    else:
        wind_style = "neutraal"

    prompt = f"""
Je bent een ervaren Nederlandse surfcoach die korte, nuchtere updates schrijft over de surf aan de Noordzee (Scheveningen).
Je krijgt een kleurbeoordeling en een paar labels over hoogte, periode en wind. Schrijf op basis daarvan één korte, natuurlijke zin
(max 12 woorden) over hoe de surfdag aanvoelt.

### Data
- Kleur: {kleur}
- Golfhoogte: {wave} m ({wave_quality})
- Periode: {period} s ({period_quality})
- Wind: {wind} km/u ({wind_quality}, {wind_style})
- Energie: {energy} kJ/m

### Richtlijnen
- Gebruik de kleur als sentiment:
  - 🟢 = goed, clean, krachtig, mooi venster, duidelijk surfbaar
  - 🟠 = oké, surfbaar maar wat rommelig of matig
  - 🔴 = slecht, weinig kracht, korte swell of te veel wind
- Schrijf alsof je een maat appt over het surfweer.
- Nuchtere spreektaal, geen poëzie, geen marketing.
- Gebruik woorden als clean, rommelig, blown out, prima, matig, krachtig, klein, deining.
- Geen cijfers, geen tijden, geen opsommingen.
- Maximaal één zin.
- Geef alleen de zin, zonder uitleg of aanhalingstekens.

### Voorbeelden
- 🔴: Kleine rommelige golven met weinig kracht vandaag.
- 🔴: Korte onrustige swell met harde onshore wind.
- 🟠: Surfbaar venster, maar wat rommelige lijnen en windgevoelig.
- 🟠: Aardig te doen, maar niet super clean of krachtig.
- 🟢: Clean lijntje met prima hoogte en lange sets.
- 🟢: Mooie krachtige deining, zeker de moeite waard.
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
    best_today = best_moment_text(today)

    lines = [
        f"📅 {label}",
        f"{today['color']} {ai_part}",
        f"Wind: {today['wind']}",
        f"Swell: {today['swell']}",
        f"👉 Beste moment: {best_today}",
        "",
    ]

    def color_word(c):
        if c == "🟢":
            return "goed"
        if c == "🟠":
            return "oké"
        return "slecht"

    if len(summary) > 1:
        t = summary[1]
        best_t = best_moment_text(t)
        lines.append(
            f"{t['color']} Morgen: rond {best_t} lijkt het {color_word(t['color'])}, "
            f"met ~{t['avg_wave']:.1f} m en {round(t['avg_per'])} s swell."
        )
    if len(summary) > 2:
        o = summary[2]
        best_o = best_moment_text(o)
        lines.append(
            f"{o['color']} Overmorgen: rond {best_o} waarschijnlijk {color_word(o['color'])}, "
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
