import os, json, datetime as dt, requests, http.client, statistics as stats, re

# =======================
# Instellingen
# =======================
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MODEL_ID = "openai/gpt-oss-120b"  # eenvoudig te wisselen

SPOT = {"name": "Scheveningen Pier", "lat": 52.109, "lon": 4.276}
TZ = "Europe/Amsterdam"

DAGEN = ["Maandag","Dinsdag","Woensdag","Donderdag","Vrijdag","Zaterdag","Zondag"]
MAANDEN = ["jan","feb","mrt","apr","mei","jun","jul","aug","sep","okt","nov","dec"]

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

# =======================
# Surf score
# =======================
def score_for_conditions(H, T, W, dir_type):
    if H is None or T is None or W is None:
        return 0.0

    score = 0.0

    # Hoogte
    if H < 0.4: score += 0.0
    elif H < 0.6: score += 0.3
    elif H < 0.8: score += 0.6
    elif H < 1.2: score += 1.0
    else: score += 1.2

    # Periode
    if T >= 8: score += 1.0
    elif T >= 7: score += 0.8
    elif T >= 6: score += 0.5
    elif T >= 5: score += 0.3

    # Windkracht
    if W <= 10: score += 1.0
    elif W <= 18: score += 0.7
    elif W <= 26: score += 0.3

    # Windrichting
    if dir_type == "offshore":
        score += 0.4
        if W > 25: score -= 0.2
    elif dir_type == "onshore":
        if W > 28: score -= 0.8
        elif W > 20: score -= 0.5
        else: score -= 0.2
    else:
        if W > 28: score -= 0.2

    if H < 0.4: score = min(score, 0.5)
    elif H < 0.6: score = min(score, 1.0)

    return score

# =======================
# Forecast samenvatten
# =======================
def summarize_forecast(marine, wind):
    hrs = marine["hourly"]["time"]
    waves = [h * 1.4 if h else None for h in marine["hourly"]["wave_height"]]
    periods = [(p + 1) if p else None for p in marine["hourly"]["swell_wave_period"]]
    winds = wind["hourly"]["windspeed_10m"]
    dirs = wind["hourly"]["winddirection_10m"]

    def angle_diff(a, b): return abs((a - b + 180) % 360 - 180)

    start_date = dt.date.fromisoformat(hrs[0][:10])
    data = []

    for d in range(3):
        date = start_date + dt.timedelta(days=d)
        idx = [i for i,t in enumerate(hrs) if t.startswith(str(date)) and 8 <= int(t[11:13]) < 20]
        if not idx: continue

        wv = [waves[i] for i in idx if waves[i]]
        pr = [periods[i] for i in idx if periods[i]]
        ws = [winds[i] for i in idx if winds[i]]
        wd = [dirs[i] for i in idx if dirs[i]]
        if not (wv and pr and ws and wd): continue

        avg_wave, avg_per, avg_wind, avg_dir = map(stats.mean, [wv, pr, ws, wd])
        energy = 0.49 * avg_wave**2 * avg_per

        if angle_diff(avg_dir, 270) <= 60: wind_type = "onshore"
        elif angle_diff(avg_dir, 90) <= 60: wind_type = "offshore"
        else: wind_type = "sideshore"

        day_score = score_for_conditions(avg_wave, avg_per, avg_wind, wind_type)

        hourly_scores, hourly_meta = {}, {}
        for h in range(8,20):
            ids = [i for i,t in enumerate(hrs) if t.startswith(str(date)) and int(t[11:13]) == h]
            if not ids: continue
            hw, tp, wd_, dr = waves[ids[0]], periods[ids[0]], winds[ids[0]], dirs[ids[0]]
            if None in (hw,tp,wd_,dr): continue

            if angle_diff(dr,270)<=60: ht="onshore"
            elif angle_diff(dr,90)<=60: ht="offshore"
            else: ht="sideshore"

            s = score_for_conditions(hw,tp,wd_,ht)
            hourly_scores[h]=s
            hourly_meta[h]={"wave":hw,"period":tp,"wind":wd_,"wind_type":ht}

        good_hours = [h for h,s in hourly_scores.items() if s>=max(1.0,0.7*day_score)]
        clusters=[]
        if good_hours:
            start=prev=good_hours[0]
            for h in good_hours[1:]:
                if h==prev+1: prev=h
                else:
                    clusters.append({"start":start,"end":prev+1})
                    start=prev=h
            clusters.append({"start":start,"end":prev+1})

        best_cluster = max((c["end"]-c["start"] for c in clusters), default=0)
        color = "🟢" if best_cluster>=6 and energy>=2.5 else "🟠" if best_cluster>=2 else "🔴"

        data.append({
            "date":date,
            "color":color,
            "avg_wave":avg_wave,
            "avg_per":avg_per,
            "avg_wind":avg_wind,
            "wind_type":wind_type,
            "clusters":clusters,
            "hourly_meta":hourly_meta
        })
    return data

