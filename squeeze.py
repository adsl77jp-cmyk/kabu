"""踏み上げ（空売りの買い戻し）と新規の空売りを、公表データと1分足から推測する

板や歩み値には「空売り」「買い戻し」の区別が載らないので、次の4つを組み合わせて推測する。
  日証金（貸借取引残高）… 制度信用の「貸株新規＝新規の空売り」「貸株返済＝買い戻し」を日次で公表（約2営業日遅れ）
                         品貸料率一覧 … 逆日歩・貸株超過・利用制限（空売り側の苦しさ）
  東証の信用残          … 信用売残の日々の増減（margin.py が取得）
  空売り残高報告（JPX）  … 発行済株式の0.5%以上を空売りしている大口の残高と増減
  1分足（yfinance）     … 出来高を伴う急騰（買い戻しの集中に見える場面）と、高値からの売り込み
いずれも「そう見える」までの推測で、確定はできない。
取得したデータは jsf_cache/・short_cache/ に日付ごとに保存し、掲載期間を過ぎた分も履歴として使う。
"""

import csv
import io
import json
import re
import sys
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

import margin

BASE = Path(__file__).resolve().parent
JSF = "https://www.taisyaku.jp/data/"
SHORT_PAGE = "https://www.jpx.co.jp/markets/public/short-selling/index.html"
_MEMO = {}  # サーバーを開いている間は1時間だけ使い回す


def _memo(key, fn, ttl=3600):
    hit = _MEMO.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _MEMO[key] = (time.time(), val)
    return val


