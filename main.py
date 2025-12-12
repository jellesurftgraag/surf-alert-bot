import os, json, datetime as dt, requests, http.client, statistics as stats, re

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

MODEL_ID = "openai/gpt-oss-120b"  # <--- hier kun je later switchen

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
    """
    Surf-score per uur/dagdeel/dag.
    Principes:
    - <0.4m is praktisch niet surfbaar (cap).
    - Periode helpt, maar redt microgolf niet.
    - Harde onshore wordt stevig afgestraft.
    """
    if H is None or T is None or W is None:
        return 0.0

    score = 0.0

    # Hoogte
    if H < 0.4:
        height_score = 0.0
    elif 0.4 <= H < 0.6:
        height_score = 0.3
    elif 0.6 <= H < 0.8:
        height_score = 0.6
    elif 0.8 <= H < 1.2:
        height_score = 1.0
    else:
        height_score = 1.2
    score += height_score

    # Periode
    if T >= 8:
        score += 1.0
    elif 7 <= T < 8:
        score += 0.8
    elif 6 <= T < 7:
        score += 0.5
    elif 5 <= T < 6:
        score += 0.3
    else:
        score += 0.0

    # Windkracht
    if W <= 10:
        score += 1.0
    elif 10 < W <= 18:
        score += 0.7
    elif 18 < W <= 26:
        score += 0.3
    else:
        score += 0.0

    # Windrichting
    if dir_type == "offshore":
        score += 0.4
        if W > 25:
            score -= 0.2
    elif dir_type == "onshore":
        if W > 28:
            score -= 0.8
        elif W > 20:
            score -= 0.5
        else:
            score -= 0.2
    else:  # sideshore
        if W > 28:
            score -= 0.2

    # Caps op te kleine hoogte
    if H < 0.4:
        score = min(score, 0.5)
    elif H < 0.6:
        score = min(score, 1.0)

    return score

# -----------------------
# Samenvatten + uur-scores + clusters + dagdelen
# -----------------------
def summarize_forecast(marine, wind):
    hrs = marine["hourly"]["time"]

    # Correcties voor realistischer surfgevoel
    waves = [h * 1.4 if h is not None else None for h in marine["hourly"]["wave_height"]]
    periods = [(p + 1.0) if p is not None else None for p in marine["hourly"]["swell_wave_period"]]
    winds = wind["hourly"]["windspeed_10m"]   # km/u
    dirs = wind["hourly"]["winddirection_10m"]

    start_date = dt.date.fromisoformat(hrs[0][:10])

    def angle_diff(a, b):
        return abs((a - b + 180) % 360 - 180)

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
        energy = 0.49 * avg_wave**2 * avg_per

        # Dag windtype
        if angle_diff(avg_dir, 270) <= 60:
            day_wind_type = "onshore"
        elif angle_diff(avg_dir, 90) <= 60:
            day_wind_type = "offshore"
        else:
            day_wind_type = "sideshore"

        day_score = score_for_conditions(avg_wave, avg_per, avg_wind, day_wind_type)

        # Uur-scores + meta
        hourly_scores = {}
        hourly_meta = {}
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

            if angle_diff(h_dir, 270) <= 60:
                h_type = "onshore"
            elif angle_diff(h_dir, 90) <= 60:
                h_type = "offshore"
            else:
                h_type = "sideshore"

            s = score_for_conditions(h_wave, h_per, h_wind, h_type)
            hourly_scores[h] = s
            hourly_meta[h] = {
                "wave": h_wave,
                "period": h_per,
                "wind": h_wind,
                "dir": h_dir,
                "wind_type": h_type,
            }

        # drempel voor "goed" uur
        base_threshold = 1.0
        rel_threshold = 0.7 * max(day_score, 0.0001)
        threshold = max(base_threshold, rel_threshold)

        # clusters van goede uren
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
                    clusters.append({"start": start, "end": prev + 1, "score": stats.mean(scores_cluster)})
                    start = prev = h
                    scores_cluster = [hourly_scores[h]]
            clusters.append({"start": start, "end": prev + 1, "score": stats.mean(scores_cluster)})

        best_cluster_score = max((c["score"] for c in clusters), default=day_score)

        # Kleur dag (zelfde als eerder)
        if best_cluster_score >= 2.3 and energy >= 2.5:
            day_color = "🟢"
        elif best_cluster_score >= 1.0:
            day_color = "🟠"
        else:
            day_color = "🔴"

        # Dagdelen
        dayparts_def = {"Ochtend": (8, 12), "Middag": (12, 16), "Avond": (16, 20)}
        dayparts = {}
        for naam, (h_start, h_end) in dayparts_def.items():
            part_hours = [h for h in range(h_start, h_end) if h in hourly_meta]
            if not part_hours:
                continue

            pwaves = [hourly_meta[h]["wave"] for h in part_hours]
            pper = [hourly_meta[h]["period"] for h in part_hours]
            pwinds = [hourly_meta[h]["wind"] for h in part_hours]
            pdirs = [hourly_meta[h]["dir"] for h in part_hours]

            p_wave_avg = stats.mean(pwaves)
            p_per_avg = stats.mean(pper)
            p_wind_avg = stats.mean(pwinds)
            p_dir_avg = stats.mean(pdirs)

            if angle_diff(p_dir_avg, 270) <= 60:
                p_type = "onshore"
            elif angle_diff(p_dir_avg, 90) <= 60:
                p_type = "offshore"
            else:
                p_type = "sideshore"

            p_score = score_for_conditions(p_wave_avg, p_per_avg, p_wind_avg, p_type)
            p_energy = 0.49 * p_wave_avg**2 * p_per_avg

            if p_score >= 2.3 and p_energy >= 2.5:
                p_color = "🟢"
            elif p_score >= 1.0:
                p_color = "🟠"
            else:
                p_color = "🔴"

            dayparts[naam] = {
                "color": p_color,
                "h_min": min(pwaves),
                "h_max": max(pwaves),
                "t_min": min(pper),
                "t_max": max(pper),
                "wind_avg": p_wind_avg,
                "wind_type": p_type,
            }

        data.append({
            "date": date,
            "color": day_color,
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
            "dayparts": dayparts,
        })
    return data

