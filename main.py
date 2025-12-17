import os, json, datetime as dt, requests, http.client, statistics as stats, re

# =======================
# Instellingen
# =======================
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MODEL_ID = "openai/gpt-oss-120b"  # makkelijk te wisselen

SPOT = {"name": "Scheveningen Pier", "lat": 52.109, "lon": 4.276}
TZ = "Europe/Amsterdam"

DAGEN = ["Maandag", "Dinsdag", "Woensdag", "Donderdag", "Vrijdag", "Zaterdag", "Zondag"]
MAANDEN = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]

DAYPARTS_DEF = {
    "Ochtend": (8, 12),
    "Middag": (12, 16),
    "Avond": (16, 20),
}

# =======================
# Nieuwe harde regels
# =======================
PERIOD_ORANGE_MIN_S = 6  # <6s is altijd rood, vanaf 6s mag oranje (of groen)


# =======================
# Data ophalen
# =======================
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


def angle_diff(a, b):
    return abs((a - b + 180) % 360 - 180)


def wind_type_from_dir(direction_deg):
    if angle_diff(direction_deg, 270) <= 60:
        return "onshore"
    if angle_diff(direction_deg, 90) <= 60:
        return "offshore"
    return "sideshore"


# =======================
# Helpers: netjes formatteren
# =======================
def _round_safe(x, ndigits=0):
    if x is None:
        return None
    return round(float(x), ndigits)


def format_range(lo, hi, *, ndigits=0, unit="", dash="–"):
    """
    Als lo en hi gelijk zijn na afronden, toon één waarde.
    Voorbeelden:
      5.0, 5.0 -> "5 s"
      5.0, 6.0 -> "5–6 s"
      0.4, 0.4 -> "0.4 m"
    """
    if lo is None or hi is None:
        return ""
    lo_r = _round_safe(lo, ndigits)
    hi_r = _round_safe(hi, ndigits)
    if lo_r == hi_r:
        return f"{lo_r:.{ndigits}f}{(' ' + unit) if unit else ''}"
    return f"{lo_r:.{ndigits}f}{dash}{hi_r:.{ndigits}f}{(' ' + unit) if unit else ''}"


def period_is_short(T):
    return (T is not None) and (T < PERIOD_ORANGE_MIN_S)


# =======================
# Surf score
# =======================
def score_for_conditions(H, T, W, dir_type):
    """
    Score is leidend voor clusters en stoplicht.
    Kort samengevat:
    - Te klein blijft te klein (caps)
    - Periode helpt, maar redt microgolf niet
    - Harde onshore straft af

    Let op: kleur wordt later alsnog hard afgekapt bij T < 6s.
    """
    if H is None or T is None or W is None:
        return 0.0

    score = 0.0

    # Hoogte
    if H < 0.4:
        score += 0.0
    elif H < 0.6:
        score += 0.3
    elif H < 0.8:
        score += 0.6
    elif H < 1.2:
        score += 1.0
    else:
        score += 1.2

    # Periode
    if T >= 8:
        score += 1.0
    elif T >= 7:
        score += 0.8
    elif T >= 6:
        score += 0.5
    elif T >= 5:
        score += 0.3

    # Windkracht
    if W <= 10:
        score += 1.0
    elif W <= 18:
        score += 0.7
    elif W <= 26:
        score += 0.3

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


def color_from_score_energy(best_score, energy):
    if best_score >= 2.3 and energy >= 2.5:
        return "🟢"
    if best_score >= 1.0:
        return "🟠"
    return "🔴"


def enforce_period_rule(color, T_rep):
    """
    Harde regel: onder 6 seconden is altijd rood.
    """
    if period_is_short(T_rep):
        return "🔴"
    return color


def color_square(c):
    if c == "🟢":
        return "🟩"
    if c == "🟠":
        return "🟧"
    if c == "🔴":
        return "🟥"
    return "🟧"


