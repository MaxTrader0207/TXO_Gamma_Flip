"""
證交所資料：加權指數每日收盤（均線改用現貨計算，避開台指期轉倉與除息價差）與休市日曆（剩餘交易日扣掉國定假日）。
- data/taiex.json：{"YYYY-MM-DD": 收盤}，逐月補抓，每次執行至少更新當月與上月
- data/holidays_tw.json：{"YYYY": ["YYYY-MM-DD", ...]}，平日但不交易的日子
抓不到時回傳既有快取，不讓主流程失敗。
"""
import json
import os
import re
import time
from datetime import date, datetime

import requests

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
UA = {"User-Agent": "Mozilla/5.0 (txo-gamma-flip)"}
TAIEX_URL = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"
HOLI_URLS = [
    "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date={y}0101&response=json",
    "https://www.twse.com.tw/holidaySchedule/holidaySchedule?response=json&queryYear={roc}",
]


def _roc_date(s):
    m = re.match(r"(\d{2,3})/(\d{1,2})/(\d{1,2})", s.strip())
    if m:
        return date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s.strip())
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _get(url, **kw):
    for i in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=30, **kw)
            if r.status_code == 200:
                return r.json()
        except Exception as e:  # noqa: BLE001
            print("twse 取得失敗", url, e)
        time.sleep(2 + i * 2)
    return None


def taiex(start, end=None):
    """回傳 {date: close}，涵蓋 start～end（含快取）"""
    path = os.path.join(DATA, "taiex.json")
    cache = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    end = end or date.today()
    months = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    have_months = {k[:7] for k in cache}
    recent = {(end.year, end.month), ((end.year - 1, 12) if end.month == 1 else (end.year, end.month - 1))}
    got = 0
    for y, m in months:
        key = f"{y:04d}-{m:02d}"
        if key in have_months and (y, m) not in recent:
            continue
        js = _get(TAIEX_URL, params={"date": f"{y:04d}{m:02d}01", "response": "json"})
        if not js or not js.get("data"):
            print("加權指數", key, "沒有資料", (js or {}).get("stat"))
            continue
        for row in js["data"]:
            d = _roc_date(str(row[0]))
            if d:
                cache[d.isoformat()] = float(str(row[4]).replace(",", ""))
                got += 1
        time.sleep(0.6)
    if got:
        json.dump(dict(sorted(cache.items())), open(path, "w", encoding="utf-8"), indent=0)
    print("加權指數快取", len(cache), "天，本次更新", got, "筆")
    return {date.fromisoformat(k): v for k, v in cache.items()}


def holidays(years):
    """回傳 set(date)：平日但休市的日子"""
    path = os.path.join(DATA, "holidays_tw.json")
    cache = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    for y in years:
        if str(y) in cache and y < datetime.now().year:
            continue
        found = None
        for u in HOLI_URLS:
            js = _get(u.format(y=y, roc=y - 1911))
            rows = (js or {}).get("data") or []
            if rows:
                found = rows
                break
        if not found:
            print("休市日曆", y, "抓不到，沿用快取")
            continue
        out = []
        for row in found:
            d = _roc_date(str(row[0]))
            if not d or d.year != y or d.weekday() >= 5:
                continue
            text = " ".join(str(x) for x in row[1:])
            # 「開始交易」「最後交易」是交易日；其他列（國定假日、無交易僅辦理結算交割）視為休市
            if ("開始交易" in text or "最後交易" in text) and "無交易" not in text:
                continue
            out.append(d.isoformat())
        cache[str(y)] = sorted(set(out))
        print("休市日曆", y, cache[str(y)])
    json.dump(cache, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    return {date.fromisoformat(d) for v in cache.values() for d in v}


MARGIN_URL = "https://www.taifex.com.tw/cht/5/indexMarging"


def txo_margin():
    """期交所臺指選擇權風險保證金 A、B 值（結算／維持／原始），抓不到時回傳 None"""
    path = os.path.join(DATA, "margin.json")
    cache = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else None
    try:
        r = requests.get(MARGIN_URL, headers=UA, timeout=30)
        html = r.content.decode("utf-8", "replace")
        out = {}
        for key, label in (("A", "臺指選擇權風險保證金(A)值"), ("B", "臺指選擇權風險保證金(B)值")):
            i = html.find(label)
            if i < 0:
                raise ValueError("找不到 " + label)
            nums = re.findall(r">\s*([\d,]{4,})\s*<", html[i:i + 1500])[:3]
            out[key] = [int(x.replace(",", "")) for x in nums]
        m = re.search(r"更新日期[：:]\s*(\d{4}/\d{1,2}/\d{1,2})", html)
        out["updated"] = m.group(1) if m else None
        if len(out["A"]) == 3 and len(out["B"]) == 3:
            json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False)
            print("臺指選擇權保證金", out)
            return out
    except Exception as e:  # noqa: BLE001
        print("保證金抓取失敗", e)
    return cache