# -----------------------
# Best-moment tekst (vandaag): 1-2 beste clusters, zoals je al had
# -----------------------
def best_moment_text(day):
    clusters = day.get("clusters") or []
    if not clusters:
        return "Geen duidelijk goed moment vandaag."

    clusters_sorted = sorted(clusters, key=lambda c: c["score"], reverse=True)
    top = clusters_sorted[0]
    moments = [f"{top['start']:02d}–{top['end']:02d}u"]

    if len(clusters_sorted) > 1:
        second = clusters_sorted[1]
        if second["score"] >= 0.85 * top["score"]:
            moments.append(f"{second['start']:02d}–{second['end']:02d}u")

    return " en ".join(moments)

# -----------------------
# Natuurlijke venstertekst (morgen/overmorgen) - menselijker
# -----------------------
def natural_window_phrase(day, nearly_all_day_hours=9):
    """
    Gebruikt de clusters maar praat menselijk:
    - totaal goede uren >= 9: 'vrijwel de hele dag'
    - anders: 'vooral HH–HHu' + (optioneel) 'later nog HH–HHu'
    """
    clusters = day.get("clusters") or []
    if not clusters:
        return "geen duidelijk goed venster"

    # merge-achtige logica: tel unieke uren in good vensters
    covered = set()
    for c in clusters:
        for h in range(c["start"], c["end"]):
            covered.add(h)
    total = len(covered)

    # kies "hoofdvenster" = langste; bij gelijk: hoogste score
    clusters_sorted = sorted(clusters, key=lambda c: ((c["end"] - c["start"]), c["score"]), reverse=True)
    main = clusters_sorted[0]
    main_len = main["end"] - main["start"]

    if total >= nearly_all_day_hours:
        phrase = "vrijwel de hele dag"
        # alleen extra noemen als er echt een los, duidelijk blok is dat niet in main zit
        extras = [c for c in clusters if c is not main and (c["end"] - c["start"]) >= 2 and c["score"] >= 0.85 * main["score"]]
        extras = sorted(extras, key=lambda c: c["start"])
        if extras:
            e = extras[0]
            phrase += f", met een extra piek {e['start']:02d}–{e['end']:02d}u"
        return phrase

    phrase = f"vooral {main['start']:02d}–{main['end']:02d}u"

    # tweede blok: beste van de rest als het echt “iets toevoegt”
    rest = [c for c in clusters if c is not main]
    rest = sorted(rest, key=lambda c: c["score"], reverse=True)
    if rest:
        second = rest[0]
        second_len = second["end"] - second["start"]
        if second_len >= 1 and second["score"] >= 0.85 * main["score"]:
            # formulering: "later nog" / "en ook"
            if second["start"] > main["end"]:
                phrase += f", later nog {second['start']:02d}–{second['end']:02d}u"
            else:
                phrase += f" en ook {second['start']:02d}–{second['end']:02d}u"

    return phrase

