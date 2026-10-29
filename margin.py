"""信用取引残高（東証「銘柄別信用取引残高」日次PDF）から需給を判定する

信用倍率 = 信用買残 ÷ 信用売残
  - 買残が多い（倍率が高い）… 将来の「返済の売り」が控えていて、上値が重く下がりやすい
  - 売残が多い（倍率1倍未満）… 将来の「買い戻し」が控えていて、上がると踏み上げやすい
買残の重さは「買残 ÷ 20日平均出来高」（何日分の出来高か）でも見る。
解析結果は margin_cache/日付.json に保存し、同じ日のPDFは2回読まない。
"""

import io
import json
import re
import sys
import urllib.request
from pathlib import Path

import pandas as pd
from pypdf import PdfReader

BASE = Path(__file__).resolve().parent
PAGE = "https://www.jpx.co.jp/markets/statistics-equities/margin/01.html"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
ROW = re.compile(
    r"(\w{4})\w JP\w{10} 株数 Shs\. ([\d,]+) (▲ ?)?([\d,]+) ([\d.]+%|\*) ([\d,]+) (▲ ?)?([\d,]+) ([\d.]+%|\*)")


def _get(url, timeout=60):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def _n(s):
    return int(s.replace(",", ""))


VAL = re.compile(r"(\w{4})\w\s+J\s*P\s*(?:[0-9A-Z]\s*){10}金\s*額\s*V\s*a\s*l\s*\.\s*([^\n]*)")
VALID = re.compile(r"^\d{1,3}(,\d{3})*$")


def _first_amount(part):
    """「1,923,184 ,300」「32,21 8,702,500」のようにPDFで崩れた金額をつなぎ直し、最初の金額を返す"""
    part = part.split("▲")[0]
    toks = re.findall(r"[\d,]+", part)
    if not toks:
        return None
    cur, i = toks[0], 1
    while i < len(toks) and (not VALID.match(cur) or toks[i].startswith(",")):
        cur += toks[i]
        i += 1
    return _n(cur) if VALID.match(cur) else None


def _parse(pdf_bytes):
    text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf_bytes)).pages)
    text = re.sub(r"(\d),\s+(\d)", r"\1,\2", text)  # 「1, 200」のような崩れを直す
    out = {}
    for m in ROW.finditer(text):
        code, sell, sneg, schg, _, buy, bneg, bchg, bratio = m.groups()
        out[code] = {"sell": _n(sell), "buy": _n(buy),
                     "sell_chg": -_n(schg) if sneg else _n(schg),
                     "buy_chg": -_n(bchg) if bneg else _n(bchg),
                     "buy_listed": None if bratio == "*" else float(bratio.rstrip("%"))}
    # 金額の行：売残金額・買残金額（約定代金ベース）。金額÷株数＝平均の約定単価
    for m in VAL.finditer(text):
        code, rest = m.groups()
        if code not in out:
            continue
        parts = re.split(r"\s-\s", " " + rest + " ")
        if len(parts) >= 2:
            out[code]["sell_val"] = _first_amount(parts[0])
            out[code]["buy_val"] = _first_amount(parts[1])
    return out


def load():
    """最新日と、一覧の中で最も古い日（約1週間前）の残高"""
    body = _get(PAGE, timeout=20).decode("utf-8", "replace")
    links = re.findall(r'href="([^"]+?(\d{8})_mtall\.pdf)"', body)
    if not links:
        return None
    cache_dir = BASE / "margin_cache"
    cache_dir.mkdir(exist_ok=True)

    def by_date(href, date):
        path = cache_dir / f"{date}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        print(f"信用残（{date}）解析中…", file=sys.stderr)
        data = _parse(_get("https://www.jpx.co.jp" + href))
        path.write_text(json.dumps(data), encoding="utf-8")
        return data

    # 東証は直近5営業日分しか公開しないので、掲載中の日は全部保存して履歴をためる
    for href, date in links:
        by_date(href, date)
    (h_new, d_new), (h_old, d_old) = links[0], links[-1]
    history = sorted((p.stem, json.loads(p.read_text(encoding="utf-8")))
                     for p in cache_dir.glob("*.json") if p.stem.isdigit())
    return {"date": d_new, "old_date": d_old, "now": by_date(h_new, d_new),
            "old": by_date(h_old, d_old) if d_old != d_new else {}, "history": history}


