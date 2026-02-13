import os
import json
import re
import math
import time
import http.client
import datetime as dt
import statistics as stats
import requests

# =======================
# Config
# =======================
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MODEL_ID = "openai/gpt-oss-120b"

SPOT = {"name": "Scheveningen Pier", "lat": 52.109, "lon": 4.276}
TZ = "Europe/Amsterdam"

DAGEN = ["Maandag", "Dinsdag", "Woensdag", "Donderdag", "Vrijdag", "Zaterdag", "Zondag"]
MAANDEN = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]

DAYPARTS_DEF = {
    "Ochtend": (8, 12),
    "Middag": (12, 16),
    "Avond": (16, 20),
}

# Harde regel: onder 6s nooit oranje of groen
PERIOD_ORANGE_MIN_S = 6

# Kalibratie (bewust expliciet)
WAVE_MULT = 1.4
PERIOD_BIAS_S = 1.0

# Stabiliteit voor getoonde ranges (trim)
PERIOD_Q_LO = 0.20
PERIOD_Q_HI = 0.80

# Als getoonde periode-band te breed wordt, toon "~median (wisselend)"
PERIOD_MAX_SPREAD_FOR_BAND = 4.0

# =======================
# Run-window / verzending (08:00 NL tijd)
# =======================
SEND_AT_HOUR = 8
SEND_AT_MINUTE = 0


# =======================
# Time helpers
# =======================
def _tz_now_amsterdam():
    """
    Werkt zonder externe libs. TZ in Open-Meteo zit goed; voor scheduling is lokaal oké.
    Als je server niet in NL draait, zet TZ via env (bijv. in cron) of installeer zoneinfo usage.
    """
    return dt.datetime.now()


def wait_until_send_time(hour=SEND_AT_HOUR, minute=SEND_AT_MINUTE):
    """
    Als je dit script continu runt (bijv. systemd), wacht het tot 08:00 en stuurt dan.
    Als je cron gebruikt, zet je cron op 08:00 en is dit niet nodig (maar kan geen kwaad: het wacht dan 0 sec).
    """
    now = _tz_now_amsterdam()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now > target:
        # Als je het later op de dag runt: stuur direct (geen wachten tot morgen)
        return
    delta = (target - now).total_seconds()
    if delta > 0:
        time.sleep(delta)


# =======================
# Network helpers (retries)
# =======================
def _safe_get_json(url, params, *, timeout=20, retries=3, backoff_s=2):
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(backoff_s * (attempt + 1))
    raise RuntimeError(f"GET faalde na {retries} pogingen: {url} ({last_err})")


# =======================
# Fetch Open-Meteo
# =======================
def get_open_meteo(lat, lon, days=2):
    marine = _safe_get_json(
        "https://marine-api.open-meteo.com/v1/marine",
        params={
            "latitude": lat,
            "longitude": lon,
            "timezone": TZ,
            "hourly": "wave_height,swell_wave_period,wave_period,swell_wave_peak_period",
            "forecast_days": days + 1,
        },
        timeout=20,
        retries=3,
        backoff_s=2,
    )

    wind = _safe_get_json(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "timezone": TZ,
            "hourly": "windspeed_10m,winddirection_10m",
            "forecast_days": days + 1,
        },
        timeout=20,
        retries=3,
        backoff_s=2,
    )

    return marine, wind


# =======================
# Wind helpers
# =======================
def angle_diff(a, b):
    return abs((a - b + 180) % 360 - 180)


def wind_type_from_dir(direction_deg):
    if angle_diff(direction_deg, 270) <= 60:
        return "onshore"
    if angle_diff(direction_deg, 90) <= 60:
        return "offshore"
    return "sideshore"


def period_is_short(t):
    return (t is not None) and (t < PERIOD_ORANGE_MIN_S)


# =======================
# Stats helpers
# =======================
def quantile(values, q):
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    frac = pos - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


def robust_band(values, q_lo=PERIOD_Q_LO, q_hi=PERIOD_Q_HI):
    if not values:
        return (None, None)
    lo = quantile(values, q_lo)
    hi = quantile(values, q_hi)
    if lo is None or hi is None:
        return (min(values), max(values))
    return (lo, hi)


