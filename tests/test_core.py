"""
資料管線核心測試：用固定樣本資料檢查解析、Gamma 計算、燈號、均線、波動指標、模擬帳本與回測成交價。
執行：pytest -q
"""
import json
import math
import os
import sys
from datetime import date, datetime, timedelta

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
FIX = os.path.join(os.path.dirname(__file__), "fixtures")

import paper  # noqa: E402
import strategy_bt as sb  # noqa: E402
import txo_gex as tg  # noqa: E402


def fx(name):
    return os.path.join(FIX, name)


def test_parse_options_fields():
    txt = open(fx("opt_20261106.csv"), encoding="utf-8").read()
    out = tg.parse_options(txt, keep_zero_oi=True)
    rows = out[date(2026, 11, 6)]
    assert len(rows) > 100
    r = rows[0]
    assert len(r) == 9, "每列應有 代號、到期、履約價、買賣、結算、OI、買價、賣價、開盤 9 欄"
    code, exp, k, cp, px, oi, bid, ask, opn = r
    assert cp in ("C", "P") and k > 0 and oi >= 0
    assert any(x[6] for x in rows) and any(x[7] for x in rows), "應解析到最後最佳買／賣價"
    # 買價不應高於賣價
    assert all(x[6] <= x[7] for x in rows if x[6] and x[7])


def test_parse_futures_sessions():
    head = "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差對單式委託成交量"
    rows = ["2026/11/06,TX,202611,41000,41200,39500,39650,,,1000,39652,100,,,,,,一般,",
            "2026/11/06,TX,202612,41100,41300,39600,39750,,,500,39760,50,,,,,,一般,",
            "2026/11/06,TX,202611,41800,41900,40900,41050,,,800,,,,,,,,盤後,",
            "2026/11/06,TX,202611/202612,1,1,1,1,,,1,1,1,,,,,,一般,"]
    txt = head + "\n" + "\n".join(rows)
    d = tg.parse_futures(txt)
    n = tg.parse_futures(txt, "盤後")
    assert d[date(2026, 11, 6)][0] == "202611" and d[date(2026, 11, 6)][1] == 39652
    assert n[date(2026, 11, 6)][5] == 41050, "夜盤沒有結算價時用收盤價"


def test_summarize_flip_and_dealer():
    snap = json.load(open(fx("snap_20261106.json"), encoding="utf-8"))
    val = datetime.fromisoformat(snap["valuation"])
    s = tg.summarize(snap["rows"], snap["F"], val, (0.15, 0.04))
    for m in ("naive", "short", "dealer"):
        assert m in s and math.isfinite(s[m]["gex"])
    # 與當天存檔的結果一致
    assert abs(s["naive"]["gex"] - snap["summary"]["naive"]["gex"]) < 1e-6


def test_bt_bucket():
    assert tg.bt_bucket(100, 110, 5) == "red"
    assert tg.bt_bucket(120, 110, -5) == "green"
    assert tg.bt_bucket(100, None, -1) == "red"
    assert tg.bt_bucket(100, None, 1) == "green"


def test_ma_status_first_break_and_rising():
    # 穩定上漲、最後一天小幅跌破 30MA（均線仍比昨天高）：應判定為近期第一次跌破、均線上揚
    closes = [100 + i * 0.5 for i in range(120)]
    closes[-1] = sum(closes[-31:-1]) / 30 - 1
    st = tg.ma_status(closes)
    assert 30 in st["breaks"]
    assert st["rising"][30] is True
    # 大跌讓 30MA 當天就下彎：依「今天均線 > 昨天均線」的定義不算
    big = [100 + i * 0.5 for i in range(120)]
    big[-1] = big[-2] - 20
    assert 30 not in tg.ma_status(big)["breaks"]
    # 均線下彎時不算
    down = [200 - i * 0.5 for i in range(120)]
    down[-2] = 300
    assert 30 not in tg.ma_status(down)["breaks"]


def test_vol_ind_and_status():
    snap = json.load(open(fx("snap_20261106.json"), encoding="utf-8"))
    v = tg.vol_ind(snap["rows"], snap["F"], datetime.fromisoformat(snap["valuation"]))
    assert v["iv30"] and 0.05 < v["iv30"] < 1.5
    assert v["skew"] is not None
    hist = json.load(open(fx("history.json"), encoding="utf-8"))["items"]
    st = tg.vol_status(sorted(hist, key=lambda x: x["date"]))
    assert "vrp" in st and "skewPct" in st


def test_attach_signal_has_expected_keys():
    snap = json.load(open(fx("snap_20261106.json"), encoding="utf-8"))
    hist = sorted(json.load(open(fx("history.json"), encoding="utf-8"))["items"], key=lambda x: x["date"])
    sig = tg.attach_signal(snap, hist)
    assert sig is not None
    for k in ("bucket", "label", "ma", "vol", "stats", "expand", "ret"):
        assert k in sig
    st = sig["stats"]
    for k in ("_model", "_strategy", "_board", "red", "green"):
        assert k in st, f"signal.json 缺少 {k}"