# -----------------------
# Helpers voor kleuren
# -----------------------
def color_word(c):
    if c == "🟢":
        return "goed"
    if c == "🟠":
        return "oké"
    return "slecht"

def color_square(c):
    if c == "🟢":
        return "🟩"
    if c == "🟠":
        return "🟧"
    if c == "🔴":
        return "🟥"
    return c

# -----------------------
# Coach-AI: 1 zin, maar echt gebaseerd op surf-logica
# -----------------------
def _fallback_coach_line(day):
    c = day["color"]
    w = day["avg_wave"]
    t = day["avg_per"]
    wind = day["avg_wind"]
    wt = day.get("wind_type", "sideshore")

    # simpele maar betrouwbare fallback (geen tijden/cijfers in tekst)
    if w < 0.45:
        return "Klein en weinig power, vooral even kijken voor een longboard."
    if wt == "offshore" and wind <= 18 and t >= 6:
        return "Best netjes door offshore, met wat power in de sets."
    if wt == "onshore" and wind >= 20:
        return "Wind maakt het snel rommelig, vooral losse chop en weinig lijn."
    if t < 5:
        return "Korte periode, dus het voelt wat rommelig en minder krachtig."
    if c == "🟢":
        return "Prima surfdag, met genoeg lijn en af en toe een mooie set."
    if c == "🟠":
        return "Surfbaar, maar verwacht wisselende lijnen en wat rommel."
    return "Lastige dag: weinig lijn of te veel wind voor echt lekker surfen."

def _sanitize_ai_line(text: str) -> str:
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"\s+", " ", t)
    # geen cijfers/tijden in de coach-zin
    if re.search(r"\d", t):
        return ""
    # geen opsomming / meerdere regels
    if "\n" in t:
        t = t.split("\n")[0].strip()
    # te kort/leeg
    if len(t) < 6:
        return ""
    return t