def trend_label(values):
    if not values or len(values) < 6:
        return "onzeker"
    mid = len(values) // 2
    a = values[:mid]
    b = values[mid:]
    if not a or not b:
        return "onzeker"
    ma = stats.median(a)
    mb = stats.median(b)
    diff = mb - ma

    if abs(diff) < 0.4:
        return "stabiel"
    if diff > 0:
        return "stijgend"
    return "dalend"


# =======================
# Format helpers
# =======================
def fmt_range(lo, hi, ndigits=0, unit=""):
    if lo is None or hi is None:
        return ""
    lo_r = round(float(lo), ndigits)
    hi_r = round(float(hi), ndigits)
    if lo_r == hi_r:
        return f"{lo_r:.{ndigits}f}{(' ' + unit) if unit else ''}"
    return f"{lo_r:.{ndigits}f}–{hi_r:.{ndigits}f}{(' ' + unit) if unit else ''}"


def fmt_period_band(lo, hi, unit="s", median=None):
    """
    - Normaal: integer band (floor/ceil).
    - Als band te breed wordt: "~median s (wisselend)".
    """
    if lo is None or hi is None:
        return ""
    lo_f = float(lo)
    hi_f = float(hi)
    spread = hi_f - lo_f

    if spread > PERIOD_MAX_SPREAD_FOR_BAND:
        if median is None:
            m = (lo_f + hi_f) / 2.0
        else:
            m = float(median)
        m_i = int(round(m))
        return f"~{m_i} {unit} (wisselend)"

    lo_i = int(math.floor(lo_f))
    hi_i = int(math.ceil(hi_f))
    if lo_i == hi_i:
        return f"{lo_i} {unit}"
    return f"{lo_i}–{hi_i} {unit}"


# =======================
# Score & kleur
# =======================
def score_for_conditions(H, T, W, dir_type):
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

    # Caps bij te klein
    if H < 0.4:
        score = min(score, 0.5)
    elif H < 0.6:
        score = min(score, 1.0)

    return score


def color_from_score_energy(score, energy):
    if score >= 2.3 and energy >= 2.5:
        return "🟢"
    if score >= 1.0:
        return "🟠"
    return "🔴"


def enforce_period_color(color, t_rep):
    return "🔴" if period_is_short(t_rep) else color


def color_square(c):
    return {"🟢": "🟩", "🟠": "🟧", "🔴": "🟥"}.get(c, "🟧")


def cap_color_for_wind(color, wind_type, wind_kmh):
    """
    Alleen caps op kleurweergave, niet op de onderliggende score.
    """
    if wind_type == "onshore":
        if wind_kmh >= 35:
            return "🔴"
        if wind_kmh >= 28 and color == "🟢":
            return "🟠"
    return color


# =======================
# Period choice per hour
# =======================
def choose_period_hour(t_peak, t_wave, t_swell):
    candidates = []
    if t_peak is not None:
        candidates.append(("peak", t_peak))
    if t_wave is not None:
        candidates.append(("wave", t_wave))
    if t_swell is not None:
        candidates.append(("swell", t_swell))

    if not candidates:
        return (None, None)

    if t_peak is not None:
        refs = [v for k, v in candidates if k in ("wave", "swell") and v is not None]
        if refs:
            ref = stats.median(refs)
            if abs(t_peak - ref) <= 4.0:
                return ("peak", t_peak)

    if t_wave is not None:
        return ("wave", t_wave)
    return ("swell", t_swell)