def test_trading_days_skip_holidays():
    paper.HOLI = {date(2026, 10, 26)}
    assert paper.trading_days(date(2026, 10, 22), date(2026, 10, 27)) == 2  # 23、27（26 休市）
    paper.HOLI = set()


def test_paper_update_seller_and_settle(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "PATH", str(tmp_path / "paper.json"))
    d0, d1 = date(2026, 11, 2), date(2026, 11, 4)
    F = 40000.0
    rows0 = []
    for k in range(36000, 44001, 100):
        for cp in "CP":
            otm = (k - F) if cp == "C" else (F - k)
            px = max(1.0, 300 * math.exp(-max(otm, 0) / 600)) + max(-otm, 0)
            rows0.append(["202611W1", d1.isoformat(), float(k), cp, round(px, 1), 100, round(px - 1, 1), round(px + 1, 1), round(px, 1)])
    # 到期日：價內選擇權結算價＝內含價值（S=40200）
    S = 40200.0
    rows1 = [["202611W1", d1.isoformat(), float(k), cp, max(S - k, 0) if cp == "C" else max(k - S, 0), 0, None, None, None]
             for k in range(36000, 44001, 100) for cp in "CP"]
    opt = {d0: rows0, d1: rows1}
    fut = {d0: ("202611", F, F, F, F, F), d1: ("202611", S, S, S, S, S)}
    items = {d0.isoformat(): {"date": d0.isoformat(), "F": F, "naive_flip": 39000, "naive_gex": 5, "atr5": 300, "close": F},
             d1.isoformat(): {"date": d1.isoformat(), "F": S, "naive_flip": 39000, "naive_gex": 5, "atr5": 300, "close": S}}
    model = {"a": 0.4, "b": 0.7, "c": 0.3}
    summ = paper.update([d0, d1], opt, fut, items, tg, model, [{"name": "標準", "k": 2.5}])
    led = json.load(open(tmp_path / "paper.json", encoding="utf-8"))
    e = [x for x in led["entries"] if x["tier"] == "標準" and x["date"] == d0.isoformat()][0]
    assert e["status"] == "closed" and abs(e["S"] - S) < 1
    # 賣出用買價
    assert e["pc"] < e["Kc"] and e["credit"] == round(e["pc"] + e["pp"], 1)
    assert summ["tiers"][0]["closed"] >= 1


def test_strategy_price_sides():
    row = [40000, 100, 90, 99, 101, 89, 91, 102, 88]
    sb.set_fill("quote")
    assert sb.price(row, True, "sell") == 99 and sb.price(row, True, "buy") == 101
    assert sb.price(row, False, "sell") == 89 and sb.price(row, False, "open") == 88
    assert sb.price(row[:3], True, "sell") == 100, "舊資料沒有買賣價時退回結算價"
    sb.set_fill("settle")
    assert sb.price(row, True, "sell") == 100
    sb.set_fill("quote")


def test_snapshot_json_shape():
    lat = json.load(open(os.path.join(ROOT, "data", "latest.json"), encoding="utf-8"))
    assert lat["fields"][:6] == ["code", "expiry", "strike", "cp", "settle", "oi"]
    assert all(len(r) >= 6 for r in lat["rows"][:50])
    assert "summary" in lat and "naive" in lat["summary"]
    datetime.fromisoformat(lat["valuation"])
    assert date.fromisoformat(lat["tradeDate"]) <= date.today() + timedelta(days=1)


def test_night_map_premarket():
    # 期交所把夜盤記在隔天日期下；盤前那筆夜盤（日期＝今天）還沒有日盤可對應，要接在最後一個日盤之後
    import night_bt
    d = [date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11), date(2026, 11, 12)]
    day = {d[0]: ("202611", 100, 99, 101, 98, 100), d[1]: ("202611", 110, 100, 111, 99, 110),
           d[2]: ("202611", 120, 110, 121, 109, 120)}
    night = {d[1]: ("202611", 101, 100.5, 102, 100, 101), d[2]: ("202611", 111, 110.5, 112, 110, 111),
             d[3]: ("202611", 123, 120.5, 124, 120, 123)}
    nm, nxt = night_bt.night_map(day, night)
    assert nxt, "夜盤開盤接近前一個日盤收盤 → 判定為次一營業日歸屬"
    assert nm[d[0]][5] == 101 and nm[d[1]][5] == 111
    assert nm[d[2]][5] == 123, "盤前：11/12 的夜盤要對到 11/11 日盤"
    # 當天日盤出來之後，對應結果不變
    day[d[3]] = ("202611", 130, 123, 131, 122, 130)
    assert night_bt.night_map(day, night)[0][d[2]][5] == 123