def _num(s):
    s = (s or "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------- 日証金
def _load_jsf():
    """日証金の銘柄別残高・品貸料率。戻り値: {"zandaka": [(日付, {コード: 行})...], "shina": {コード: 行}, ...}"""
    cache = BASE / "jsf_cache"
    cache.mkdir(exist_ok=True)
    try:
        rows = list(csv.DictReader(io.StringIO(margin._get(JSF + "zandaka.csv", 30).decode("cp932", "replace"))))
        by_date = {}
        for r in rows:
            date = r["申込日"].replace("/", "")
            code = r["銘柄コード"].strip()
            if not code or "東証" not in r["取引所区分名"]:
                continue
            old = by_date.setdefault(date, {}).get(code)
            if old and old["kind"] == "確報" and r["速報／確報"] != "確報":
                continue
            by_date[date][code] = {"kind": r["速報／確報"],
                                   **{k: _num(r[f"{j}株数"]) for k, j in (
                                       ("loan_new", "融資新規"), ("loan_ret", "融資返済"), ("loan", "融資残高"),
                                       ("lend_new", "貸株新規"), ("lend_ret", "貸株返済"), ("lend", "貸株残高"))}}
        for date, d in by_date.items():
            (cache / f"z_{date}.json").write_text(json.dumps(d), encoding="utf-8")
    except Exception as e:
        print(f"  日証金の残高の取得失敗: {e}", file=sys.stderr)
    shina, shina_date = {}, None
    try:
        lines = margin._get(JSF + "shina.csv", 30).decode("cp932", "replace").splitlines()
        head = next(i for i, l in enumerate(lines) if l.startswith("貸借申込日"))
        for r in csv.DictReader(lines[head:]):
            code = (r.get("コード") or "").strip()
            if not code or "東証" not in (r.get("取引所区分") or ""):
                continue
            shina_date = r["貸借申込日"]
            shina[code] = {"price": _num(r["貸借値段（円）"]), "excess": _num(r["貸株超過株数"]),
                           "rate": _num(r["当日品貸料率（円）"]), "days": _num(r["当日品貸日数"]),
                           "max": _num(r["最高料率（円）"]), "limit": (r.get("制限") or "").strip(),
                           "rank": (r.get("応札倍率ランク") or "").strip()}
    except Exception as e:
        print(f"  日証金の品貸料率の取得失敗: {e}", file=sys.stderr)
    history = sorted((p.stem[2:], json.loads(p.read_text(encoding="utf-8"))) for p in cache.glob("z_*.json"))
    return {"zandaka": history, "shina": shina, "shina_date": shina_date,
            "date": history[-1][0] if history else None}


# ---------------------------------------------------------------- 空売り残高報告
def _parse_short(xls):
    df = pd.read_excel(io.BytesIO(xls), header=None)
    out = []
    for _, r in df.iloc[8:].iterrows():
        code = str(r[2]).strip()
        if code in ("nan", "") or pd.isna(r[10]):
            continue
        code = code[:4] if len(code) == 5 and code.endswith("0") else code
        out.append({"code": code, "date": str(pd.Timestamp(r[1]).date()) if pd.notna(r[1]) else None,
                    "who": str(r[5]).strip(),
                    "key": "|".join(str(r[i]).strip() for i in (5, 7, 9)),
                    "ratio": float(r[10]), "shares": _num(str(r[11])),
                    "prev": float(r[14]) if pd.notna(r[14]) and str(r[14]).strip() not in ("", "-") else None})
    return out


def _load_short():
    """空売り残高報告。戻り値: {"rows": [行...（全保存分）], "date": 最新の公表日}"""
    cache = BASE / "short_cache"
    cache.mkdir(exist_ok=True)
    try:
        body = margin._get(SHORT_PAGE, 20).decode("utf-8", "replace")
        for href, date in set(re.findall(r'href="([^"]+?(\d{8})_Short_Positions\.xlsx?)"', body)):
            path = cache / f"{date}.json"
            if not path.exists():
                print(f"空売り残高報告（{date}）取得中…", file=sys.stderr)
                path.write_text(json.dumps(_parse_short(margin._get("https://www.jpx.co.jp" + href, 60)),
                                           ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"  空売り残高報告の取得失敗: {e}", file=sys.stderr)
    rows, last = [], None
    for p in sorted(cache.glob("*.json")):
        for r in json.loads(p.read_text(encoding="utf-8")):
            r["pub"] = p.stem
            rows.append(r)
        last = p.stem
    return {"rows": rows, "date": last, "first": min((r["pub"] for r in rows), default=None)}


def short_positions(code, sr, recent_pubs=10):
    """大口の空売り残高（報告者ごとの最新）と、直近の増減"""
    latest = {}
    for r in sr["rows"]:
        if r["code"] != code:
            continue
        k = r["key"]
        if k not in latest or (r["date"] or "", r["pub"]) >= (latest[k]["date"] or "", latest[k]["pub"]):
            latest[k] = r
    pubs = sorted({r["pub"] for r in sr["rows"]})[-recent_pubs:]
    holders = sorted((r for r in latest.values() if r["ratio"] >= 0.005), key=lambda r: -r["ratio"])
    moves = [r for r in latest.values() if r["pub"] in pubs and r["prev"] is not None]
    return {"total": sum(r["ratio"] for r in holders), "holders": holders,
            "up": [r for r in moves if r["ratio"] > r["prev"]],
            "down": [r for r in moves if r["ratio"] < r["prev"]],
            "new": [r for r in latest.values() if r["pub"] in pubs and r["prev"] is None and r["ratio"] >= 0.005]}


# ---------------------------------------------------------------- 1分足
def fetch_minutes(codes):
    """直近5営業日の1分足 {コード: DataFrame(Open/High/Low/Close/Volume)}"""
    syms = [f"{c}.T" for c in codes]
    try:
        df = yf.download(syms, period="5d", interval="1m", auto_adjust=True, progress=False,
                         threads=True, group_by="ticker")
    except Exception as e:
        print(f"  1分足の取得失敗: {e}", file=sys.stderr)
        return {}
    out = {}
    for c, s in zip(codes, syms):
        try:
            one = df[s].dropna(subset=["Close"])
        except KeyError:
            continue
        if len(one) >= 60:
            out[c] = one
    return out


def _clusters(mask, gap_min=10):
    """True の1分足のうち、10分以上あいたものだけを別の場面として数える"""
    picks, last = [], None
    for t in mask[mask].index:
        if last is None or (t - last).total_seconds() > gap_min * 60:
            picks.append(t)
        last = t
    return picks


def intraday(df):
    """出来高を伴う急騰（買い戻しが集中したように見える場面）と、高値からの売り込みを探す"""
    day = pd.Series(df.index.date, index=df.index)
    bar_no = df.groupby(day.values).cumcount()  # 寄り付き直後の5本は出来高が膨らむので基準から外す
    base = df["Volume"][(bar_no >= 5) & (df["Volume"] > 0)].median()
    if not base or pd.isna(base):
        return None
    g = df.groupby(day.values)
    r5 = g["Close"].pct_change(5)
    v5 = g["Volume"].transform(lambda v: v.rolling(5).sum()) / (5 * base)
    th = max(0.01, 3 * float(r5.std()))  # 普段の5分の値動きの3倍（最低1%）
    hi10 = g["High"].transform(lambda h: h.rolling(10).max())
    day_hi = g["High"].cummax()
    surge = (r5 >= th) & (v5 >= 3)
    selloff = (df["Close"] / hi10 - 1 <= -th) & (v5 >= 3) & (hi10 >= day_hi * 0.995)
    last_day = day.iloc[-1]
    today = df[day == last_day]
    prev = df[day < last_day]
    up_vol = today["Volume"][today["Close"] > today["Open"]].sum()
    dn_vol = today["Volume"][today["Close"] < today["Open"]].sum()

    def fmt(ts):
        return [(t.strftime("%m/%d %H:%M"), float(r5[t]), float(v5[t])) for t in ts]

    up_ts, dn_ts = _clusters(surge), _clusters(selloff)
    return {"date": str(last_day), "th": th,
            "up": fmt([t for t in up_ts if t.date() == last_day]),
            "down": fmt([t for t in dn_ts if t.date() == last_day]),
            "up_5d": len(up_ts), "down_5d": len(dn_ts),
            "up_share": up_vol / (up_vol + dn_vol) if up_vol + dn_vol else None,
            "broke_high": bool(len(prev) and today["High"].max() > prev["High"].max()),
            "day_ret": float(today["Close"].iloc[-1] / prev["Close"].iloc[-1] - 1) if len(prev) else None}


# ---------------------------------------------------------------- 判定
def _ret_between(close, d0, d1):
    s = close.dropna()
    if s.empty:
        return None
    idx = s.index.tz_localize(None) if s.index.tz is not None else s.index
    a = idx.searchsorted(pd.Timestamp(d0), side="right") - 1
    b = idx.searchsorted(pd.Timestamp(d1), side="right") - 1
    return float(s.iloc[b] / s.iloc[a] - 1) if a >= 0 and b > a else None


def judge(x, close, mg, jsf, sr, minutes):
    """1銘柄の推測。x は diagnose の行（price・avg_vol・margin を使う）"""
    code, sym = x["code"], f"{x['code']}.T"
    px = close[sym] if sym in close else pd.Series(dtype=float)
    avg_vol = x.get("avg_vol")
    m = x.get("margin") or {}
    sq, sh, sq_notes, sh_notes = {"fuel": 0, "press": 0, "trig": 0}, 0, [], []

    # 空売りの量（東証の信用売残・大口の空売り）
    sell_days = m["sell"] / avg_vol if m.get("sell") is not None and avg_vol else None
    if sell_days is not None:
        if sell_days >= 3:
            sq["fuel"] += 2
            sq_notes.append(f"信用売残が出来高の{sell_days:.1f}日分（買い戻しの燃料が多い）")
        elif sell_days >= 1:
            sq["fuel"] += 1
            sq_notes.append(f"信用売残が出来高の{sell_days:.1f}日分")
    sp = short_positions(code, sr)
    if sp["total"] >= 0.05:
        sq["fuel"] += 2
    elif sp["total"] >= 0.02:
        sq["fuel"] += 1
    if sp["total"] >= 0.02:
        sq_notes.append(f"大口の空売りが発行済株式の{sp['total']:.1%}")

    # 空売り側の苦しさ（逆日歩・制限・含み損）
    s = jsf["shina"].get(code)
    if s and s.get("rate"):
        per_day = s["rate"] / (s["days"] or 1)
        hot = s["price"] and per_day / s["price"] >= 0.0005
        sq["press"] += 2 if hot else 1
        sq_notes.append(f"逆日歩 {per_day:.2f}円/日{'（株価の0.05%以上で重い）' if hot else ''}")
    if s and s.get("limit"):
        sq["press"] += 1
        sq_notes.append(f"日証金の利用制限：{s['limit']}")
    if m.get("sell_gap") is not None and m["sell_gap"] >= 0.05 and (sell_days or 0) >= 0.3:
        sq["press"] += 1
        sq_notes.append(f"株価が空売りの平均（{m['sell_cost']:,.0f}円）より{m['sell_gap']:+.0%}：空売り勢は含み損")

    # 日証金：新規の空売り（貸株新規）と買い戻し（貸株返済）
    zh = [(d, z[code]) for d, z in jsf["zandaka"] if code in z][-5:]
    jsf_info = None
    if zh:
        new = sum(z["lend_new"] or 0 for _, z in zh)
        ret = sum(z["lend_ret"] or 0 for _, z in zh)
        last = zh[-1][1]
        r = _ret_between(px, zh[0][0], zh[-1][0]) if len(zh) > 1 else None
        jsf_info = {"days": len(zh), "from": zh[0][0], "to": zh[-1][0], "new": new, "ret": ret,
                    "lend": last["lend"], "loan": last["loan"],
                    "loan_new": sum(z["loan_new"] or 0 for _, z in zh),
                    "loan_ret": sum(z["loan_ret"] or 0 for _, z in zh)}
        lend = last["lend"] or 0
        # 1日の出来高の2%に満たない増減は、値動きへの影響が小さいので数えない
        floor = max(lend * 0.05, (avg_vol or 0) * 0.02 * len(zh), 1)
        if ret > new and ret - new >= floor:
            sq["trig"] += 1
            sq_notes.append(f"日証金で買い戻し（貸株返済{ret:,.0f}株）が新規の空売り（{new:,.0f}株）を上回る")
        elif new > ret and new - ret >= floor:
            sh += 2 if new - ret >= lend * 0.2 else 1
            sh_notes.append(f"日証金で新規の空売り（貸株新規{new:,.0f}株）が買い戻し（{ret:,.0f}株）を上回る")
        if r is not None and new > ret and r < -0.03:
            sh_notes.append(f"下落（{r:+.0%}）しながら空売りが増加：売り崩しの可能性")

    # 東証の信用売残の増減（日次の履歴）
    hist = [(d, h[code]["sell"]) for d, h in (mg or {}).get("history", []) if code in h][-6:]
    if len(hist) >= 2 and hist[0][1]:
        chg = hist[-1][1] / hist[0][1] - 1
        r = _ret_between(px, hist[0][0], hist[-1][0])
        if chg <= -0.15 and r is not None and r > 0:
            sq["trig"] += 1
            sq_notes.append(f"上昇（{r:+.0%}）しながら信用売残が{chg:+.0%}：買い戻しが進んでいる")
        elif chg >= 0.2:
            sh += 1
            sh_notes.append(f"信用売残が{len(hist)}日で{chg:+.0%}")

    # 大口の空売りの増減
    if sp["down"]:
        sq["trig"] += 1
        sq_notes.append("大口が空売りを減らした：" + "、".join(f"{r['who'][:20]} {r['prev']:.2%}→{r['ratio']:.2%}"
                                                          for r in sp["down"][:2]))
    grow = sp["up"] + sp["new"]
    if grow:
        sh += 2 if len(grow) >= 2 else 1
        sh_notes.append("大口が空売りを増やした：" + "、".join(
            f"{r['who'][:20]} {'新規' if r['prev'] is None else format(r['prev'], '.2%')}→{r['ratio']:.2%}"
            for r in grow[:2]))

    # 1分足
    it = intraday(minutes[code]) if code in minutes else None
    if it:
        if it["up"]:
            sq["trig"] += 1
            sq_notes.append(f"{it['date']} に出来高を伴う急騰 {len(it['up'])}回（{it['up'][0][0][-5:]}〜）")
        if it["broke_high"] and (it["day_ret"] or 0) > 0:
            sq["trig"] += 1
            sq_notes.append("5日間の高値を更新（損切りの買い戻しが出やすい価格帯）")
        if len(it["down"]) >= 2 or (it["down"] and it["up_share"] is not None and it["up_share"] < 0.4):
            sh += 1
            sh_notes.append(f"{it['date']} に高値から出来高を伴って売り込まれた場面 {len(it['down'])}回")

    fuel_press = sq["fuel"] + sq["press"]
    if sq["trig"] >= 2 and sq["fuel"] >= 1:
        sq_label = ("ok", "踏み上げの兆候あり")
    elif fuel_press >= 3:
        sq_label = ("warn", "踏み上げ予備軍（空売りが溜まっている）")
    elif sq["trig"] >= 2:
        sq_label = ("warn", "急騰しているが空売りは少ない（新規の買いが中心か）")
    else:
        sq_label = ("na", "踏み上げの材料は少ない")
    sh_label = ("ng", "新規の空売りが増えている") if sh >= 2 else ("warn", "空売りがやや増加") if sh == 1 \
        else ("na", "空売りの増加は見えない")
    return {"squeeze": sq_label, "short": sh_label, "sq_score": sq["fuel"] + sq["press"] + sq["trig"] * 2,
            "sh_score": sh, "sq_notes": sq_notes, "sh_notes": sh_notes, "sell_days": sell_days,
            "shina": s, "sp": sp, "jsf": jsf_info, "it": it}


def analyze(rows, close, mg):
    """diagnose の行（エラーのないもの）について推測を付ける。戻り値: {コード: 推測}, データの日付"""
    codes = [x["code"] for x in rows if not x.get("error")]
    if not codes:
        return {}, {}
    jsf = _memo("jsf", _load_jsf)
    sr = _memo("short", _load_short)
    minutes = fetch_minutes(codes)
    out = {x["code"]: judge(x, close, mg, jsf, sr, minutes) for x in rows if not x.get("error")}
    dates = {"jsf": jsf["date"], "shina": jsf["shina_date"], "short": sr["date"], "short_first": sr["first"],
             "minute": max((o["it"]["date"] for o in out.values() if o["it"]), default=None)}
    return out, dates