# =======================
# Analyse kern
# =======================
def build_day_features(hrs, waves, t_swell, t_wave, t_peak, winds, dirs, date):
    ids = [
        i for i, ts in enumerate(hrs)
        if ts.startswith(str(date)) and 8 <= int(ts[11:13]) < 20
    ]
    if not ids:
        return None

    hourly = {}
    for h in range(8, 20):
        ixs = [i for i in ids if int(hrs[i][11:13]) == h]
        if not ixs:
            continue
        i0 = ixs[0]

        hw = waves[i0]
        ws = winds[i0]
        dr = dirs[i0]
        if None in (hw, ws, dr):
            continue

        src, tp = choose_period_hour(t_peak[i0], t_wave[i0], t_swell[i0])
        if tp is None:
            continue

        wt = wind_type_from_dir(dr)
        hourly[h] = {
            "wave": hw,
            "wind": ws,
            "wind_type": wt,
            "period": tp,
            "period_src": src,
        }

    if len(hourly) < 6:
        return None

    hours_sorted = sorted(hourly)
    waves_h = [hourly[h]["wave"] for h in hours_sorted]
    per_h = [hourly[h]["period"] for h in hours_sorted]
    wind_h = [hourly[h]["wind"] for h in hours_sorted]
    wtype_h = [hourly[h]["wind_type"] for h in hours_sorted]

    avg_wave = stats.mean(waves_h)
    avg_per = stats.mean(per_h)
    rep_per = stats.median(per_h)
    avg_wind = stats.mean(wind_h)

    day_wt = max(set(wtype_h), key=wtype_h.count)

    energy = 0.49 * (avg_wave ** 2) * avg_per
    day_score = score_for_conditions(avg_wave, avg_per, avg_wind, day_wt)

    # clusters
    hourly_scores = {
        h: score_for_conditions(hourly[h]["wave"], hourly[h]["period"], hourly[h]["wind"], hourly[h]["wind_type"])
        for h in hours_sorted
    }

    base_thr = 1.0
    rel_thr = 0.7 * max(day_score, 0.0001)
    thr = max(base_thr, rel_thr)

    good_hours = sorted([h for h, s in hourly_scores.items() if s >= thr])

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
    day_color = color_from_score_energy(best_cluster_score, energy)
    day_color = enforce_period_color(day_color, rep_per)

    def pct(val):
        return round(100 * sum(1 for x in wtype_h if x == val) / len(wtype_h))

    srcs = [hourly[h]["period_src"] for h in hours_sorted if hourly[h]["period_src"]]
    src_mode = max(set(srcs), key=srcs.count) if srcs else "unknown"

    diag = {
        "spot": SPOT["name"],
        "period_src_mode": src_mode,
        "period_trend": trend_label(per_h),
        "wave_min": round(min(waves_h), 2),
        "wave_max": round(max(waves_h), 2),
        "wave_med": round(stats.median(waves_h), 2),
        "period_min": round(min(per_h), 1),
        "period_max": round(max(per_h), 1),
        "period_med": round(stats.median(per_h), 1),
        "wind_min": round(min(wind_h), 1),
        "wind_max": round(max(wind_h), 1),
        "wind_med": round(stats.median(wind_h), 1),
        "onshore_pct": pct("onshore"),
        "offshore_pct": pct("offshore"),
        "sideshore_pct": pct("sideshore"),
        "period_spread": round(max(per_h) - min(per_h), 1),
        "wind_spread": round(max(wind_h) - min(wind_h), 1),
    }

    hourly_compact = [
        {
            "h": h,
            "w": round(hourly[h]["wave"], 2),
            "t": round(hourly[h]["period"], 1),
            "ws": round(hourly[h]["wind"], 1),
            "wt": hourly[h]["wind_type"],
            "src": hourly[h]["period_src"],
        }
        for h in hours_sorted
    ]

    # dagdelen
    dayparts = {}
    for name, (h0, h1) in DAYPARTS_DEF.items():
        hs = [h for h in range(h0, h1) if h in hourly]
        if not hs:
            continue

        pw = [hourly[h]["wave"] for h in hs]
        pt = [hourly[h]["period"] for h in hs]
        pwind = [hourly[h]["wind"] for h in hs]
        pwt = [hourly[h]["wind_type"] for h in hs]

        p_wave_avg = stats.mean(pw)
        p_per_avg = stats.mean(pt)
        p_per_rep = stats.median(pt)
        p_wind_avg = stats.mean(pwind)
        p_dir_type = max(set(pwt), key=pwt.count)

        p_energy = 0.49 * (p_wave_avg ** 2) * p_per_avg
        p_score = score_for_conditions(p_wave_avg, p_per_avg, p_wind_avg, p_dir_type)
        p_color = color_from_score_energy(p_score, p_energy)
        p_color = enforce_period_color(p_color, p_per_rep)

        # visuele cap bij harde onshore
        p_color = cap_color_for_wind(p_color, p_dir_type, p_wind_avg)

        # robuuste band voor periode
        t_lo, t_hi = robust_band(pt, PERIOD_Q_LO, PERIOD_Q_HI)

        dayparts[name] = {
            "color": p_color,
            "h_min": min(pw),
            "h_max": max(pw),
            "t_min": t_lo,
            "t_max": t_hi,
            "t_rep": p_per_rep,
            "wind_avg": p_wind_avg,
            "wind_type": p_dir_type,
        }

    return {
        "date": date,
        "color": day_color,
        "avg_wave": avg_wave,
        "avg_per": avg_per,
        "rep_per": rep_per,
        "avg_wind": avg_wind,
        "wind_type": day_wt,
        "energy": energy,
        "day_score": day_score,
        "threshold": thr,
        "clusters": clusters,
        "dayparts": dayparts,
        "diag": diag,
        "hourly_compact": hourly_compact,
    }