# =======================
# Forecast samenvatten
# =======================
def summarize_forecast(marine, wind):
    hrs = marine["hourly"]["time"]

    waves_raw = marine["hourly"]["wave_height"]
    periods_raw = marine["hourly"]["swell_wave_period"]
    winds_raw = wind["hourly"]["windspeed_10m"]
    dirs_raw = wind["hourly"]["winddirection_10m"]

    # jouw kalibratie
    waves = [(h * 1.4) if h is not None else None for h in waves_raw]
    periods = [(p + 1.0) if p is not None else None for p in periods_raw]
    winds = [w if w is not None else None for w in winds_raw]
    dirs = [d if d is not None else None for d in dirs_raw]

    start_date = dt.date.fromisoformat(hrs[0][:10])
    out = []

    for d in range(3):
        date = start_date + dt.timedelta(days=d)

        idx = [
            i for i, t in enumerate(hrs)
            if t.startswith(str(date)) and 8 <= int(t[11:13]) < 20
        ]
        if not idx:
            continue

        wv = [waves[i] for i in idx if waves[i] is not None]
        pr = [periods[i] for i in idx if periods[i] is not None]
        ws = [winds[i] for i in idx if winds[i] is not None]
        wd = [dirs[i] for i in idx if dirs[i] is not None]
        if not (wv and pr and ws and wd):
            continue

        avg_wave = stats.mean(wv)
        avg_per = stats.mean(pr)
        # representatief voor harde periode-regel: median is rustiger
        rep_per = stats.median(pr)

        avg_wind = stats.mean(ws)
        avg_dir = stats.mean(wd)
        energy = 0.49 * (avg_wave ** 2) * avg_per

        day_wind_type = wind_type_from_dir(avg_dir)
        day_score = score_for_conditions(avg_wave, avg_per, avg_wind, day_wind_type)

        hourly_scores = {}
        hourly_meta = {}

        for h in range(8, 20):
            ids = [
                i for i, t in enumerate(hrs)
                if t.startswith(str(date)) and int(t[11:13]) == h
            ]
            if not ids:
                continue

            i0 = ids[0]
            hw = waves[i0]
            tp = periods[i0]
            wv_ = winds[i0]
            dr = dirs[i0]
            if None in (hw, tp, wv_, dr):
                continue

            ht = wind_type_from_dir(dr)
            s = score_for_conditions(hw, tp, wv_, ht)

            hourly_scores[h] = s
            hourly_meta[h] = {
                "wave": hw,
                "period": tp,
                "wind": wv_,
                "dir": dr,
                "wind_type": ht,
                "score": s,
            }

        base_threshold = 1.0
        rel_threshold = 0.7 * max(day_score, 0.0001)
        threshold = max(base_threshold, rel_threshold)

        # Clusters blijven score-based, maar we zullen later "korte-periode dagen" niet promoten als venster
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

        best_cluster_score = max((c["score"] for c in clusters), default=day_score)
        day_color = color_from_score_energy(best_cluster_score, energy)
        day_color = enforce_period_rule(day_color, rep_per)

        # Dagdelen
        dayparts = {}
        for name, (h0, h1) in DAYPARTS_DEF.items():
            hs = [h for h in range(h0, h1) if h in hourly_meta]
            if not hs:
                continue

            pw = [hourly_meta[h]["wave"] for h in hs]
            pt = [hourly_meta[h]["period"] for h in hs]
            pwind = [hourly_meta[h]["wind"] for h in hs]
            pdir = [hourly_meta[h]["dir"] for h in hs]

            p_wave_avg = stats.mean(pw)
            p_per_avg = stats.mean(pt)
            p_per_rep = stats.median(pt)

            p_wind_avg = stats.mean(pwind)
            p_dir_avg = stats.mean(pdir)

            p_type = wind_type_from_dir(p_dir_avg)
            p_score = score_for_conditions(p_wave_avg, p_per_avg, p_wind_avg, p_type)
            p_energy = 0.49 * (p_wave_avg ** 2) * p_per_avg
            p_color = color_from_score_energy(p_score, p_energy)
            p_color = enforce_period_rule(p_color, p_per_rep)

            dayparts[name] = {
                "color": p_color,
                "h_min": min(pw),
                "h_max": max(pw),
                "t_min": min(pt),
                "t_max": max(pt),
                "t_rep": p_per_rep,
                "wind_avg": p_wind_avg,
                "wind_type": p_type,
                "score": p_score,
            }

        out.append({
            "date": date,
            "color": day_color,
            "avg_wave": avg_wave,
            "avg_per": avg_per,
            "rep_per": rep_per,
            "avg_wind": avg_wind,
            "wind_type": day_wind_type,
            "energy": energy,
            "day_score": day_score,
            "threshold": threshold,
            "clusters": clusters,
            "dayparts": dayparts,
        })

    return out


# =======================
# Vensters en natuurlijke tekst
# =======================
def best_moment_text(day):
    clusters = day.get("clusters") or []
    if not clusters:
        return ""

    clusters_sorted = sorted(
        clusters,
        key=lambda c: (c["score"], (c["end"] - c["start"])),
        reverse=True
    )
    top = clusters_sorted[0]
    moments = [f"{top['start']:02d}–{top['end']:02d}u"]

    if len(clusters_sorted) > 1:
        second = clusters_sorted[1]
        if second["score"] >= 0.85 * top["score"]:
            moments.append(f"{second['start']:02d}–{second['end']:02d}u")

    return " en ".join(moments)