def ai_text(day):
    """
    1 coach-zin, zonder cijfers/tijden, maar wél met surf-logica:
    windtype + windkracht -> clean/rommelig
    periode -> power
    hoogte -> size
    """
    kleur = day["color"]

    # dagdelen compacte context
    dp = day.get("dayparts", {})
    def dp_str(name):
        p = dp.get(name)
        if not p:
            return ""
        return f"{name}: {p['h_min']:.1f}-{p['h_max']:.1f}m, {round(p['t_min'])}-{round(p['t_max'])}s, {round(p['wind_avg'])}km/u {p['wind_type']}"
    dayparts_compact = "; ".join([s for s in [dp_str("Ochtend"), dp_str("Middag"), dp_str("Avond")] if s])

    best = best_moment_text(day)

    prompt = f"""
Je bent een Nederlandse surfcoach voor Scheveningen.
Schrijf EXACT 1 zin (8–16 woorden), alsof je een vriend appt: hoe voelt de surf vandaag?

Je krijgt data. Je zin moet logisch kloppen met:
- Hoogte + periode (power)
- Windtype + windkracht (clean vs rommelig)
- Kleur (stoplicht)
- Beste momenten (mag je noemen als 'ochtend'/'middag', maar GEEN tijden/cijfers)

DATA (intern, NIET letterlijk herhalen):
- Stoplicht: {kleur}
- Gemiddeld: hoogte {day['avg_wave']:.1f}m, periode {day['avg_per']:.0f}s, wind {day['avg_wind']:.0f}km/u, windtype {day.get('wind_type','sideshore')}
- Dagdelen: {dayparts_compact}
- Beste momenten (tijden): {best}

REGELS:
- Geen cijfers, geen tijden, geen meet-woorden (geen “m”, “s”, “km/u”, “kJ”).
- Geen opsommingen.
- Vermijd: "om hier", "een beetje zee", "matige golf".
- Gebruik natuurlijk surf-taal: clean, rommelig, chop, lijnen, sets, power, longboard.
- Als het vrijwel de hele dag hetzelfde is: zeg dat het “de hele dag” ongeveer zo blijft.
- Output: alleen die ene zin.
"""

    body = json.dumps({
        "model": MODEL_ID,
        "messages": [
            {"role": "system", "content": "Je bent een nuchtere Nederlandse surfcoach. Kort, menselijk, geen cijfers."},
            {"role": "user", "content": prompt.strip()},
        ],
        "temperature": 0.55,     # iets strakker → minder rare zinnen
        "max_tokens": 60,
    })

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    conn = http.client.HTTPSConnection("api.groq.com")
    conn.request("POST", "/openai/v1/chat/completions", body, headers)
    res = conn.getresponse()
    raw = res.read().decode()

    if res.status != 200:
        return _fallback_coach_line(day)

    try:
        txt = json.loads(raw)["choices"][0]["message"]["content"]
    except Exception:
        return _fallback_coach_line(day)

    txt = _sanitize_ai_line(txt)
    if not txt:
        return _fallback_coach_line(day)
    return txt

# -----------------------
# Bericht samenstellen
# -----------------------
def build_message(spot, summary):
    today = summary[0]
    d = today["date"]
    label = f"{DAGEN[d.weekday()]} {d.day} {MAANDEN[d.month-1]}"

    coach_line = ai_text(today)
    best_today = best_moment_text(today)

    lines = []
    lines.append(f"📅 {label}")
    lines.append(f"{color_square(today['color'])} {coach_line}")
    lines.append("")

    # dagdelen
    dayparts = today.get("dayparts") or {}
    for naam in ["Ochtend", "Middag", "Avond"]:
        part = dayparts.get(naam)
        if not part:
            continue
        c = part["color"]
        lines.append(
            f"{c} {naam}: {part['h_min']:.1f}–{part['h_max']:.1f} m / {round(part['t_min'])}–{round(part['t_max'])} s — ~{round(part['wind_avg'])} km/u {part['wind_type']}"
        )

    lines.append("")

    # beste momenten
    if "Geen duidelijk goed moment" in best_today:
        lines.append("👉 Beste moment: geen echt duidelijk venster vandaag.")
    else:
        if " en " in best_today:
            lines.append(f"👉 Beste momenten: {best_today}")
        else:
            lines.append(f"👉 Beste moment: {best_today}")

    lines.append("")

    # Morgen / overmorgen
    if len(summary) > 1:
        t = summary[1]
        phrase_t = natural_window_phrase(t, nearly_all_day_hours=9)
        if phrase_t == "geen duidelijk goed venster":
            lines.append(
                f"{color_square(t['color'])} Morgen: geen duidelijk goed venster, met ~{t['avg_wave']:.1f} m en {round(t['avg_per'])} s swell."
            )
        else:
            # menselijker: geen “rond”
            lines.append(
                f"{color_square(t['color'])} Morgen: {phrase_t} {color_word(t['color'])} surf, met ~{t['avg_wave']:.1f} m en {round(t['avg_per'])} s swell."
            )

    if len(summary) > 2:
        o = summary[2]
        phrase_o = natural_window_phrase(o, nearly_all_day_hours=9)
        if phrase_o == "geen duidelijk goed venster":
            lines.append(
                f"{color_square(o['color'])} Overmorgen: geen duidelijk goed venster, met ~{o['avg_wave']:.1f} m en {round(o['avg_per'])} s swell."
            )
        else:
            lines.append(
                f"{color_square(o['color'])} Overmorgen: {phrase_o} {color_word(o['color'])} surf, met ~{o['avg_wave']:.1f} m en {round(o['avg_per'])} s swell."
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