def summarize_forecast(marine, wind, days_out=3):
    hrs = marine.get("hourly", {}).get("time", [])
    if not hrs:
        return []

    waves_raw = marine.get("hourly", {}).get("wave_height", [])
    swell_period_raw = marine.get("hourly", {}).get("swell_wave_period", [])
    wave_period_raw = marine.get("hourly", {}).get("wave_period", [])
    peak_period_raw = marine.get("hourly", {}).get("swell_wave_peak_period", [])

    winds_raw = wind.get("hourly", {}).get("windspeed_10m", [])
    dirs_raw = wind.get("hourly", {}).get("winddirection_10m", [])

    waves = [(h * WAVE_MULT) if h is not None else None for h in waves_raw]

    def adj_period(arr):
        return [(p + PERIOD_BIAS_S) if p is not None else None for p in arr]

    t_swell = adj_period(swell_period_raw)
    t_wave = adj_period(wave_period_raw)
    t_peak = adj_period(peak_period_raw)

    winds = [w if w is not None else None for w in winds_raw]
    dirs = [d if d is not None else None for d in dirs_raw]

    # guard: arrays kunnen soms korter zijn, pad met None
    n = len(hrs)

    def pad(arr):
        if len(arr) >= n:
            return arr[:n]
        return arr + [None] * (n - len(arr))

    waves = pad(waves)
    t_swell = pad(t_swell)
    t_wave = pad(t_wave)
    t_peak = pad(t_peak)
    winds = pad(winds)
    dirs = pad(dirs)

    start_date = dt.date.fromisoformat(hrs[0][:10])
    out = []
    for d in range(days_out):
        date = start_date + dt.timedelta(days=d)
        day = build_day_features(hrs, waves, t_swell, t_wave, t_peak, winds, dirs, date)
        if day:
            out.append(day)
    return out


# =======================
# Venster tekst
# =======================
def _best_cluster(day):
    clusters = day.get("clusters") or []
    if not clusters:
        return None
    return sorted(
        clusters,
        key=lambda c: (c["score"], (c["end"] - c["start"])),
        reverse=True,
    )[0]


def _daypart_blocks(day):
    """
    Bouw aaneengesloten blokken van dagdelen met dezelfde kleur.
    """
    dp = day.get("dayparts") or {}
    order = [("Ochtend", 8, 12), ("Middag", 12, 16), ("Avond", 16, 20)]
    seq = []
    for name, h0, h1 in order:
        if name in dp and dp[name].get("color"):
            seq.append((name, h0, h1, dp[name]["color"]))

    if not seq:
        return []

    blocks = []
    cur = {"start": seq[0][1], "end": seq[0][2], "color": seq[0][3]}
    for _, h0, h1, c in seq[1:]:
        if c == cur["color"] and h0 == cur["end"]:
            cur["end"] = h1
        else:
            blocks.append(cur)
            cur = {"start": h0, "end": h1, "color": c}
    blocks.append(cur)
    return blocks


def _best_daypart_block(day):
    """
    Kies beste blok obv dagdelen:
    - Als er groen is: pak het langste GROENE blok (beste surfstuk)
    - Anders: pak het langste ORANJE blok
    - Rood: geen echt venster (wordt elders afgevangen bij korte periode)
    """
    blocks = _daypart_blocks(day)
    if not blocks:
        return None

    colors = [b["color"] for b in blocks]
    if "🟢" in colors:
        candidates = [b for b in blocks if b["color"] == "🟢"]
    elif "🟠" in colors:
        candidates = [b for b in blocks if b["color"] == "🟠"]
    else:
        return None

    # langste blok wint; bij gelijk: vroegste
    candidates = sorted(candidates, key=lambda b: ((b["end"] - b["start"]), -b["start"]), reverse=True)
    return candidates[0]


