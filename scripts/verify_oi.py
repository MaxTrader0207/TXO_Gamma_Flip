"""
未平倉核對：用三種獨立來源比對管線解析的 TXO 未平倉量，結果寫到 verify/report.md
A. 期交所原始 CSV（optDataDown）用 pandas 獨立加總，與 txo_gex.parse_options 的結果逐到期比對
B. 期交所 Put/Call 比報表（pcRatioDown）的全市場買權／賣權未平倉總量
C. 期交所網頁每日行情表（optDailyMarketReport）逐履約價比對指定到期
用法：python scripts/verify_oi.py [YYYY-MM-DD]
"""
import io
import json
import os
import re
import sys
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(__file__))
import txo_gex as tg  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
OUT = os.path.join(ROOT, "verify")
UA = {"User-Agent": "Mozilla/5.0 (txo-gamma-flip verify)"}
L = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    L.append(s)


def main():
    os.makedirs(OUT, exist_ok=True)
    if len(sys.argv) > 1:
        d = date.fromisoformat(sys.argv[1])
    else:
        d = date.fromisoformat(json.load(open(os.path.join(ROOT, "data", "latest.json")))["tradeDate"])
    ds = d.strftime("%Y/%m/%d")
    log(f"# TXO 未平倉核對（{d}）\n")
    log(f"產生時間：{datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')}\n")

    # ---------- A ----------
    raw = requests.post(tg.OPT_URL, data={"down_type": "1", "commodity_id": "TXO", "commodity_id2": "",
                                          "queryStartDate": ds, "queryEndDate": ds}, headers=UA, timeout=60).content
    open(os.path.join(OUT, f"raw_opt_{d:%Y%m%d}.csv"), "wb").write(raw)
    txt = raw.decode("big5", "replace")
    hi = next(i for i, l in enumerate(txt.splitlines()) if "到期月份" in l)
    df = pd.read_csv(io.StringIO("\n".join(txt.splitlines()[hi:])), dtype=str, index_col=False)
    df.columns = [c.strip() for c in df.columns]
    log("## A. 原始 CSV 欄位\n")
    log("欄位：" + "、".join(df.columns) + "\n")
    log(f"總列數 {len(df)}；交易時段分布：{df['交易時段'].str.strip().value_counts().to_dict() if '交易時段' in df else '無此欄'}\n")
    oicol = [c for c in df.columns if "未沖銷" in c][0]
    df["oi"] = pd.to_numeric(df[oicol].str.replace(",", "").str.strip(), errors="coerce")
    df["mon"] = df["到期月份(週別)"].str.replace(r"\s", "", regex=True) if "到期月份(週別)" in df else df[[c for c in df.columns if "到期月份" in c][0]].str.replace(r"\s", "", regex=True)
    df["cp"] = df["買賣權"].str.strip()
    df["sess"] = df["交易時段"].str.strip() if "交易時段" in df else "一般"
    log("各交易時段的未平倉加總（檢查盤後列是否也帶未平倉）：\n")
    log(df.groupby(["sess", "cp"])["oi"].agg(["count", "sum"]).to_markdown() + "\n")
    g = df[df["sess"].str.contains("一般")].groupby(["mon", "cp"])["oi"].sum().unstack(fill_value=0)
    log("一般時段、依到期月份加總（pandas 獨立計算）：\n")
    log(g.to_markdown() + "\n")
    tot_raw = g.sum()
    log(f"一般時段合計：{tot_raw.to_dict()}\n")

    # 管線解析結果（未套用 ±15% 與到期過濾）
    parsed = tg.parse_options(txt).get(d, [])
    p = defaultdict(lambda: [0, 0])
    for code, exp, k, cp, px, oi, *_ in parsed:
        p[code][0 if cp == "C" else 1] += oi
    log("管線 parse_options 結果（未過濾）：\n")
    log("| 到期 | 買權 OI | 賣權 OI |\n|---|---|---|")
    for c in sorted(p):
        log(f"| {c} | {p[c][0]} | {p[c][1]} |")
    log("")
    pc = [sum(v[0] for v in p.values()), sum(v[1] for v in p.values())]
    log(f"管線合計：買權 {pc[0]}，賣權 {pc[1]}\n")

    # ---------- B ----------
    log("## B. 期交所 Put/Call 比報表（全市場）\n")
    try:
        r = requests.post("https://www.taifex.com.tw/cht/3/pcRatioDown",
                          data={"queryStartDate": ds, "queryEndDate": ds, "down_type": "1"}, headers=UA, timeout=60)
        t = r.content.decode("big5", "replace")
        open(os.path.join(OUT, f"raw_pcratio_{d:%Y%m%d}.csv"), "w", encoding="utf-8").write(t)
        log("```\n" + t.strip()[:600] + "\n```\n")
    except Exception as e:  # noqa: BLE001
        log(f"取得失敗：{e}\n")

    # ---------- C ----------
    log("## C. 期交所網頁每日行情表，逐履約價比對\n")
    for mon in [m for m in sorted(p) if re.search(r"F\d$|W\d$", m)][:2] + [m for m in sorted(p) if re.fullmatch(r"\d{6}", m)][:1]:
        try:
            form = {"queryType": "2", "marketCode": "0", "dateaddcnt": "", "commodity_id": "TXO", "commodity_id2": "",
                    "queryDate": ds, "MarketCode": "0", "commodity_idt": "TXO", "commodity_id2t": "", "commodity_id2t2": ""}
            r = requests.post("https://www.taifex.com.tw/cht/3/optDailyMarketReport", data=form, headers=UA, timeout=60)
            open(os.path.join(OUT, f"raw_html_{d:%Y%m%d}.html"), "wb").write(r.content)
            tabs = pd.read_html(io.StringIO(r.content.decode("utf-8", "replace")))
            big = max(tabs, key=len)
            big.columns = ["".join(str(x) for x in (c if isinstance(c, tuple) else (c,))).strip() for c in big.columns]
            log(f"網頁表格欄位：{'、'.join(big.columns[:20])}\n")
            mcol = [c for c in big.columns if "到期" in c][0]
            kcol = [c for c in big.columns if "履約價" in c][0]
            ccol = [c for c in big.columns if "買賣權" in c][0]
            ocol = [c for c in big.columns if "未沖銷" in c][0]
            sub = big[big[mcol].astype(str).str.replace(r"\s", "", regex=True) == mon]
            web = {(float(k), str(cp).strip()[:2]): pd.to_numeric(str(o).replace(",", ""), errors="coerce")
                   for k, cp, o in zip(sub[kcol], sub[ccol], sub[ocol])}
            mine = {(k, "買權" if cp == "C" else "賣權"): oi for code, exp, k, cp, px, oi, *_ in parsed if code == mon}
            diff = [(k, v, web.get(k)) for k, v in mine.items() if web.get(k) is not None and web.get(k) != v]
            miss = [k for k in web if k not in mine and (web[k] or 0) > 0]
            log(f"### {mon}\n網頁 {len(web)} 筆、管線 {len(mine)} 筆；數值不同 {len(diff)} 筆；網頁有 OI 但管線缺 {len(miss)} 筆\n")
            if diff[:10]:
                log("| 履約價／買賣 | 管線 | 網頁 |\n|---|---|---|")
                for k, a, b in diff[:10]:
                    log(f"| {k[0]:.0f} {k[1]} | {a} | {b} |")
                log("")
            top = sorted(mine.items(), key=lambda x: -x[1])[:5]
            log("管線 OI 前 5：" + "、".join(f"{k[0]:.0f}{k[1]} {v}（網頁 {web.get(k)}）" for k, v in top) + "\n")
            break  # 網頁一次回傳所有到期，處理一次即可
        except Exception as e:  # noqa: BLE001
            log(f"{mon} 比對失敗：{e}\n```\n{traceback.format_exc()[-600:]}\n```\n")
    open(os.path.join(OUT, "report.md"), "w", encoding="utf-8").write("\n".join(L))


if __name__ == "__main__":
    main()