# =======================
# Coach fallback
# =======================
def _fallback_coach_line(day):
    w,t,wind = day["avg_wave"],day["avg_per"],day["avg_wind"]
    wt = day["wind_type"]
    if w < 0.45:
        return "Klein en weinig power, longboard is je beste kans."
    if w < 0.7 and t <= 5:
        return "Klein en kort, longboard of funboard werkt het lekkerst."
    if wt=="onshore" and wind>=18:
        return "Onshore maakt het rommelig, vooral voor beginners wat taai."
    if t<5:
        return "Korte periode, dus snel rommelig en weinig echte power."
    if wt=="offshore" and wind<=18 and t>=6:
        return "Netter door offshore, lekker voor een snelle shortboard sessie."
    return "Surfbaar, maar verwacht wisselende lijnen en wat rommel."

def _sanitize(text):
    if not text or re.search(r"\d",text): return ""
    return re.sub(r"\s+"," ",text).strip()

# =======================
# AI coach
# =======================
def ai_text(day):
    payload = {
        "kleur": day["color"],
        "hoogte": day["avg_wave"],
        "periode": day["avg_per"],
        "wind": day["avg_wind"],
        "windtype": day["wind_type"],
    }

    prompt = f"""
Je bent een Nederlandse surfcoach.
Schrijf 1 zin (8–16 woorden) alsof je een vriend appt.

Je mag soms advies geven over longboard, shortboard of niveau,
maar alleen als het logisch is.

Geen cijfers, geen tijden, geen units.

DATA:
{json.dumps(payload,ensure_ascii=False)}
"""

    body = json.dumps({
        "model": MODEL_ID,
        "messages":[
            {"role":"system","content":"Nuchtere Nederlandse surfcoach."},
            {"role":"user","content":prompt.strip()}
        ],
        "temperature":0.55,
        "max_tokens":60
    })

    conn = http.client.HTTPSConnection("api.groq.com")
    conn.request("POST","/openai/v1/chat/completions",body,{
        "Authorization":f"Bearer {GROQ_API_KEY}",
        "Content-Type":"application/json"
    })
    res = conn.getresponse()
    if res.status!=200:
        return _fallback_coach_line(day)

    txt=_sanitize(json.loads(res.read())["choices"][0]["message"]["content"])
    return txt if txt else _fallback_coach_line(day)

# =======================
# Bericht bouwen
# =======================
def build_message(summary):
    today=summary[0]
    d=today["date"]
    label=f"{DAGEN[d.weekday()]} {d.day} {MAANDEN[d.month-1]}"
    return f"📅 {label}\n{today['color']} {ai_text(today)}"

# =======================
# Telegram
# =======================
def send_telegram_message(text):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id":TELEGRAM_CHAT_ID,"text":text},
        timeout=20
    )

# =======================
# Main
# =======================
if __name__=="__main__":
    marine,wind=get_marine(SPOT["lat"],SPOT["lon"])
    summary=summarize_forecast(marine,wind)
    if summary:
        send_telegram_message(build_message(summary))