def _is_truly_all_day(day):
    """
    Alleen 'hele dag' als:
    - alle dagdelen dezelfde kleur hebben
    - én het langste cluster lang is (>= 8 uur)
    """
    dp = day.get("dayparts") or {}
    colors = [v.get("color") for v in dp.values() if v.get("color")]
    if len(colors) < 2:
        return False
    if len(set(colors)) != 1:
        return False

    clusters = day.get("clusters") or []
    if not clusters:
        return False
    longest_len = max((c["end"] - c["start"]) for c in clusters)
    return longest_len >= 8


def natural_window_phrase(day):
    # Als er een duidelijk groen/oranje dagdeel-blok is, gebruik dat als venster-taal
    b = _best_daypart_block(day)
    if b:
        if _is_truly_all_day(day):
            return "vrijwel de hele dag"
        return f"vooral {b['start']:02d}–{b['end']:02d}u"

    # fallback: clusters
    bc = _best_cluster(day)
    if not bc:
        return "geen duidelijk venster"
    if _is_truly_all_day(day):
        return "vrijwel de hele dag"
    return f"vooral {bc['start']:02d}–{bc['end']:02d}u"


def best_moments_line(day):
    if period_is_short(day.get("rep_per", day.get("avg_per"))):
        return "👉 Beste moment: geen echt venster (te korte periode, vooral rommel)"

    # Als er groen (of oranje) als duidelijk beste blok is, gebruik dat.
    b = _best_daypart_block(day)
    if b:
        if _is_truly_all_day(day) and day.get("color") == "🟢":
            return "👉 Beste momenten: de hele dag vrij consistent (08–20u)"
        return f"👉 Beste moment: {b['start']:02d}–{b['end']:02d}u"

    # fallback: clusters
    bc = _best_cluster(day)
    if not bc:
        return "👉 Beste moment: geen duidelijk venster vandaag"

    if _is_truly_all_day(day) and day.get("color") == "🟢":
        return "👉 Beste momenten: de hele dag vrij consistent (08–20u)"

    return f"👉 Beste moment: {bc['start']:02d}–{bc['end']:02d}u"
    
# =======================
# 1-oogopslag: overall kleur highlight (optioneel groen als er groen moment is)
# =======================
def pick_header_color(day):
    """
    Jij vroeg: als er een groen moment is, wil je dat in 1 oogopslag zien.
    Zonder je hele systeem om te gooien:
    - Als day.color 🟢 -> 🟢
    - Als day.color 🟠 maar er is minimaal 1 dagdeel 🟢 -> header 🟢 (highlight)
    - Als day.color 🔴 -> 🔴 (blijft rood)
    """
    if day.get("color") == "🟢":
        return "🟢"
    if day.get("color") == "🟠":
        dp = day.get("dayparts") or {}
        if any(v.get("color") == "🟢" for v in dp.values()):
            return "🟢"
    return day.get("color", "🟠")


def why_tag(day):
    d = day.get("diag", {})
    wt = day.get("wind_type", "")
    wind = d.get("wind_med", None)
    per = d.get("period_med", None)

    bits = []
    if wt:
        bits.append(wt)
    if wind is not None:
        if wind >= 30:
            bits.append("veel wind")
        elif wind <= 10:
            bits.append("weinig wind")
    if per is not None:
        if per < 6:
            bits.append("korte periode")
        elif per >= 8:
            bits.append("lange periode")

    bits = bits[:2]
    return " (" + " + ".join(bits) + ")" if bits else ""


# =======================
# AI coach
# =======================
SYSTEM_COACH = (
    f"Je bent een nuchtere maar enthousiaste Nederlandse surfcoach voor {SPOT['name']} (Scheveningen). "
    "Je baseert je op model-forecast data voor deze spot; andere apps kunnen kust-breed of op metingen samenvatten. "
    "Je bent eerlijk: hoogte alleen maakt het niet goed; wind en periode zijn doorslaggevend. "
    "Je praat als tegen een vaste surfmaat: kort, warm, concreet, zonder hype. "
    "Als je over 'venster' praat, bedoel je verdeling over de dag, niet automatisch dat het goed is. "
    "Enthousiasme is verdiend: op groen mag je echt blij zijn. "
    "Op oranje mag je zeggen dat er heerlijke momenten of heerlijke setjes tussenzitten, maar noem het geen heerlijk surfen. "
    "Op rood ben je duidelijk dat het vooral rommelig of taai is."
)


