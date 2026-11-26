#!/usr/bin/env python3
"""ECOPOINT: предрасчёт паводковых порогов по рекам.

Скачивает историю расхода (GloFAS через Open-Meteo Flood API) кусками по 5 лет
с паузами, чтобы не упираться в лимит запросов, и сохраняет пороги в data/rivers.js.
Только стандартная библиотека Python. Запуск из корня проекта:

    python3 tools/precompute.py

Занимает несколько минут. Запускать один раз (и перед защитой — для свежих данных).
"""
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://flood-api.open-meteo.com/v1/flood"
START_YEAR = 1994      # 30+ лет истории достаточно для порогов «раз в 2/5/10 лет»
CHUNK_YEARS = 5        # кусок истории в одном запросе
PAUSE_S = 15           # пауза между запросами
SNAP_STEP = 0.05       # шаг поиска главной реки вокруг точки, градусы (~5 км)

POINTS = [
    {"id": "ural-uralsk", "name": "Урал · Уральск", "lat": 51.20, "lon": 51.34,
     # Казгидромет: уровень Урала у г. Уральск 854 см 21.04.2024 при опасной отметке 850 см.
     "known_peak": {"date": "2024-04-21",
                    "note": "Казгидромет: пик уровня Урала у Уральска — 854 см, 21.04.2024"}},
    {"id": "tobol-kostanay", "name": "Тобол · Костанай", "lat": 53.21, "lon": 63.62},
    {"id": "zhem-kulsary", "name": "Жем · Кульсары", "lat": 46.95, "lon": 54.02},
]


def get(params, tries=4):
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < tries - 1:
                print("    лимит запросов (429), жду 65 с…")
                time.sleep(65)
                continue
            raise


def series(daily):
    return [(t, q) for t, q in zip(daily["time"], daily["river_discharge"]) if q is not None]


def quantile(values, p):
    s = sorted(values)
    i = (len(s) - 1) * p
    lo, hi = int(i), min(int(i) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


def snap_to_river(lat, lon, today):
    """Ячейка 3×3 вокруг точки с наибольшим средним расходом за 14 дней (лёгкие запросы)."""
    start = (today - dt.timedelta(days=13)).isoformat()
    best = None
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            c_lat, c_lon = round(lat + dy * SNAP_STEP, 4), round(lon + dx * SNAP_STEP, 4)
            d = get({"latitude": c_lat, "longitude": c_lon, "daily": "river_discharge",
                     "start_date": start, "end_date": today.isoformat()})
            s = series(d["daily"])
            mean = sum(q for _, q in s) / len(s) if s else 0.0
            print(f"    ячейка {c_lat}, {c_lon}: средний расход {mean:.1f} м³/с")
            if best is None or mean > best["mean"]:
                best = {"lat": c_lat, "lon": c_lon, "mean": round(mean, 2)}
            time.sleep(1)
    return best


def history(lat, lon, today):
    out = []
    for y0 in range(START_YEAR, today.year + 1, CHUNK_YEARS):
        y1 = min(y0 + CHUNK_YEARS - 1, today.year)
        end = min(dt.date(y1, 12, 31), today)
        print(f"    история {y0}–{y1}…")
        d = get({"latitude": lat, "longitude": lon, "daily": "river_discharge",
                 "start_date": f"{y0}-01-01", "end_date": end.isoformat()})
        out += series(d["daily"])
        time.sleep(PAUSE_S)
    return out


def analyse(point, today):
    print(f"\n{point['name']}")
    cell = snap_to_river(point["lat"], point["lon"], today)
    hist = history(cell["lat"], cell["lon"], today)

    max_by_year = {}
    for t, q in hist:
        y = t[:4]
        if y == str(today.year):
            continue
        if y not in max_by_year or q > max_by_year[y][1]:
            max_by_year[y] = (t, q)
    maxima = [q for _, q in max_by_year.values()]

    thresholds = None
    if len(maxima) >= 10 and quantile(maxima, 0.5) >= 1:
        thresholds = {"years": len(maxima),
                      "q2": round(quantile(maxima, 0.5), 1),
                      "q5": round(quantile(maxima, 0.8), 1),
                      "q10": round(quantile(maxima, 0.9), 1)}

    spring2024 = [{"t": t, "q": round(q, 1)} for t, q in hist if "2024-03-01" <= t <= "2024-05-31"]
    peak2024 = max(spring2024, key=lambda p: p["q"]) if spring2024 else None

    print(f"    пороги: {thresholds}")
    print(f"    пик весны 2024 (модель): {peak2024}")
    return {**point, "cell": cell, "thresholds": thresholds, "peak2024": peak2024,
            "spring2024": spring2024,
            "annual_max": {y: {"date": t, "q": round(q, 1)} for y, (t, q) in sorted(max_by_year.items())}}


def main():
    today = dt.date.today()
    rivers = [analyse(p, today) for p in POINTS]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(os.path.join(root, "data"), exist_ok=True)
    payload = {"generated": today.isoformat(), "rivers": rivers}
    with open(os.path.join(root, "data", "rivers.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    # .js-версия грузится через <script> и работает даже при открытии файла без сервера
    with open(os.path.join(root, "data", "rivers.js"), "w", encoding="utf-8") as f:
        f.write("window.RIVERS = " + json.dumps(payload, ensure_ascii=False) + ";\n")
    print("\nГотово: data/rivers.json и data/rivers.js")


if __name__ == "__main__":
    main()