def natural_window_phrase(day, nearly_all_day_hours=9):
    clusters = day.get("clusters") or []
    if not clusters:
        return "geen duidelijk goed venster"

    covered = set()
    for c in clusters:
        for h in range(c["start"], c["end"]):
            covered.add(h)
    total = len(covered)

    clusters_sorted = sorted(clusters, key=lambda c: ((c["end"] - c["start"]), c["score"]), reverse=True)
    main = clusters_sorted[0]

    if total >= nearly_all_day_hours:
        return "vrijwel de hele dag"

    phrase = f"vooral {main['start']:02d}–{main['end']:02d}u"

    rest = [c for c in clusters_sorted[1:]]
    if rest:
        second = rest[0]
        if second["score"] >= 0.85 * main["score"]:
            if second["start"] >= main["end"]:
                phrase += f", later nog {second['start']:02d}–{second['end']:02d}u"
            else:
                phrase += f" en ook {second['start']:02d}–{second['end']:02d}u"

    return phrase


def best_moments_line_for_today(day):
    # Als de periode te kort is, niet doen alsof er een echt "beste" venster is
    if period_is_short(day.get("rep_per", day.get("avg_per"))):
        return "👉 Beste moment: geen echt venster (te korte periode, vooral rommel)"

    phrase = natural_window_phrase(day, nearly_all_day_hours=9)
    if phrase == "vrijwel de hele dag":
        return "👉 Beste momenten: de hele dag vrij consistent (08–20u)"

    best = best_moment_text(day)
    if not best:
        return "👉 Beste moment: geen duidelijk venster vandaag"
    if " en " in best:
        return f"👉 Beste momenten: {best}"
    return f"👉 Beste moment: {best}"


# =======================
# Coach (AI) met fallback
# =======================
def _sanitize_one_line(text):
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"\s+", " ", t)
    if "\n" in t:
        t = t.split("\n")[0].strip()
    if re.search(r"\d", t):
        return ""
    if len(t) < 8:
        return ""
    return t


def _fallback_coach_line(day):
    w, t, wind = day["avg_wave"], day["avg_per"], day["avg_wind"]
    wt = day.get("wind_type", "sideshore")

    if period_is_short(day.get("rep_per", t)):
        if wt == "onshore" and wind >= 12:
            return "Te korte periode en wind erbij, vooral rommelig en weinig lijn."
        return "Korte periode, dus vooral rommelig en weinig echte power."

    if w < 0.45:
        return "Klein en weinig power, longboard is je beste kans."
    if w < 0.7 and t <= 6:
        return "Klein en kort, longboard of funboard werkt het lekkerst."
    if wt == "onshore" and wind >= 18:
        return "Onshore maakt het rommelig, vooral voor beginners wat taai."
    if wt == "offshore" and wind <= 18 and t >= 6:
        return "Netter door offshore, lekker voor een ervaren shortboard sessie."
    return "Surfbaar, maar verwacht wisselende lijnen en wat rommel."