def _sanitize_coach(text):
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"\s+", " ", t)
    t = t.split("\n")[0].strip()

    if re.search(r"\b(\d{1,2})\s*(u|uur)\b", t.lower()):
        return ""
    if re.search(r"\b\d{1,2}[:.]\d{2}\b", t):
        return ""

    if len(t) < 8:
        return ""
    return t


def _contains_heerlijk_surfen(text):
    if not text:
        return False
    return re.search(r"\bheerlij\w*\s+surfen\b", text.lower()) is not None


def _ai_coach(day, purpose="today"):
    payload = {
        "spot": SPOT["name"],
        "stoplicht": day["color"],
        "header_hint": pick_header_color(day),  # extra hint voor toon
        "avg": {
            "wave_m": round(day["avg_wave"], 2),
            "period_s": round(day["avg_per"], 1),
            "period_rep_s": round(day["rep_per"], 1),
            "wind_kmh": round(day["avg_wind"], 1),
            "wind_type": day.get("wind_type", "sideshore"),
        },
        "diag": day.get("diag", {}),
        "hourly_compact": day.get("hourly_compact", []),
        "dayparts": {
            k: {
                "color": v["color"],
                "wave_min": round(v["h_min"], 2),
                "wave_max": round(v["h_max"], 2),
                "period_med": round(v["t_rep"], 1) if v.get("t_rep") is not None else None,
                "period_band": [
                    round(v["t_min"], 1) if v.get("t_min") is not None else None,
                    round(v["t_max"], 1) if v.get("t_max") is not None else None,
                ],
                "wind_avg": round(v["wind_avg"], 1),
                "wind_type": v["wind_type"],
            }
            for k, v in (day.get("dayparts") or {}).items()
        },
        "window_phrase": natural_window_phrase(day),
    }

    if purpose == "future":
        instruction = (
            "Schrijf 1 korte, menselijke surfcoach-zin (10-18 woorden) voor morgen/overmorgen. "
            "Je mag 1-2 getallen noemen (hoogte/periode/wind), maar noem geen tijden. "
            "Onderbouw met 2 signalen uit de data (windrichting-aandeel, periode-trend, windsterkte, stabiliteit, piek). "
            "Pas je enthousiasme aan op stoplicht (groen blij, oranje genuanceerd, rood duidelijk). "
            "Vermijd vage woorden zonder onderbouwing."
        )
    else:
        instruction = (
            "Schrijf 1 korte, menselijke surfcoach-zin (10-20 woorden) voor vandaag. "
            "Je mag 1-2 getallen noemen (hoogte/periode/wind), maar noem geen tijden. "
            "Onderbouw met 2 signalen uit de data (windrichting-aandeel, periode-trend, windsterkte, stabiliteit, piek). "
            "Als stoplicht groen is, mag je echt enthousiast zijn. "
            "Als stoplicht oranje is, mag je zeggen dat er heerlijke momenten of setjes tussenzitten, "
            "maar noem het geen heerlijk surfen. "
            "Als stoplicht rood is, wees helder dat het rommelig of taai is. "
            "Vermijd 'hele dag goed' taal tenzij stoplicht groen is."
        )

    body = json.dumps(
        {
            "model": MODEL_ID,
            "messages": [
                {"role": "system", "content": SYSTEM_COACH},
                {"role": "user", "content": f"{instruction}\n\nData:\n{json.dumps(payload, ensure_ascii=False)}"},
            ],
            "temperature": 0.8,
            "max_tokens": 95,
        }
    )

    conn = http.client.HTTPSConnection("api.groq.com")
    conn.request(
        "POST",
        "/openai/v1/chat/completions",
        body,
        {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
    )
    res = conn.getresponse()
    raw = res.read().decode()

    if res.status != 200:
        return ""

    try:
        txt = json.loads(raw)["choices"][0]["message"]["content"]
    except Exception:
        return ""

    txt = _sanitize_coach(txt)

    if txt and day.get("color") != "🟢" and _contains_heerlijk_surfen(txt):
        return ""

    return txt


def fallback_coach(day):
    w = day["avg_wave"]
    t = day["avg_per"]
    wt = day.get("wind_type", "sideshore")
    wind = day["avg_wind"]

    if period_is_short(day.get("rep_per", t)):
        if wt == "onshore" and wind >= 12:
            return "Korte periode en veel onshore, dus vooral chop en weinig lijn."
        return "Korte periode, dus snel rommelig en weinig echte power."

    if w < 0.45:
        return "Klein en weinig power, longboard is je beste kans."
    if w < 0.7 and t <= 6:
        return "Klein en kort, longboard of funboard werkt het lekkerst."
    if wt == "onshore" and wind >= 18:
        return "Hoogte zat, maar de wind drukt het snel plat; vooral werken voor je golven."
    if wt == "offshore" and wind <= 18 and t >= 6:
        return "Wind helpt mee en de periode geeft ruimte, hier kun je lekker doorpakken."
    return "Met wat geduld zitten er best bruikbare setjes tussen, al blijft het wisselend."


def coach_line(day, purpose="today"):
    if not GROQ_API_KEY:
        return fallback_coach(day)
    txt = _ai_coach(day, purpose=purpose)
    return txt if txt else fallback_coach(day)


# =======================
# Bericht
# =======================
def build_message(summary):
    today = summary[0]
    d = today["date"]
    label = f"{DAGEN[d.weekday()]} {d.day} {MAANDEN[d.month-1]}"

    header_color = pick_header_color(today)

    lines = []
    lines.append(f"📅 {label}")
    lines.append(f"{color_square(header_color)} {coach_line(today, 'today')}{why_tag(today)}")
    lines.append("")

    dp = today.get("dayparts") or {}
    for name in ["Ochtend", "Middag", "Avond"]:
        part = dp.get(name)
        if not part:
            continue

        h_txt = fmt_range(part["h_min"], part["h_max"], ndigits=1, unit="m")
        t_txt = fmt_period_band(part["t_min"], part["t_max"], unit="s", median=part.get("t_rep"))
        lines.append(
            f"{part['color']} {name}: {h_txt} / {t_txt} ~{round(part['wind_avg'])} km/u {part['wind_type']}"
        )

    lines.append("")
    lines.append(best_moments_line(today))
    lines.append("")

    if len(summary) > 1:
        t = summary[1]
        phrase = natural_window_phrase(t)
        if phrase == "vrijwel de hele dag" and t.get("color") != "🟢":
            phrase = "door de dag heen (met dips)"
        lines.append(
            f"{color_square(t['color'])} Morgen: {coach_line(t, 'future')} "
            f"Venster: {phrase}, met ~{t['avg_wave']:.1f} m en {round(t['avg_per'])} s swell."
        )

    if len(summary) > 2:
        o = summary[2]
        phrase = natural_window_phrase(o)
        if phrase == "vrijwel de hele dag" and o.get("color") != "🟢":
            phrase = "door de dag heen (met dips)"
        lines.append(
            f"{color_square(o['color'])} Overmorgen: {coach_line(o, 'future')} "
            f"Venster: {phrase}, met ~{o['avg_wave']:.1f} m en {round(o['avg_per'])} s swell."
        )

    return "\n".join(lines)


# =======================
# Telegram
# =======================
def send_telegram_message(text):
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN ontbreekt (env var leeg).")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID ontbreekt (env var leeg).")

    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=20,
    )

    if r.status_code != 200:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f"Telegram API error {r.status_code}: {detail}")

    return True


# =======================
# Main
# =======================
if __name__ == "__main__":
    # Belangrijk:
    # - Als je cron gebruikt: zet cron op 08:00 en laat onderstaande wait staan (wacht dan 0 sec).
    # - Als je script continu draait: dit zorgt dat hij om 08:00 verstuurt.
    wait_until_send_time(SEND_AT_HOUR, SEND_AT_MINUTE)

    try:
        marine, wind = get_open_meteo(SPOT["lat"], SPOT["lon"], days=2)
        summary = summarize_forecast(marine, wind, days_out=3)

        if not summary:
            message = "Geen surfdata beschikbaar vandaag."
        else:
            message = build_message(summary)

    except Exception as e:
        message = f"Surfbot: Open-Meteo tijdelijk traag/onbereikbaar. ({str(e)[:220]})"

    print("----- SURF MESSAGE START -----")
    print(message)
    print("----- SURF MESSAGE END -----")

    send_telegram_message(message)