def avg_cost(code, mg, close, volume, months=6):
    """信用買い（6か月以内）の平均取得価格の推定

    1) 履歴の最初の日の買残は、その前6か月の出来高加重平均価格（VWAP）で買われたとみなす
    2) 以降、買残が増えた分はその日の終値で買われ、減った分は古い順に返済されたとみなす（先入れ先出し）
    3) 6か月を過ぎた分は返済期限で消えたとみなす
    戻り値: dict（推定平均・計算方法・履歴の日数）。close/volume は日付インデックスの Series。
    """
    close, volume = close.dropna(), volume.reindex(close.index).fillna(0)
    if len(close) < 20:
        return None
    # 東証の公表値（買残金額÷買残株数）があればそれを使う。金額は約定代金ベースなので平均取得単価になる
    x = (mg or {}).get("now", {}).get(code, {})
    if x.get("buy_val") and x.get("buy"):
        cost = x["buy_val"] / x["buy"]
        price = float(close.iloc[-1])
        if 0.3 < cost / price < 3:  # PDFの読み取り崩れの保険
            out = {"cost": cost, "method": f"東証公表の買残金額÷買残株数（{mg['date']}時点）", "days": None}
            if x.get("sell_val") and x.get("sell"):
                out["sell_cost"] = x["sell_val"] / x["sell"]
            return out
    hist = [(d, h[code]["buy"]) for d, h in (mg or {}).get("history", []) if code in h]
    window = 21 * months

    def vwap(end_idx):
        c, v = close.iloc[max(0, end_idx - window):end_idx + 1], volume.iloc[max(0, end_idx - window):end_idx + 1]
        return float((c * v).sum() / v.sum()) if v.sum() else float(c.mean())

    def idx_on_or_before(date_str):
        ts = pd.Timestamp(date_str)
        pos = close.index.tz_localize(None).searchsorted(ts, side="right") - 1 \
            if close.index.tz is not None else close.index.searchsorted(ts, side="right") - 1
        return max(pos, 0)

    if len(hist) < 2:
        return {"cost": vwap(len(close) - 1), "method": "6か月VWAP（近似）", "days": len(hist)}

    first_date, first_buy = hist[0]
    i0 = idx_on_or_before(first_date)
    lots = [[close.index[i0], first_buy, vwap(i0)]]  # [日付, 株数, 単価]
    prev = first_buy
    for date, buy in hist[1:]:
        i = idx_on_or_before(date)
        d = buy - prev
        if d > 0:
            lots.append([close.index[i], d, float(close.iloc[i])])
        elif d < 0:
            remove = -d
            while remove > 0 and lots:
                take = min(remove, lots[0][1])
                lots[0][1] -= take
                remove -= take
                if lots[0][1] <= 0:
                    lots.pop(0)
        limit = close.index[i] - pd.Timedelta(days=183)
        lots = [l for l in lots if l[0] >= limit]
        prev = buy
    shares = sum(l[1] for l in lots)
    if shares <= 0:
        return None
    cost = sum(l[1] * l[2] for l in lots) / shares
    return {"cost": cost, "method": f"信用残の増減から推定（履歴{len(hist)}日）", "days": len(hist)}


def judge(code, mg, avg_volume=None, ret_week=None):
    """1銘柄の需給。戻り値: dict（倍率・買残日数・週間の増減・判定・方向）"""
    if not mg or code not in mg["now"]:
        return None
    x, o = mg["now"][code], mg["old"].get(code)
    ratio = x["buy"] / x["sell"] if x["sell"] else float("inf")
    days = x["buy"] / avg_volume if avg_volume else None
    buy_wk = (x["buy"] / o["buy"] - 1) if o and o["buy"] else None
    sell_wk = (x["sell"] / o["sell"] - 1) if o and o["sell"] else None

    sell_days = x["sell"] / avg_volume if avg_volume else None
    heavy = x["buy_listed"] is not None and x["buy_listed"] >= 5  # 買残が発行済株式の5%以上
    notes, tilt = [], 0
    if ratio < 1 and (sell_days is None or sell_days >= 0.3):
        notes.append("売り長：買い戻しで踏み上げやすい")
        tilt += 1
    elif heavy or (days is not None and days >= 5) or (days is None and ratio >= 8):
        notes.append("買い長：返済売りが控え上値が重い")
        tilt -= 1
    elif ratio >= 3 and days is not None and days >= 2:
        notes.append("やや買い長")
    elif ratio >= 8 and days is not None:
        notes.append(f"倍率は高いが買残は出来高の{days:.1f}日分で影響小")
    if buy_wk is not None and ret_week is not None:
        if buy_wk >= 0.2 and ret_week < 0:
            notes.append("下落中に買残が急増（しこり）")
            tilt -= 1
        elif buy_wk <= -0.15 and ret_week > 0:
            notes.append("買残が減りながら上昇（需給改善）")
            tilt += 1
    if sell_wk is not None and sell_wk >= 0.3 and ret_week is not None and ret_week > 0:
        notes.append("上昇中に売残が増加（踏み上げ余地）")
        tilt += 1
    direction = "上がりやすい" if tilt > 0 else "下がりやすい" if tilt < 0 else "中立"
    return {"ratio": ratio, "buy": x["buy"], "sell": x["sell"], "days": days, "buy_listed": x["buy_listed"],
            "buy_wk": buy_wk, "sell_wk": sell_wk, "notes": notes, "tilt": tilt, "direction": direction}