def _ai_coach(day, purpose="today"):
    payload = {
        "stoplicht": day["color"],
        "avg": {
            "wave_m": round(day["avg_wave"], 2),
            "period_s": round(day["avg_per"], 1),
            "wind_kmh": round(day["avg_wind"], 1),
            "wind_type": day.get("wind_type", "sideshore"),
        },
        "dayparts": {
            k: {
                "color": v["color"],
                "wave_min": round(v["h_min"], 2),
                "wave_max": round(v["h_max"], 2),
                "period_min": round(v["t_min"], 1),
                "period_max": round(v["t_max"], 1),
                "wind_avg": round(v["wind_avg"], 1),
                "wind_type": v["wind_type"],
            } for k, v in (day.get("dayparts") or {}).items()
        },
        "best_phrase": natural_window_phrase(day, nearly_all_day_hours=9),
    }

    if purpose == "future":
        instruction = (
            "Schrijf 1 korte zin (7-14 woorden) als surfcoach voor morgen/overmorgen. "
            "Geen cijfers of tijden. "
            "Zeg iets over wind (clean/rommelig) en power (periode/hoogte). "
            "Als de periode kort is, wees duidelijk dat het vooral rommelig is."
        )
    else:
        instruction = (
            "Schrijf 1 zin (8-16 woorden) als surfcoach voor vandaag. "
            "Geen cijfers of tijden of units. "
            "Je mag soms longboard/shortboard of beginner/ervaren noemen als het logisch is. "
            "Laat de zin kloppen met wind (clean/rommelig) en power (periode). "
            "Als de periode kort is, zeg niet dat het 'surfbaar' of 'prima' is."
        )

    prompt = f"""{instruction}

Context (niet letterlijk herhalen):
{json.dumps(payload, ensure_ascii=False)}
"""

    body = json.dumps({
        "model": MODEL_ID,
        "messages": [
            {"role": "system", "content": "Je bent een nuchtere Nederlandse surfcoach. Menselijk, kort, geen cijfers."},
            {"role": "user", "content": prompt.strip()},
        ],
        "temperature": 0.6,
        "max_tokens": 80,
    })

    conn = http.client.HTTPSConnection("api.groq.com")
    conn.request(
        "POST",
        "/openai/v1/chat/completions",
        body,
        {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    res = conn.getresponse()
    raw = res.read().decode()

    if res.status != 200:
        return ""

    try:
        txt = json.loads(raw)["choices"][0]["message"]["content"]
    except Exception:
        return ""

    txt = _sanitize_one_line(txt)

    # Guardrail: bij korte periode geen te positieve AI-zin
    if txt and period_is_short(day.get("rep_per", day.get("avg_per"))):
        lowered = txt.lower()
        if any(w in lowered for w in ["surfbaar", "prima", "lekker", "goed", "top"]):
            return ""
    return txt


def coach_line_today(day):
    txt = _ai_coach(day, purpose="today")
    return txt if txt else _fallback_coach_line(day)


def coach_line_future(day):
    txt = _ai_coach(day, purpose="future")
    if txt:
        return txt

    # fallback future: consistent met periode-regel
    if period_is_short(day.get("rep_per", day.get("avg_per"))):
        return "Waarschijnlijk vooral rommelig en kort, weinig echte lijnen."

    c = day["color"]
    if c == "🟢":
        return "Lijkt een prima surfdag met wat meer lijn en betere sets."
    if c == "🟠":
        return "Surfbaar, maar hou rekening met wat rommel en wisselende lijnen."
    return "Waarschijnlijk lastig: weinig power of te veel wind voor echt lekker."


# =======================
# Bericht bouwen
# =======================
def build_message(summary):
    today = summary[0]
    d = today["date"]
    label = f"{DAGEN[d.weekday()]} {d.day} {MAANDEN[d.month-1]}"

    lines = []
    lines.append(f"📅 {label}")
    lines.append(f"{color_square(today['color'])} {coach_line_today(today)}")
    lines.append("")

    # Dagdelen
    dp = today.get("dayparts") or {}
    for name in ["Ochtend", "Middag", "Avond"]:
        part = dp.get(name)
        if not part:
            continue

        h_txt = format_range(part["h_min"], part["h_max"], ndigits=1, unit="m")
        # periode als integer, maar zonder 5-5
        t_txt = format_range(part["t_min"], part["t_max"], ndigits=0, unit="s")

        lines.append(
            f"{part['color']} {name}: "
            f"{h_txt} / {t_txt} "
            f"~{round(part['wind_avg'])} km/u {part['wind_type']}"
        )

    lines.append("")
    lines.append(best_moments_line_for_today(today))
    lines.append("")

    # Morgen / overmorgen: als periode kort is, niet als "surfbaar" framen via kleur
    if len(summary) > 1:
        t = summary[1]
        phrase_t = natural_window_phrase(t, nearly_all_day_hours=9)
        lines.append(
            f"{color_square(t['color'])} Morgen: {coach_line_future(t)} "
            f"Venster: {phrase_t}, met ~{t['avg_wave']:.1f} m en {round(t['avg_per'])} s swell."
        )

    if len(summary) > 2:
        o = summary[2]
        phrase_o = natural_window_phrase(o, nearly_all_day_hours=9)
        lines.append(
            f"{color_square(o['color'])} Overmorgen: {coach_line_future(o)} "
            f"Venster: {phrase_o}, met ~{o['avg_wave']:.1f} m en {round(o['avg_per'])} s swell."
        )

    return "\n".join(lines)


# =======================
# Telegram
# =======================
def send_telegram_message(text):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=20,
    ).raise_for_status()


# =======================
# Main
# =======================
if __name__ == "__main__":
    marine, wind = get_marine(SPOT["lat"], SPOT["lon"], days=2)
    summary = summarize_forecast(marine, wind)

    if not summary:
        send_telegram_message("Geen surfdata beschikbaar vandaag.")
    else:
        message = build_message(summary)
        print("----- SURF MESSAGE START -----")
        print(message)
        print("----- SURF MESSAGE END -----")
        send_telegram_message(message)
