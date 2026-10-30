"""テーマ株 注目度スコア

themes.yaml のテーマごとに、構成銘柄の株価・出来高とニュース件数から
「注目度スコア（0〜100）」を計算し、ランキングを表示・保存する。

スコアの中身（各指標は、そのテーマ自身の過去60営業日と比べた偏差＝zスコア）
  - 売買代金倍率   45%  当日売買代金 ÷ 過去20日平均（お金が集まったか）
  - 対TOPIX超過    35%  テーマ平均騰落率 − TOPIX(1306)騰落率（強く買われたか）
  - 上昇銘柄比率   20%  構成銘柄のうち上昇した割合（テーマ全体に広がったか）
  → 株価スコア = 50 + zの加重平均 × 16.7（0〜100にクリップ）。50が平常、70超で明確に注目。
  - ニュース倍率: 直近の1日あたり記事数 ÷ その前7日の1日平均（Googleニュース）
  → 総合スコア = 株価スコア 80% + ニューススコア 20%

評価（銘柄ごと）= 成長度 60% + 割安度 40%   ※業績の伸びを最重視
対象銘柄 = テーマの構成銘柄 ＋ 東証プライム・スタンダードの全内国株式（日経225の構成銘柄もすべて含む）

成長度（0〜100、全銘柄の中での順位から算出）
  - 営業利益の3年平均成長率 30% ・連続増益年数（最大3）20% ・売上の3年平均成長率 20%
  - 直近四半期の利益 前年同期比 15% ・直近四半期の売上 前年同期比 15%
  - 銀行・保険は営業利益の代わりに純利益。直近期が赤字なら最低評価、黒字転換は+50%扱い

割安度（0〜100、銘柄ごと。全銘柄の中での順位を平均したもの）
  - PER（予想があれば予想、なければ実績。低いほど高得点）
  - PBR（低いほど高得点） ・ROE（高いほど高得点） ・配当利回り（高いほど高得点）
  - 予想PERが実績PERの1/2.5未満の銘柄は「※」を付け、データ誤りの疑いとして実績PERを使う
  - テーマの割安度 = 構成銘柄の割安度の中央値

先回りスコア（テーマごと。いち早く注目テーマを見つけるため）
  = ニューススコア 35% + イベント・決算の近さ 25%（events.yaml と yfinance の決算予定日）
    + 株価がまだ動いていない度合い（100 − 株価スコア）20%
  ＋ watchwords.yaml の言葉のニュース急増を「新テーマ候補」として表示

使い方:  run.bat をダブルクリック（または python theme_score.py [--no-news]）
出力:    コンソールにランキング、report.html、history.csv（毎日追記）
"""

import argparse
import datetime as dt
import email.utils
import html
import io
import json
import logging
import math
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
import yfinance as yf

import margin

BASE = Path(__file__).resolve().parent
SITE_URL = "https://adsl77jp-cmyk.github.io/kabu/"  # 公開ページのURL（sitemap.xml・canonical に使う）
SITE_DESC = ("日経平均の終値と、いま東京市場で資金が集まっているテーマ・銘柄を毎営業日更新。"
             "株価・売買代金・ニュース・決算開示・信用残から集計した日本株の分析サイトです。")
BENCH = "1306.T"  # TOPIX連動ETF（TOPIX指数の代わり）
WEIGHTS = {"turnover": 0.45, "excess": 0.35, "breadth": 0.20}
NEWS_WEIGHT = 0.20
Z_WINDOW = 60
TURNOVER_WINDOW = 20
Z_CLIP = 3.0


def load_themes():
    with open(BASE / "themes.yaml", encoding="utf-8") as f:
        themes = yaml.safe_load(f)
    for t in themes.values():
        t["tickers"] = {str(k): v for k, v in t["tickers"].items()}
    return themes


def fill_last_close(close):
    """最終日の終値が欠けた銘柄を、60分足のその日最後の値で補う（yfinanceは日足の最終日が欠けることがある）"""
    day = close.index[-1]
    missing = list(close.columns[close.iloc[-1].isna()])
    for i in range(0, len(missing), 400):
        part = missing[i:i + 400]
        try:
            h = yf.download(part, period="5d", interval="60m", auto_adjust=True, progress=False, threads=True)["Close"]
        except Exception:
            continue
        if isinstance(h, pd.Series):
            h = h.to_frame(part[0])
        h = h[[ts.date() == day.date() for ts in h.index]]
        if h.empty:
            continue
        last = h.ffill().iloc[-1]
        for s in part:
            if s in last and pd.notna(last[s]):
                close.at[day, s] = float(last[s])
    return close


UNIVERSE_MARKETS = ("プライム（内国株式）", "スタンダード（内国株式）")
LIST_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"


def load_universe():
    """東証プライム・スタンダードの内国株式 {コード: {"name", "market", "sector"}}（1日1回取得して保存）"""
    path = BASE / "listed_universe.json"
    cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if cache.get("date") == str(dt.date.today()):
        return cache["stocks"]
    try:
        body = margin._get(LIST_PAGE, timeout=20).decode("utf-8", "replace")
        href = re.findall(r'href="([^"]+data_j\.xlsx?)"', body)[0]
        df = pd.read_excel(io.BytesIO(margin._get("https://www.jpx.co.jp" + href)), dtype={"コード": str})
        df = df[df["市場・商品区分"].isin(UNIVERSE_MARKETS)]
        stocks = {r["コード"]: {"name": unicodedata.normalize("NFKC", r["銘柄名"]),
                               "market": r["市場・商品区分"].split("（")[0], "sector": r["33業種区分"]}
                  for _, r in df.iterrows()}
        path.write_text(json.dumps({"date": str(dt.date.today()), "stocks": stocks}, ensure_ascii=False),
                        encoding="utf-8")
        return stocks
    except Exception as e:
        print(f"  上場銘柄一覧の取得失敗（前回の一覧を使います）: {e}", file=sys.stderr)
        return cache.get("stocks", {})


def _download(symbols, threads=True):
    """日足1年分の (終値, 出来高)。銘柄が多いときは500ずつに分けて取る"""
    closes, vols = [], []
    for i in range(0, len(symbols), 500):
        part = symbols[i:i + 500]
        df = yf.download(part, period="1y", interval="1d", auto_adjust=True, progress=False, threads=threads)
        if df.empty:
            continue
        c, v = df["Close"], df["Volume"]
        if isinstance(c, pd.Series):
            c, v = c.to_frame(part[0]), v.to_frame(part[0])
        closes.append(c)
        vols.append(v)
    if not closes:
        return pd.DataFrame(), pd.DataFrame()
    return pd.concat(closes, axis=1), pd.concat(vols, axis=1)


def fetch_prices(codes):
    symbols = sorted({f"{c}.T" for c in codes}) + [BENCH]
    close, volume = _download(symbols)
    # 並列取得で失敗した銘柄（"database is locked" など）を取り直す。多いときはまとめて1回だけ
    failed = [s for s in symbols if s not in close or close[s].dropna().empty]
    retry = [failed] if len(failed) > 30 else [[s] for s in failed]
    for part in retry:
        c, v = _download(part, threads=False)
        got = [s for s in part if s in c and not c[s].dropna().empty]
        if got:
            close = pd.concat([close.drop(columns=got, errors="ignore"), c[got]], axis=1)
            volume = pd.concat([volume.drop(columns=got, errors="ignore"), v[got]], axis=1)
    # 東証の営業日だけ残す（半数以上の銘柄に値がある日）。yfinanceは最終日の終値が欠けることがあるので、
    # 欠けた銘柄は fast_info の直近値で補う
    # （yfinanceは最終日に出来高だけ入って終値が欠けることがあるので、出来高で営業日を判定する）
    valid = (close.notna() | (volume.fillna(0) > 0)).sum(axis=1) >= close.shape[1] * 0.5
    close, volume = close[valid].copy(), volume[valid]
    return fill_last_close(close), volume


def rolling_z(s):
    past = s.shift(1).rolling(Z_WINDOW, min_periods=20)
    z = (s - past.mean()) / past.std()
    return z.clip(-Z_CLIP, Z_CLIP)


def to_score(z):
    return (50 + z * 50 / Z_CLIP).clip(0, 100)


def theme_metrics(symbols, close, volume, bench_ret):
    c, v = close[symbols], volume[symbols]
    ret = c.pct_change(fill_method=None)
    turnover = (c * v).sum(axis=1, min_count=1)
    to_ratio = turnover / turnover.shift(1).rolling(TURNOVER_WINDOW).mean()
    theme_ret = ret.mean(axis=1)
    excess = theme_ret - bench_ret
    breadth = (ret > 0).sum(axis=1) / ret.notna().sum(axis=1)
    z = (WEIGHTS["turnover"] * rolling_z(np.log(to_ratio))
         + WEIGHTS["excess"] * rolling_z(excess)
         + WEIGHTS["breadth"] * rolling_z(breadth))
    return pd.DataFrame({
        "price_score": to_score(z),
        "to_ratio": to_ratio,
        "to_dod": turnover / turnover.shift(1) - 1,
        "theme_ret": theme_ret,
        "excess": excess,
        "breadth": breadth,
    }), ret


def news_window_days(last_trade_date, today):
    """数える期間（日数）。前の取引日から何日あいたか。
    平日は1（24時間）、月曜朝は3（金曜朝から72時間）、連休明けはその分だけ長くする。
    土日・祝日は記事が少ないので、24時間だけだと週明けの倍率が低く出てしまうのを防ぐ。"""
    return min(max(1, (today - last_trade_date).days), 5)


def fetch_news(keyword, now, window=1):
    q = urllib.parse.quote(f"{keyword} when:{window + 7}d")
    url = f"https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            root = ET.fromstring(r.read())
    except Exception as e:
        print(f"  ニュース取得失敗: {keyword} ({e})", file=sys.stderr)
        return None
    recent = prev = 0
    headlines = []
    for item in root.iter("item"):
        try:
            pub = email.utils.parsedate_to_datetime(item.findtext("pubDate"))
        except (TypeError, ValueError):
            continue
        age = (now - pub).total_seconds() / 86400
        if age < window:
            recent += 1
            headlines.append((pub, item.findtext("title") or "", item.findtext("link") or ""))
        elif age < window + 7:
            prev += 1
    # 1日あたりの記事数どうしで比べる（+1 で件数ゼロ時の暴れを抑える）
    ratio = (recent / window + 1) / (prev / 7 + 1)
    # 表示用の見出しは、検索語そのものを含むものを優先（Googleニュースは関係の薄い記事も返すため）
    tokens = [w for w in keyword.split() if w != "株"]
    headlines.sort(key=lambda h: (not any(w in h[1] for w in tokens), -h[0].timestamp()))
    return {"news_recent": recent, "news_ratio": ratio,
            "headlines": [(t, l) for _, t, l in headlines[:3]]}


def news_score(ratio):
    return float(np.clip(50 + math.log2(ratio) * 50 / Z_CLIP, 0, 100))


def load_events(today, horizon=14):
    """events.yaml から今日以降 horizon 日以内（開催中を含む）のイベント"""
    path = BASE / "events.yaml"
    if not path.exists():
        return []
    out = []
    for e in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
        start = dt.date.fromisoformat(str(e["date"]))
        end = dt.date.fromisoformat(str(e.get("end", e["date"])))
        if end >= today and (start - today).days <= horizon:
            out.append({**e, "start": start, "days": max((start - today).days, 0)})
    return sorted(out, key=lambda e: e["start"])


def scan_watchwords(now, window=1):
    path = BASE / "watchwords.yaml"
    if not path.exists():
        return []
    words = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda w: (w, fetch_news(f"{w} 株", now, window)), words))
    return sorted(((w, n) for w, n in res if n), key=lambda x: -x[1]["news_ratio"])


FUND_KEYS = {"trailingPE": "per", "forwardPE": "fper", "priceToBook": "pbr",
             "returnOnEquity": "roe", "dividendYield": "div",
             "revenueGrowth": "q_rev", "earningsGrowth": "q_earn"}  # q_* = 直近四半期の前年同期比
CACHE_VERSION = 5  # v5: 銘柄ごとに取得日（at）を持つ
FUND_MAX_AGE = 7   # テーマ外の銘柄の財務指標は何日まで使い回すか（年次業績は頻繁に変わらないので）
FUND_PER_RUN = 500  # テーマ外の銘柄は1回の実行でこの件数まで取る（一度に大量に取るとYahooに拒否されるため、数日かけて埋める）
GROWTH_WEIGHT = 0.6  # 評価 = 成長度60% + 割安度40%


def growth_from_statement(series):
    """年次の数値（新しい順）から 3年CAGR と 連続増加年数 を出す"""
    v = [x for x in series if x is not None and not pd.isna(x)][:4]
    if len(v) < 2:
        return None, None
    latest, oldest, n = v[0], v[-1], len(v) - 1
    if latest <= 0:
        cagr = -1.0  # 直近が赤字
    elif oldest <= 0:
        cagr = 0.5   # 黒字転換（安定成長ではないので控えめに評価）
    else:
        cagr = min((latest / oldest) ** (1 / n) - 1, 1.0)
    streak = 0
    for a, b in zip(v, v[1:]):
        if a > b:
            streak += 1
        else:
            break
    return cagr, streak


class _NotFoundFilter(logging.Filter):
    """yfinance が出す「Quote not found」（上場廃止・新規上場直後などでYahooにない銘柄）を拾って記録し、画面には出さない"""
    def __init__(self):
        super().__init__()
        self.symbols = set()

    def filter(self, record):
        m = re.search(r"Quote not found for symbol: (\S+?)\"", record.getMessage())
        if m:
            self.symbols.add(m.group(1))
            return False
        return True


_not_found = _NotFoundFilter()
logging.getLogger("yfinance").addFilter(_not_found)


FUND_CACHE = BASE / "fundamentals_cache.json"


def _fund_cache():
    """{コード: 財務指標}（各レコードに取得日 at）"""
    if not FUND_CACHE.exists():
        return {}
    cache = json.loads(FUND_CACHE.read_text(encoding="utf-8"))
    if cache.get("v") == CACHE_VERSION:
        return cache["data"]
    if cache.get("v") == 4:  # 旧形式（ファイル全体で1つの日付）
        return {c: {**r, "at": cache["date"]} for c, r in cache["data"].items()}
    return {}


def _age(rec):
    return (dt.date.today() - dt.date.fromisoformat(rec.get("at") or "2000-01-01")).days


def fresh_cached_codes():
    """FUND_MAX_AGE 日以内に取得済みの銘柄（銘柄診断で、ダッシュボードと同じ母集団で順位を付けるため）"""
    return {c for c, r in _fund_cache().items() if _age(r) <= FUND_MAX_AGE}


def fetch_fundamentals(codes, daily=None, per_run=FUND_PER_RUN):
    """財務指標と年次業績。fundamentals_cache.json を使い、古い・足りない銘柄だけ追加で取る

    daily の銘柄（省略時は全部）は毎日取り直す。それ以外は FUND_MAX_AGE 日まで保存済みを使う
    """
    today = str(dt.date.today())
    daily = set(codes) if daily is None else set(daily)
    have = _fund_cache()
    # 財務指標が空（Yahooの一時的なアクセス拒否など）の銘柄は保存済みでも取り直す
    # Yahooにない銘柄（missing）はその日のうちは取り直さない
    failed = lambda rec: not rec or (not rec.get("missing")
                                     and all(rec.get(k) is None for k in ("per", "pbr", "roe", "q_rev")))

    def stale(c):
        rec = have.get(c)
        if not rec:
            return True
        if "eq_ratio" not in rec and not rec.get("missing"):
            return True  # 自己資本比率・簡易OPを追加する前に取った分
        age = _age(rec)
        return age > (0 if c in daily else FUND_MAX_AGE) or (failed(rec) and (c in daily or age > 0))

    # 毎日取る銘柄を先に（Yahooに拒否されて途中で打ち切っても、テーマ銘柄の分は取れているように）。
    # それ以外は未取得→古い順に FUND_PER_RUN 件まで
    stale_codes = [c for c in codes if stale(c)]
    first = sorted(c for c in stale_codes if c in daily)
    rest = sorted((c for c in stale_codes if c not in daily), key=lambda c: (have.get(c) or {}).get("at") or "")
    need = first + rest[:per_run]
    if per_run and len(rest) > per_run:
        print(f"  財務指標の未取得・古い銘柄 {len(rest)}件のうち {per_run}件を取得（残りは次回以降）", file=sys.stderr)
    if not need:
        return {c: have[c] for c in codes if c in have}
    done, fails = [0], [0]  # fails: 連続で取れなかった件数（Yahooに拒否され始めたら打ち切る）
    step = 10 if len(need) < 300 else 100

    def one(c):
        import time
        if fails[0] >= 40:
            return c, None
        time.sleep(0.5)  # 間隔をあけてYahooのアクセス制限を避ける
        info, inc, tk = {}, None, None
        missing = False
        for attempt in range(3):  # 401（Invalid Crumb など）は少し待つと通ることが多い
            try:
                tk = yf.Ticker(f"{c}.T")
                info = tk.info or {}
                if f"{c}.T" in _not_found.symbols:
                    missing = True  # 404 は待っても通らないので諦める
                    break
                inc = tk.income_stmt
            except Exception:
                info = {}
            if any(info.get(k) is not None for k in FUND_KEYS):
                break
            time.sleep(2 + 3 * attempt)
        ok = missing or any(info.get(k) is not None for k in FUND_KEYS)
        fails[0] = 0 if ok else fails[0] + 1
        done[0] += 1
        if done[0] % step == 0 or done[0] == len(need):
            print(f"  … {done[0]}/{len(need)}", file=sys.stderr)
        if tk is None:
            return c, {"at": today}
        rec = {v: info.get(k) for k, v in FUND_KEYS.items()}
        rec["at"] = today
        if missing:
            rec.update(earn_date=None, rev_cagr=None, op_cagr=None, op_streak=None, fy=None,
                       eq_ratio=None, op_eq=None, missing=True)
            return c, rec
        rec["earn_date"] = None
        if c in daily:  # 決算予定日はテーマ銘柄の「決算が近い」にだけ使うので、それ以外は取らない（時間短縮）
            try:
                ed = (tk.calendar or {}).get("Earnings Date") or []
                rec["earn_date"] = str(ed[0]) if ed else None
            except Exception:
                pass
        rec["rev_cagr"] = rec["op_cagr"] = rec["op_streak"] = rec["fy"] = None
        if inc is not None and not inc.empty:
            inc = inc[sorted(inc.columns, reverse=True)]
            rec["fy"] = str(inc.columns[0].date())
            if "Total Revenue" in inc.index:
                rec["rev_cagr"], _ = growth_from_statement(inc.loc["Total Revenue"].tolist())
            # 銀行・保険は営業利益の項目がないので純利益で代用
            profit = next((r for r in ["Operating Income", "Net Income"] if r in inc.index), None)
            if profit:
                rec["op_cagr"], rec["op_streak"] = growth_from_statement(inc.loc[profit].tolist())
        # 自己資本比率 = 自己資本 ÷ 総資産、簡易OP = 営業利益 ÷ 自己資本（いずれも直近の年次）
        rec["eq_ratio"] = rec["op_eq"] = None
        try:
            bs = tk.balance_sheet
        except Exception:
            bs = None
        if bs is not None and not bs.empty:
            bs = bs[sorted(bs.columns, reverse=True)]
            latest = lambda df, row: next((float(v) for v in df.loc[row] if pd.notna(v)), None) if row in df.index else None
            equity, assets = latest(bs, "Stockholders Equity"), latest(bs, "Total Assets")
            if equity and assets and equity > 0:
                rec["eq_ratio"] = equity / assets
                op = latest(inc, "Operating Income") if inc is not None and not inc.empty else None
                if op is not None:
                    rec["op_eq"] = op / equity
        return c, rec

    def save():
        # 1か月以上前の銘柄（上場廃止など）は捨てる
        keep = {c: r for c, r in have.items() if _age(r) <= 30}
        FUND_CACHE.write_text(json.dumps({"v": CACHE_VERSION, "data": keep}, ensure_ascii=False), encoding="utf-8")

    print(f"財務指標・業績取得中… {len(need)}銘柄" + ("（初回や1週間ぶりは時間がかかります）" if len(need) > 300 else ""),
          file=sys.stderr)
    with ThreadPoolExecutor(2) as ex:
        for i, (c, rec) in enumerate(ex.map(one, need), 1):
            if rec is not None:
                have[c] = rec
            if i % 200 == 0:
                save()  # 途中で止めても、取れた分は次回に使えるように
    if fails[0] >= 40:
        print("  Yahooが応答しなくなったため財務指標の取得を中断しました（残りは次回の実行で取得）", file=sys.stderr)
    still = [c for c in need if c in have and failed(have[c]) and not have[c].get("missing")]
    gone = [c for c in need if (have.get(c) or {}).get("missing")]
    if gone:
        print(f"  Yahooに銘柄データがないため財務指標を省略: {len(gone)}銘柄"
              + (f"（{', '.join(gone)}）" if len(gone) <= 20 else ""), file=sys.stderr)
    if still:
        print(f"  財務指標を取得できなかった銘柄（次回の実行で再取得）: {len(still)}銘柄"
              + (f"（{', '.join(still)}）" if len(still) <= 20 else ""), file=sys.stderr)
    save()
    return {c: have[c] for c in codes if c in have}


def pct_rank(s, higher_is_better=True):
    r = s.rank(pct=True)
    return r if higher_is_better else 1 - r


def stock_scores(fund):
    cols = list(FUND_KEYS.values()) + ["rev_cagr", "op_cagr", "op_streak", "eq_ratio", "op_eq"]
    df = pd.DataFrame.from_dict(fund, orient="index").reindex(columns=cols + ["fy", "earn_date"])
    df[cols] = df[cols].apply(pd.to_numeric, errors="coerce")

    # 成長度：年次で営業利益・売上が伸び続けているか ＋ 直近四半期も伸びているか
    growth = pd.DataFrame({
        "op_cagr": pct_rank(df["op_cagr"]),          # 営業利益 3年CAGR
        "op_streak": df["op_streak"] / 3,            # 連続増益（最大3年）
        "rev_cagr": pct_rank(df["rev_cagr"]),        # 売上 3年CAGR
        "q_earn": pct_rank(df["q_earn"].clip(-1, 3)),  # 直近四半期 利益 前年同期比
        "q_rev": pct_rank(df["q_rev"]),              # 直近四半期 売上 前年同期比
    })
    gw = pd.Series({"op_cagr": 0.30, "op_streak": 0.20, "rev_cagr": 0.20, "q_earn": 0.15, "q_rev": 0.15})
    avail = growth.notna()  # 欠けている指標は残りの重みで按分
    df["growth"] = ((growth.fillna(0) * gw).sum(axis=1) / (avail * gw).sum(axis=1) * 100
                    ).where(avail[["op_cagr", "rev_cagr"]].any(axis=1))

    # 割安度
    df["suspect"] = (df["fper"] > 0) & (df["per"] > 0) & (df["fper"] < df["per"] / 2.5)
    df["per_used"] = df["fper"].where((df["fper"] > 0) & ~df["suspect"], df["per"]).where(lambda s: s > 0)
    value = pd.DataFrame({
        "per": pct_rank(df["per_used"], False),  # 赤字（PERなし）は評価しない
        "pbr": pct_rank(df["pbr"].where(df["pbr"] > 0), False),
        "roe": pct_rank(df["roe"]),
        "div": pct_rank(df["div"]),
    })
    df["value"] = (value.mean(axis=1) * 100).where(value.notna().sum(axis=1) >= 2)

    df["total"] = GROWTH_WEIGHT * df["growth"] + (1 - GROWTH_WEIGHT) * df["value"].fillna(0)
    return df


US_SUMMARY = {"^GSPC": "S&P500", "^IXIC": "ナスダック", "^SOX": "SOX指数（半導体）", "^TNX": "米10年金利",
              "JPY=X": "ドル円", "NIY=F": "日経225先物（CME・円建て）"}
US_ETF_NAMES = {"SMH": "半導体", "SKYY": "クラウド", "CIBR": "サイバーセキュリティ", "BOTZ": "ロボット・AI",
                "QTUM": "量子コンピューター", "BDRY": "ばら積み船運賃", "XLE": "エネルギー", "KBE": "銀行",
                "KRE": "地方銀行", "KIE": "保険", "ITA": "航空宇宙・防衛", "UFO": "宇宙", "REMX": "レアアース",
                "XLU": "公益（電力）", "URA": "ウラン・原子力", "ESPO": "ゲーム・eスポーツ", "IGV": "ソフトウェア",
                "JETS": "航空会社", "PAVE": "インフラ・建設", "XLP": "生活必需品"}
US_BENCH = "SPY"


def fetch_us(themes):
    """前日（直近の取引日）の米国市場。ETFの騰落率と、S&P500(SPY)に対する超過"""
    from zoneinfo import ZoneInfo
    etfs = sorted({s for t in themes.values() for s in t.get("us") or []} | {US_BENCH})
    syms = etfs + list(US_SUMMARY)
    df = yf.download(syms, period="1mo", interval="1d", auto_adjust=True, progress=False, threads=True)["Close"]
    out = {"etf": {}, "summary": {}}
    for s in syms:
        c = df[s].dropna() if s in df else pd.Series(dtype=float)
        if len(c) < 2:
            continue
        rec = {"close": c.iloc[-1], "ret": c.iloc[-1] / c.iloc[-2] - 1, "chg": c.iloc[-1] - c.iloc[-2],
               "date": c.index[-1].date()}
        (out["summary"] if s in US_SUMMARY else out["etf"])[s] = rec
    spy = out["etf"].get(US_BENCH, {}).get("ret", 0.0)
    for rec in out["etf"].values():
        rec["excess"] = rec["ret"] - spy
    now_et = dt.datetime.now(ZoneInfo("America/New_York"))
    us_date = out["etf"].get(US_BENCH, {}).get("date")
    out["date"] = us_date
    out["live"] = (us_date == now_et.date() and now_et.weekday() < 5
                   and dt.time(9, 30) <= now_et.time() < dt.time(16, 0))
    return out


JP_INDEX = "^N225"


def fetch_jp():
    """日経平均の直近半年の終値と、今日・前日の終値"""
    from zoneinfo import ZoneInfo
    c = yf.download(JP_INDEX, period="6mo", interval="1d", auto_adjust=True, progress=False)["Close"]
    c = (c[JP_INDEX] if isinstance(c, pd.DataFrame) else c).dropna()
    if len(c) < 2:
        return None
    now = dt.datetime.now(ZoneInfo("Asia/Tokyo"))
    last = c.index[-1].date()
    return {"close": c, "last": c.iloc[-1], "prev": c.iloc[-2], "date": last, "prev_date": c.index[-2].date(),
            "live": last == now.date() and now.weekday() < 5 and dt.time(9, 0) <= now.time() < dt.time(15, 30)}


def theme_us(t, us):
    recs = [(s, us["etf"][s]) for s in t.get("us") or [] if s in us["etf"]]
    if not recs:
        return None
    return {"ret": float(np.mean([r["ret"] for _, r in recs])),
            "excess": float(np.mean([r["excess"] for _, r in recs])),
            "label": " / ".join(f"{s} {r['ret']:+.1%}" for s, r in recs)}


def margin_cells(m):
    """信用買残・信用倍率・需給判定の3セル（HTML）。見出しは MARGIN_HEAD"""
    if not m:
        return '<td class="num" data-v="">-</td><td class="num" data-v="">-</td><td data-v="">-</td>'
    buy = f'{m["buy"]:,}株'
    if m["buy_wk"] is not None:
        buy += f'<br><small class="{"up" if m["buy_wk"] > 0 else "down"}">週{m["buy_wk"]:+.0%}</small>'
    if m["days"] is not None:
        buy += f'<br><small>出来高{m["days"]:.1f}日分</small>'
    if m.get("cost"):
        g = m["cost_gap"]
        buy += (f'<br><small title="{html.escape(m["cost_method"])}">買い平均 {m["cost"]:,.0f}円</small>'
                f'<br><small class="{"up" if g > 0 else "down"}">株価は{g:+.0%}（{"含み益" if g > 0 else "含み損"}）</small>')
    r = m["ratio"]
    rv, rtxt = (9999, "売残なし") if r == float("inf") else (r, f"{r:.1f}倍")
    rtxt += f'<br><small>売残 {m["sell"]:,}株</small>'
    if m.get("sell_cost"):
        rtxt += f'<br><small>売り平均 {m["sell_cost"]:,.0f}円</small>'
    if m["sell_wk"] is not None:
        rtxt += f'<br><small>売残 週{m["sell_wk"]:+.0%}</small>'
    kind = {"上がりやすい": "news", "下がりやすい": "bad"}.get(m["direction"], "calm")
    notes = "".join(f"<br><small>{html.escape(n)}</small>" for n in m["notes"])
    return (f'<td class="num" data-v="{m["buy"]}">{buy}</td>'
            f'<td class="num" data-v="{rv}">{rtxt}</td>'
            f'<td class="wrapcell" data-v="{m["tilt"]}"><span class="badge {kind}">{m["direction"]}</span>{notes}</td>')


MARGIN_HEAD = ('<th class="num" title="信用買残（株数）。週＝1週間前からの増減、出来高○日分＝買残÷20日平均出来高、買い平均＝東証公表の買残金額÷買残株数（信用買いの平均約定単価）">信用買残<br>買い平均</th>'
               '<th class="num" title="信用買残÷信用売残">信用倍率</th><th>需給</th>')


def price_stats(close, volume, code):
    """平均出来高・1週間/20日の騰落・移動平均線との位置"""
    s = close[f"{code}.T"].dropna() if f"{code}.T" in close else pd.Series(dtype=float)
    if len(s) < 30:
        return {}
    v = volume[f"{code}.T"].dropna() if f"{code}.T" in volume else pd.Series(dtype=float)
    ma25, ma75 = s.rolling(25).mean(), s.rolling(75).mean()
    d = s.diff()
    up, dn = d.clip(lower=0).rolling(14).mean().iloc[-1], (-d.clip(upper=0)).rolling(14).mean().iloc[-1]
    return {"price": float(s.iloc[-1]),
            "avg_vol": float(v.tail(20).mean()) if len(v) else None,
            "ret_week": s.iloc[-1] / s.iloc[-6] - 1, "ret20": s.iloc[-1] / s.iloc[-21] - 1,
            "ret60": s.iloc[-1] / s.iloc[-61] - 1 if len(s) > 61 else None,
            "above25": bool(s.iloc[-1] > ma25.iloc[-1]),
            "above75": bool(len(s) >= 75 and s.iloc[-1] > ma75.iloc[-1]),
            "gap25": s.iloc[-1] / ma25.iloc[-1] - 1,
            "ma25_up": bool(ma25.iloc[-1] > ma25.iloc[-11]),
            "ma75_up": bool(len(s) >= 95 and ma75.iloc[-1] > ma75.iloc[-21]),
            "ma25_over_75": bool(len(s) >= 75 and ma25.iloc[-1] > ma75.iloc[-1]),
            "from_high": s.iloc[-1] / s.tail(250).max() - 1,
            "rsi": 100.0 if not dn else float(100 - 100 / (1 + up / dn))}


def with_market(rec, code, close, volume, mg):
    """銘柄の dict に値動きと信用需給を足す"""
    st = price_stats(close, volume, code)
    rec.update(st)
    m = rec["margin"] = margin.judge(code, mg, st.get("avg_vol"), st.get("ret_week")) if mg else None
    sym = f"{code}.T"
    if m and sym in close:
        est = margin.avg_cost(code, mg, close[sym], volume[sym])
        if est:
            price = float(close[sym].dropna().iloc[-1])
            m.update({"cost": est["cost"], "cost_gap": price / est["cost"] - 1, "cost_method": est["method"]})
            # 信用買いの多くが含み損なら、買値に戻ったところで「やれやれ売り」が出やすい
            if m["cost_gap"] <= -0.10:
                m["notes"].append(f"信用買いの多くが含み損（平均 {est['cost']:,.0f}円）：戻り売りが出やすい")
                if m["days"] is not None and m["days"] >= 1:
                    m["tilt"] -= 1
            elif m["cost_gap"] >= 0.15:
                m["notes"].append(f"信用買いの多くが含み益（平均 {est['cost']:,.0f}円）")
            # 空売りの平均より株価が上なら、空売り勢は含み損 → 買い戻し（踏み上げ）が出やすい
            if est.get("sell_cost"):
                m["sell_cost"] = est["sell_cost"]
                m["sell_gap"] = price / est["sell_cost"] - 1
                sell_days = m["sell"] / st["avg_vol"] if st.get("avg_vol") else None
                if m["sell_gap"] >= 0.05 and sell_days is not None and sell_days >= 0.3:
                    m["notes"].append(f"空売りの多くが含み損（平均 {est['sell_cost']:,.0f}円）：買い戻しが出やすい")
                    m["tilt"] += 1
            m["direction"] = "上がりやすい" if m["tilt"] > 0 else "下がりやすい" if m["tilt"] < 0 else "中立"
    mark_speculative(rec)
    return rec


def mark_speculative(rec):
    """投機的過熱：信用買残が発行済株式の5%以上、または20日で2倍以上"""
    m = rec.get("margin") or {}
    reasons = []
    if (m.get("buy_listed") or 0) >= 5:
        reasons.append(f"信用買残が発行済株式の{m['buy_listed']:.1f}%")
    if (rec.get("ret20") or 0) >= 1.0:
        reasons.append(f"20日で{1 + rec['ret20']:.1f}倍")
    rec["speculative"] = "・".join(reasons)
    return rec


def theme_label(x):
    """テーマ欄の中身（HTML）。テーマに入っていない銘柄は東証の業種を薄く出す"""
    if x.get("themes"):
        return html.escape("・".join(x["themes"]))
    return f'<small class="sector">{html.escape(x.get("sector") or "")}</small>'


def spec_badge(x):
    """銘柄名の横に付ける「投機的過熱」の印（HTML）"""
    why = x.get("speculative")
    return f' <span class="badge bad" title="{html.escape(why)}">投機的過熱</span>' if why else ""


def all_rounders(stocks, picks, n=20):
    """業績（成長度70以上・増益）× 割高すぎない × 上昇トレンド × 需給 がそろう銘柄"""
    pool = {x["code"]: dict(x) for x in stocks}
    for c in picks:
        pool.setdefault(c["code"], dict(c)).update({"surprise": c["tags"], "sp_points": c["points"]})
    out = []
    for x in pool.values():
        g, v = x.get("growth"), x.get("value")
        if g is None or v is None or pd.isna(g) or pd.isna(v) or not x.get("above75") or x.get("speculative"):
            continue
        streak, q = x.get("op_streak"), x.get("q_earn")
        if not (g >= 70 and v >= 35 and ((streak is not None and streak >= 2) or (q is not None and q > 0))):
            continue
        tilt = x["margin"]["tilt"] if x.get("margin") else 0
        x["screen"] = (x.get("total") or 0) + 4 * x.get("sp_points", 0) + 3 * bool(x.get("above25")) + 5 * tilt
        out.append(x)
    return sorted(out, key=lambda x: -x["screen"])[:n]


def lightness(x):
    """信用需給の軽さ（点数が高いほど売り圧力が小さく、買い戻しの燃料がある）"""
    m = x.get("margin")
    if not m:
        return None
    sell_days = m["sell"] / x["avg_vol"] if x.get("avg_vol") else 0
    squeeze = (m.get("sell_gap") or -1) > 0.05 and sell_days >= 0.3
    improving = (m["buy_wk"] or 0) < 0 and (x.get("ret_week") or 0) > 0
    return (-10 * (m["days"] or 0) + float(np.clip((m.get("cost_gap") or 0) * 50, -15, 15))
            + 10 * squeeze + 5 * (m["ratio"] < 1) + 5 * improving)


SCREEN_MAX = 40


def screens(stocks):
    """タブ用の銘柄分類（条件は各タブの説明文と同じ）"""
    ok = lambda v: v is not None and not (isinstance(v, float) and math.isnan(v))
    pool = [x for x in stocks if ok(x.get("growth")) and ok(x.get("value")) and ok(x.get("rsi"))]
    for x in pool:
        x["light"] = lightness(x)
    not_bad = lambda x: not x.get("margin") or x["margin"]["direction"] != "下がりやすい"
    # プライム・スタンダード全体が対象なので、各リストは評価の高い順に SCREEN_MAX 件まで
    by_total = lambda xs: sorted(xs, key=lambda x: -(x.get("total") or 0))[:SCREEN_MAX]

    # 投機的過熱（信用買残が発行済株式の5%以上・20日で2倍以上）の銘柄は、「軽い」「押し目」「過熱していない」から外す
    light = sorted((x for x in pool if x["light"] is not None and x["growth"] >= 60 and x.get("above75")
                    and not x.get("speculative")), key=lambda x: -x["light"])[:30]

    base = [x for x in pool if not x.get("speculative") and x["growth"] >= 60 and x["value"] >= 35 and x["from_high"] <= -0.08
            and x["rsi"] < 60 and x["ret20"] < 0.10 and not_bad(x)
            and (x.get("margin") is None or (x["margin"]["days"] or 0) < 2)]
    dip_a = by_total(x for x in base if x.get("above75"))
    dip_b = by_total(x for x in base if not x.get("above75") and x["from_high"] > -0.25)
    dip_c = by_total(x for x in base if not x.get("above75") and x["from_high"] <= -0.25)

    trend = [x for x in pool if x.get("above25") and x.get("above75") and x.get("ma25_over_75")
             and x.get("ma25_up") and x.get("ma75_up") and x["ret20"] > 0 and x["from_high"] >= -0.10 and not_bad(x)]
    hot = lambda x: x["gap25"] > 0.15 or x["rsi"] > 75 or bool(x.get("speculative"))
    up_a = by_total(x for x in trend if not hot(x))
    up_b = by_total(x for x in trend if hot(x))
    return {"light": light, "dip_a": dip_a, "dip_b": dip_b, "dip_c": dip_c, "up_a": up_a, "up_b": up_b}


def screen_table(rows, extra_light=False):
    """タブ内の銘柄表（HTML）"""
    if not rows:
        return '<p class="note">該当なし</p>'

    def n(v, fmt, cls=""):
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return '<td class="num" data-v="">-</td>'
        return f'<td class="num {cls}" data-v="{v}">{fmt.format(v)}</td>'

    def c(v):
        return "" if v is None else "up" if v > 0 else "down" if v < 0 else ""

    head = ('<th>コード</th><th>銘柄</th><th class="num">株価</th>'
            + ('<th class="num" data-dir="desc">軽さ</th>' if extra_light else "")
            + f'<th class="num"{"" if extra_light else " data-dir=desc"}>評価</th><th class="num">成長度</th><th class="num">割安度</th>'
            '<th class="num">PER</th><th class="num">20日</th><th class="num">60日</th><th class="num">高値比</th>'
            '<th class="num">25日線<br>乖離</th><th class="num">RSI</th>' + MARGIN_HEAD + '<th>テーマ／業種</th>')
    body = "".join(
        f'<tr><td>{x["code"]}</td><td class="name">{html.escape(x["name"])}{spec_badge(x)}</td>{n(x.get("price"), "{:,.0f}")}'
        + (n(x.get("light"), "{:.1f}", "score") if extra_light else "")
        + n(x.get("total"), "{:.0f}", "score") + n(x.get("growth"), "{:.0f}") + n(x.get("value"), "{:.0f}")
        + n(x.get("per_used"), "{:.1f}") + n(x.get("ret20"), "{:+.0%}", c(x.get("ret20")))
        + n(x.get("ret60"), "{:+.0%}", c(x.get("ret60"))) + n(x.get("from_high"), "{:+.0%}")
        + n(x.get("gap25"), "{:+.0%}", c(x.get("gap25"))) + n(x.get("rsi"), "{:.0f}")
        + margin_cells(x.get("margin"))
        + f'<td class="themes">{theme_label(x)}</td></tr>'
        for x in rows)
    return f'<div class="wrap"><table class="sortable"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def screens_html(sc, date, margin_date):
    """「需給が軽い」「押し目・出遅れ」「上昇トレンド」の3タブ分のHTML"""
    common = (f'<p class="sub">{date} 終値ベース・信用残は東証公表（{margin_date or "-"} 時点）。対象は東証プライム・スタンダードの全銘柄＋テーマ銘柄。'
              f'各リスト最大{SCREEN_MAX}銘柄。'
              '評価＝成長度60%＋割安度40%。見出しクリックで並べ替え。</p>')
    light = f"""
<h2>需給が軽く、業績も伸びていて、上昇トレンドにある銘柄</h2>{common}
<p class="sub">条件：成長度60以上・株価が75日線より上。並び＝軽さ（信用買残が出来高の何日分か が少ない／信用買いが含み益／
空売りが含み損で売残もある／信用倍率1倍未満／買残が減りながら上昇 を点数化）。</p>
{screen_table(sc["light"], extra_light=True)}"""
    dips = f"""
<h2>押し目・出遅れ候補（業績は伸びているが、株価はまだ上がりきっていない）</h2>{common}
<p class="sub">共通条件：成長度60以上・割安度35以上・1年の高値から8%以上下・RSI 60未満・20日上昇10%未満・
需給が「下がりやすい」でない・信用買残が出来高の2日分未満。</p>
<h2>A. 上昇トレンド中の一時的な下げ（75日線より上）</h2>
<p class="sub">トレンドは上向きのまま調整中。リスクは比較的低め。</p>{screen_table(sc["dip_a"])}
<h2>B. 短期の調整中（75日線より下・高値から-8〜-25%）</h2>
<p class="sub">業績は良く需給も軽いが、短期的に売られている。材料（決算・金利など）で見直されるか。</p>{screen_table(sc["dip_b"])}
<h2>C. 大きく下げた出遅れ（75日線より下・高値から-25%以上）</h2>
<p class="sub">値上がり余地は最も大きいが、下落トレンドの途中。信用買いの含み損による戻り売りにも注意。75日線を上抜けるなど、トレンド転換の確認が安全。</p>{screen_table(sc["dip_c"])}"""
    ups = f"""
<h2>上昇トレンドの途中にある銘柄</h2>{common}
<p class="sub">共通条件：株価が25日線・75日線の両方より上・25日線が75日線より上・両方の線が上向き・20日でプラス・
1年の高値から10%以内・需給が「下がりやすい」でない。過熱＝25日線より15%以上上、またはRSI 75超。</p>
<h2>A. 上昇中で、まだ過熱していない銘柄（買われすぎていない状態）</h2>{screen_table(sc["up_a"])}
<h2>B. 上昇中だが、過熱している銘柄（強いが、今から入ると高値づかみのリスク）</h2>
<p class="sub">勢いは最も強いが、短期的には25日線あたりまでの押し目を待つのが一般的。</p>{screen_table(sc["up_b"])}"""
    return {"light": light, "dips": dips, "ups": ups}


# 割安×高収益：PBR（割安度＝簿価時価比率の逆数）と営業収益力（OP）に株価の説明力がある、という研究を参考にした条件
QV_RULES = [  # (キー, 表示名, 条件)
    ("pbr", "PBR 1倍以下", lambda v: 0 < v <= 1.0),
    ("roe", "ROE 10%以上", lambda v: v >= 0.10),
    ("eq_ratio", "自己資本比率 40%以上", lambda v: v >= 0.40),
    ("op_eq", "簡易OP 15%以上", lambda v: v >= 0.15),
]


def quality_value(stocks):
    """割安×高収益の銘柄（全条件を満たす／あと1つだけ満たさない）"""
    ok = lambda v: v is not None and not (isinstance(v, float) and math.isnan(v))
    hit, near, have = [], [], 0
    for x in stocks:
        if not all(ok(x.get(k)) for k, _, _ in QV_RULES):
            continue
        have += 1
        miss = [name for k, name, rule in QV_RULES if not rule(x[k])]
        if not miss:
            hit.append(x)
        elif len(miss) == 1:
            near.append({**x, "qv_miss": miss[0]})
    key = lambda x: -x["op_eq"]
    return {"hit": sorted(hit, key=key), "near": sorted(near, key=key)[:60], "have": have}


def quality_html(qv, n_stocks, date):
    def n(v, fmt, cls=""):
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return '<td class="num" data-v="">-</td>'
        return f'<td class="num {cls}" data-v="{v}">{fmt.format(v)}</td>'

    def table(rows, near=False):
        if not rows:
            return '<p class="note">該当なし</p>'
        head = ('<th>コード</th><th>銘柄</th><th>市場</th><th class="num">株価</th><th class="num">PBR</th><th class="num">ROE</th>'
                '<th class="num">自己資本<br>比率</th><th class="num" data-dir="desc">簡易OP</th><th class="num">PER</th><th class="num">配当</th>'
                '<th class="num">成長度</th><th class="num">20日</th><th class="num">高値比</th>'
                + ('<th>満たさない条件</th>' if near else '') + MARGIN_HEAD + '<th>テーマ／業種</th>')
        body = "".join(
            f'<tr><td>{x["code"]}</td><td class="name">{html.escape(x["name"])}{spec_badge(x)}</td><td>{x.get("market") or "-"}</td>'
            + n(x.get("price"), "{:,.0f}") + n(x["pbr"], "{:.2f}") + n(x["roe"], "{:.1%}") + n(x["eq_ratio"], "{:.0%}")
            + n(x["op_eq"], "{:.1%}", "score") + n(x.get("per_used"), "{:.1f}") + n(x.get("div"), "{:.2f}%")
            + n(x.get("growth"), "{:.0f}") + n(x.get("ret20"), "{:+.0%}") + n(x.get("from_high"), "{:+.0%}")
            + (f'<td class="warn">{x["qv_miss"]}</td>' if near else '')
            + margin_cells(x.get("margin")) + f'<td class="themes">{theme_label(x)}</td></tr>'
            for x in rows)
        return f'<div class="wrap"><table class="sortable"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'

    cover = ("" if qv["have"] >= n_stocks * 0.9 else
             f'<b>財務データがそろっているのは{n_stocks}銘柄中{qv["have"]}銘柄です（残りは毎回のbat実行で少しずつ取得中）。</b>')
    return f"""
<h2>割安×高収益の銘柄（{len(qv["hit"])}銘柄）</h2>
<p class="sub">{date} 終値ベース。条件：<b>PBR 1倍以下・ROE 10%以上・自己資本比率 40%以上・簡易OP 15%以上</b>。
簡易OP（営業収益力）＝ 営業利益 ÷ 自己資本（直近の年次決算）。割安度（簿価時価比率）と営業収益力には株価の説明力があり、
規模・投資・モメンタムには一貫した説明力がない、という研究を参考にした条件です。並びは簡易OPの高い順。
銀行・保険は営業利益がないので対象外。PBR・ROEはYahoo Finance、自己資本比率・簡易OPは年次決算から計算。{cover}</p>
{table(qv["hit"])}
<h2>あと1つだけ条件を満たさない銘柄（簡易OP順に最大60）</h2>
<p class="sub">株価が少し上がってPBRが1倍を超えた、自己資本比率が40%にわずかに届かない、など。</p>
{table(qv["near"], near=True)}"""


SURPRISE_DAYS = 3  # 決算サプライズは直近何営業日分の大引け後の開示から拾うか
SURPRISE_PER_DAY = 30  # 1日あたり何銘柄まで表示するか


def earnings_surprises(since_date, universe_codes, details, mg=None, close=None, volume=None, universe=None):
    """since_date の15:00以降（＝直近 SURPRISE_DAYS 営業日分）の適時開示から、決算サプライズ（上方修正・増配・大幅増益）の銘柄"""
    import disclosures
    since = dt.datetime.combine(since_date, dt.time(15, 0))
    try:
        comps = disclosures.collect(since)
    except Exception as e:
        print(f"  適時開示の取得失敗: {e}", file=sys.stderr)
        return {"since": since, "picks": [], "warn": []}
    picks, per_day = [], {}
    for c in comps:  # comps は点数の高い順。日ごとに上位 SURPRISE_PER_DAY 件まで
        if c["points"] >= 2 and "下方修正" not in c["tags"] and not c["tob"]:
            n = per_day[c["time"].date()] = per_day.get(c["time"].date(), 0) + 1
            if n <= SURPRISE_PER_DAY:
                picks.append(c)
    warn = [c for c in comps if c["points"] <= -2][:25]
    if picks:
        # 業績の伸び（成長度）も見る。順位はテーマ銘柄と合わせた中での相対値
        # 決算を出したばかりの銘柄は財務指標を当日分に取り直す
        pick_codes = {c["code"] for c in picks}
        # （テーマ外の古い銘柄の取り直しは build で済んでいるので、ここでは決算銘柄だけ取る）
        sc = stock_scores(fetch_fundamentals(set(universe_codes) | pick_codes, daily=pick_codes, per_run=0))
        theme_of = {}
        for name, members in details.items():
            for x in members:
                theme_of.setdefault(x["code"], []).append(name)
        for c in picks:
            row = sc.loc[c["code"]] if c["code"] in sc.index else None
            for k in ("growth", "value", "total", "per_used", "pbr", "op_streak", "q_earn"):
                c[k] = None if row is None or pd.isna(row[k]) else float(row[k])
            c["themes"] = theme_of.get(c["code"], [])
            c["sector"] = (universe or {}).get(c["code"], {}).get("sector", "")
        # 値動きと信用需給。取得済みの株価にない銘柄（グロース市場など）だけ取り直す
        pc = close if close is not None else pd.DataFrame()
        pv = volume if volume is not None else pd.DataFrame()
        need = [f"{c['code']}.T" for c in picks if f"{c['code']}.T" not in pc or pc[f"{c['code']}.T"].dropna().empty]
        if need:
            nc, nv = _download(need)
            if not nc.empty:
                nc = fill_last_close(nc.copy())
                pc = pd.concat([pc.drop(columns=list(nc.columns), errors="ignore"), nc], axis=1)
                pv = pd.concat([pv.drop(columns=list(nv.columns), errors="ignore"), nv], axis=1)
        if not pc.empty:
            for c in picks:
                with_market(c, c["code"], pc, pv, mg)
                # 開示後の値動き：開示直前の終値（大引け後の開示ならその日、場中ならその前日の終値）からの騰落
                s = pc[f"{c['code']}.T"].dropna() if f"{c['code']}.T" in pc else pd.Series(dtype=float)
                t = c["time"]
                before = s[[d.date() <= t.date() if t.hour >= 15 else d.date() < t.date() for d in s.index]]
                c["reaction"] = float(s.iloc[-1] / before.iloc[-1] - 1) if len(before) and len(s) else None
        # 開示日の新しい順、同じ日の中は点数の高い順
        picks.sort(key=lambda c: (-c["time"].date().toordinal(), -c["points"],
                                  -(c["growth"] if c["growth"] is not None else -1)))
    return {"since": since, "picks": picks, "warn": warn}


def build(themes, use_news):
    theme_codes = {c for t in themes.values() for c in t["tickers"]}
    universe = load_universe()
    codes = theme_codes | set(universe)
    print(f"株価取得中… {len(codes)}銘柄（テーマ {len(theme_codes)}・プライム/スタンダード {len(universe)}）",
          file=sys.stderr)
    close, volume = fetch_prices(codes)
    bench_ret = close[BENCH].pct_change(fill_method=None)

    no_data = {c for c in codes if close.get(f"{c}.T") is None or close[f"{c}.T"].dropna().empty}
    missing = sorted(no_data & theme_codes)  # 画面に出すのはテーマ銘柄の分だけ
    if no_data - theme_codes:
        print(f"  株価を取得できなかった銘柄（テーマ外）: {len(no_data - theme_codes)}銘柄", file=sys.stderr)
    ok_codes = codes - no_data
    # テーマ銘柄は毎日、それ以外は1週間まで保存済みの財務指標を使う
    fund = stock_scores(fetch_fundamentals(ok_codes, daily=theme_codes & ok_codes)).reindex(sorted(ok_codes))
    fund["suspect"] = fund["suspect"].fillna(False).astype(bool)
    try:
        mg = margin.load()
    except Exception as e:
        print(f"  信用残の取得失敗: {e}", file=sys.stderr)
        mg = None
    last_ret = close.pct_change(fill_method=None).iloc[-1]
    today = dt.date.today()
    window = news_window_days(close.index[-1].date(), today)
    news = {}
    if use_news:
        print(f"ニュース件数取得中…（直近{window * 24}時間）", file=sys.stderr)
        now = dt.datetime.now(dt.timezone.utc)
        with ThreadPoolExecutor(8) as ex:
            futs = {name: ex.submit(fetch_news, t["news"], now, window) for name, t in themes.items()}
        news = {name: f.result() for name, f in futs.items()}
    events = load_events(today)
    print("米国市場（ETF）取得中…", file=sys.stderr)
    try:
        us = fetch_us(themes)
    except Exception as e:
        print(f"  米国市場の取得失敗: {e}", file=sys.stderr)
        us = {"etf": {}, "summary": {}, "date": None, "live": False}
    try:
        jp = fetch_jp()
    except Exception as e:
        print(f"  日経平均の取得失敗: {e}", file=sys.stderr)
        jp = None

    rows, history, details, extras = [], {}, {}, {}
    for name, t in themes.items():
        syms = [f"{c}.T" for c in t["tickers"] if c not in missing]
        if not syms:
            continue
        m, ret = theme_metrics(syms, close, volume, bench_ret)
        last = m.iloc[-1]
        price = last["price_score"]
        n = news.get(name)
        ns = news_score(n["news_ratio"]) if n else 50.0
        total = (1 - NEWS_WEIGHT) * price + NEWS_WEIGHT * ns if n else price
        r_last = ret.iloc[-1].dropna().sort_values(ascending=False)
        members = [with_market({"code": s[:-2], "name": t["tickers"][s[:-2]], "ret": last_ret.get(s),
                                **fund.loc[s[:-2]].to_dict()}, s[:-2], close, volume, mg) for s in syms]
        members.sort(key=lambda x: -x["total"] if pd.notna(x["total"]) else 1)

        # 先回りシグナル：ニュースが増えている・イベントや決算が近い・株価はまだ動いていない
        theme_events = [e for e in events if name in (e.get("themes") or [])]
        earnings = sorted(
            ((dt.date.fromisoformat(x["earn_date"]) - today).days, x["name"])
            for x in members if isinstance(x.get("earn_date"), str))
        earnings = [(d, nm) for d, nm in earnings if 0 <= d <= 14]
        ev_score = max([100 * (1 - e["days"] / 15) for e in theme_events]
                       + [80 * (1 - d / 15) for d, _ in earnings[:1]] + [0])
        # 米国連動：対応ETFのS&P500に対する超過リターン。±3%で0〜100
        tu = theme_us(t, us)
        us_score = float(np.clip(50 + tu["excess"] * 100 * 50 / 3, 0, 100)) if tu else 50.0
        early = 0.35 * ns + 0.25 * ev_score + 0.20 * us_score + 0.20 * (100 - price)
        signals = []
        if tu and abs(tu["excess"]) >= 0.01:
            signals.append(f"米国{'高' if tu['excess'] > 0 else '安'} {tu['label']}")
        if n and n["news_ratio"] >= 1.5:
            signals.append(f"ニュース急増 {n['news_ratio']:.1f}倍")
        if theme_events:
            e = theme_events[0]
            signals.append("開催中: " + e["name"] if e["days"] == 0 else f"{e['days']}日後: {e['name']}")
        if earnings:
            signals.append(f"決算 {earnings[0][0]}日後: " + "・".join(nm for _, nm in earnings[:3])
                           + (f" ほか{len(earnings) - 3}社" if len(earnings) > 3 else ""))
        if price < 55 and (signals):
            signals.append("株価はまだ平常")
        extras[name] = {"headlines": n["headlines"] if n else [], "events": theme_events,
                        "earnings": earnings}
        rows.append({
            "テーマ": name,
            "総合": total,
            "株価スコア": price,
            "前日比": price - m["price_score"].iloc[-2],
            "5日比": price - m["price_score"].iloc[-6],
            "売買代金倍率": last["to_ratio"],
            "売買代金前日比": last["to_dod"],
            "騰落率": last["theme_ret"],
            "対TOPIX": last["excess"],
            "上昇比率": last["breadth"],
            "記事数": n["news_recent"] if n else None,
            "ニュース倍率": n["news_ratio"] if n else None,
            "成長度": pd.Series([x["growth"] for x in members], dtype=float).median(),
            "割安度": pd.Series([x["value"] for x in members], dtype=float).median(),
            "成長株": " / ".join(x["name"] for x in sorted(
                (x for x in members if pd.notna(x["growth"])), key=lambda x: -x["total"])[:2]),
            "牽引": " / ".join(f"{t['tickers'][s[:-2]]} {r:+.1%}" for s, r in r_last.head(2).items()),
            "米国": tu["excess"] if tu else None,
            "米国ETF": tu["label"] if tu else "",
            "先回り": early,
            "シグナル": " ／ ".join(signals),
        })
        history[name] = m["price_score"].tail(30)
        details[name] = members

    df = pd.DataFrame(rows).sort_values("総合", ascending=False).reset_index(drop=True)
    df.index += 1
    # 全銘柄一覧（複数テーマに属する銘柄は1行にまとめる）
    stocks = {}
    for theme, members in details.items():
        for x in members:
            stocks.setdefault(x["code"], {**x, "themes": []})["themes"].append(theme)
    # テーマに入っていないプライム・スタンダード銘柄
    print(f"値動き・信用需給の計算中… {len(ok_codes - set(stocks))}銘柄", file=sys.stderr)
    for c in sorted(ok_codes & set(universe) - set(stocks)):
        stocks[c] = with_market({"code": c, "name": universe[c]["name"], "ret": last_ret.get(f"{c}.T"),
                                 **fund.loc[c].to_dict(), "themes": []}, c, close, volume, mg)
    for c, x in stocks.items():
        x["market"] = universe.get(c, {}).get("market", "")
        x["sector"] = universe.get(c, {}).get("sector", "")
    surprises = earnings_surprises(close.index[-min(SURPRISE_DAYS, len(close))].date(), ok_codes, details, mg,
                                   close, volume, universe)
    rounders = all_rounders(list(stocks.values()), surprises["picks"])
    screen_lists = screens(list(stocks.values()))
    screen_lists["quality"] = quality_value(list(stocks.values()))
    words = []
    if use_news:
        print("監視ワードのニュース件数取得中…", file=sys.stderr)
        words = scan_watchwords(dt.datetime.now(dt.timezone.utc), window)
    return {"df": df, "date": close.index[-1].date(), "history": history, "details": details,
            "stocks": list(stocks.values()), "missing": missing, "extras": extras,
            "events": events, "words": words, "today": today, "window": window, "us": us, "jp": jp, "themes": themes, "surprises": surprises, "rounders": rounders, "screens": screen_lists, "margin_date": mg["date"] if mg else None}


def print_table(df, date):
    out = pd.DataFrame({
        "テーマ": df["テーマ"],
        "総合": df["総合"].map("{:.0f}".format),
        "前日比": df["前日比"].map("{:+.0f}".format),
        "5日比": df["5日比"].map("{:+.0f}".format),
        "売買代金(対20日)": df["売買代金倍率"].map(lambda x: f"{x:.2f}倍({x - 1:+.0%})"),
        "対TOPIX": df["対TOPIX"].map("{:+.1%}".format),
        "上昇比率": df["上昇比率"].map("{:.0%}".format),
        "ニュース倍率": df["ニュース倍率"].map(lambda x: "-" if pd.isna(x) else f"{x:.1f}倍"),
        "成長度": df["成長度"].map(lambda x: "-" if pd.isna(x) else f"{x:.0f}"),
        "割安度": df["割安度"].map(lambda x: "-" if pd.isna(x) else f"{x:.0f}"),
        "評価上位": df["成長株"],
        "牽引": df["牽引"],
    }, index=df.index)
    print(f"\n=== テーマ注目度ランキング {date} ===")
    print(out.to_string())


def print_early(r):
    us = r["us"]
    if us["etf"]:
        print(f"\n=== 前日の米国市場（{us['date']} {'取引中の速報値' if us['live'] else '終値'}） ===")
        print("  " + "  ".join(
            f"{nm} {x['chg']:+.3f}pt" if s == "^TNX" else f"{nm} {x['ret']:+.2%}"
            for s, nm in US_SUMMARY.items() if (x := us["summary"].get(s))))
        etfs = sorted(((s, x) for s, x in us["etf"].items() if s != US_BENCH), key=lambda kv: -kv[1]["ret"])
        print("  上昇: " + "  ".join(f"{s}({US_ETF_NAMES.get(s, '')}) {x['ret']:+.1%}" for s, x in etfs[:5]))
        print("  下落: " + "  ".join(f"{s}({US_ETF_NAMES.get(s, '')}) {x['ret']:+.1%}" for s, x in etfs[-5:][::-1]))
    sp = r["surprises"]
    print(f"\n=== 決算サプライズ注目銘柄（{sp['since']:%m/%d %H:%M} 以降の開示） ===")
    for c in sp["picks"][:10]:
        g = "-" if c["growth"] is None else f"{c['growth']:.0f}"
        print(f" {c['points']:4.1f}点  {c['code']} {c['name']:<12} 成長度{g:>3}  {' / '.join(c['tags'])}")
    if not sp["picks"]:
        print(" 該当なし")
    print(f"\n=== 業績・トレンド・需給がそろう銘柄（信用残 {r['margin_date']}） ===")
    for x in r["rounders"]:
        m = x.get("margin")
        mtxt = "-" if not m else f"買残{m['buy']:,}株・" + \
            (f"信用倍率{m['ratio']:.1f}倍" if m["ratio"] != float("inf") else "売残なし") + \
            (f"・買残{m['days']:.1f}日分" if m["days"] is not None else "") + \
            (f"・買い平均{m['cost']:,.0f}円（株価{m['cost_gap']:+.0%}）" if m.get("cost") else "") + \
            f" → {m['direction']}"
        print(f" {x['screen']:5.1f}  {x['code']} {x['name']:<12} 評価{x.get('total') or 0:3.0f}  {mtxt}"
              + (f"  [{' / '.join(x['surprise'])}]" if x.get("surprise") else ""))
    df = r["df"].sort_values("先回り", ascending=False).head(6)
    print("\n=== いち早く注目すべきテーマ（先回りスコア上位） ===")
    for _, x in df.iterrows():
        print(f" {x['先回り']:3.0f}  {x['テーマ']:<14} {x['シグナル'] or '-'}")
    if r["events"]:
        print("\n=== 今後2週間のイベント ===")
        for e in r["events"]:
            when = "開催中" if e["days"] == 0 else f"{e['days']:2d}日後"
            print(f" {e['start']:%m/%d} {when}  {e['name']}  → {'・'.join(e.get('themes') or [])}")
    hot = [(w, n) for w, n in r["words"] if n["news_ratio"] >= 1.5 and n["news_recent"] >= 3]
    if hot:
        print("\n=== ニュースが急増しているワード（新テーマ候補） ===")
        for w, n in hot[:10]:
            print(f" {w:<12} {n['news_ratio']:.1f}倍（{r['window'] * 24}h {n['news_recent']}件）  {n['headlines'][0][0] if n['headlines'] else ''}")


def is_weekend(day):
    return day.weekday() >= 5


def save_history(df, date, today):
    path = BASE / "history.csv"
    rec = df.assign(日付=str(date))
    if path.exists():
        old = pd.read_csv(path, encoding="utf-8-sig")
        # 週末の実行は株価が同じで、ニュースだけ少なく出るので、平日に保存した行を上書きしない
        if is_weekend(today) and (old["日付"] == str(date)).any():
            return
        rec = pd.concat([old[old["日付"] != str(date)], rec])
    rec.to_csv(path, index=False, encoding="utf-8-sig")


NEWS_HISTORY = BASE / "news_history.csv"


def news_rows(r, measured_at):
    """テーマと監視ワードのニュース倍率を1つの表にまとめる"""
    rows = []
    for _, x in r["df"].iterrows():
        if pd.notna(x["ニュース倍率"]):
            rows.append({"種類": "テーマ", "名前": x["テーマ"], "倍率": x["ニュース倍率"],
                         "件数": x["記事数"], "headlines": r["extras"][x["テーマ"]]["headlines"]})
    for w, n in r["words"]:
        rows.append({"種類": "監視ワード", "名前": w, "倍率": n["news_ratio"],
                     "件数": n["news_recent"], "headlines": n["headlines"]})
    for x in rows:
        x.update({"計測日時": measured_at, "期間h": r["window"] * 24})
    return sorted(rows, key=lambda x: -x["倍率"])


def load_prev_weekday_news(today):
    """今日より前の、平日に計測した直近のニュース倍率 {名前: (倍率, 計測日時)}"""
    if not NEWS_HISTORY.exists():
        return {}
    h = pd.read_csv(NEWS_HISTORY, encoding="utf-8-sig")
    t = pd.to_datetime(h["計測日時"])
    h = h[(t.dt.weekday < 5) & (t.dt.date < today)]
    if h.empty:
        return {}
    last = h[h["計測日時"] == h["計測日時"].max()]
    return {x["名前"]: (x["倍率"], x["計測日時"]) for _, x in last.iterrows()}


def save_news_history(rows):
    rec = pd.DataFrame([{k: v for k, v in x.items() if k != "headlines"} for x in rows])
    if rec.empty:
        return
    if NEWS_HISTORY.exists():
        rec = pd.concat([pd.read_csv(NEWS_HISTORY, encoding="utf-8-sig"), rec])
    rec.to_csv(NEWS_HISTORY, index=False, encoding="utf-8-sig")


def sparkline(s, w=120, h=28):
    v = s.dropna().values
    if len(v) < 2:
        return ""
    pts = " ".join(f"{i * w / (len(v) - 1):.1f},{h - x / 100 * h:.1f}" for i, x in enumerate(v))
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" class="spark">'
            f'<line x1="0" x2="{w}" y1="{h / 2}" y2="{h / 2}" class="mid"/>'
            f'<polyline points="{pts}"/></svg>')


def early_html(r):
    """先回りテーマ・イベント予定・急増ワードの3つのセクション"""
    esc = html.escape

    def badges(sig):
        out = []
        for s in filter(None, sig.split(" ／ ")):
            kind = ("news" if s.startswith(("ニュース", "米国高")) else "calm" if s.startswith(("株価", "米国安"))
                    else "ev")
            out.append(f'<span class="badge {kind}">{esc(s)}</span>')
        return "".join(out) or '<span class="note">-</span>'

    def heads(items):
        if not items:
            return ""
        return '<ul class="heads">' + "".join(
            f'<li><a href="{esc(l)}" target="_blank" rel="noopener">{esc(t)}</a></li>' for t, l in items[:2]) + "</ul>"

    def cls_(v):
        return "up" if v > 0 else "down" if v < 0 else ""

    def us_cell(v):
        if v is None or pd.isna(v):
            return '<td class="num" data-v="">-</td>'
        return f'<td class="num {cls_(v)}" data-v="{v}">{v:+.1%}</td>'

    df = r["df"].sort_values("先回り", ascending=False)
    rows = "".join(f"""<tr>
 <td>{esc(x["テーマ"])}</td>
 <td class="num score" data-v="{x["先回り"]}" style="--s:{x["先回り"]:.0f}">{x["先回り"]:.0f}</td>
 <td class="num" data-v="{x["総合"]}">{x["総合"]:.0f}</td>
 {us_cell(x["米国"])}
 <td class="wrapcell">{badges(x["シグナル"])}{heads(r["extras"][x["テーマ"]]["headlines"])}</td>
</tr>""" for _, x in df.iterrows())

    # 前日の米国市場
    us = r["us"]
    us_label = "取引中の速報値" if us["live"] else "終値"
    summ = "".join(
        f'<tr><td>{esc(nm)}</td><td class="num">{us["summary"][s]["close"]:,.2f}</td>'
        + (f'<td class="num {cls_(us["summary"][s]["chg"])}">{us["summary"][s]["chg"]:+.3f}pt</td>' if s == "^TNX"
           else f'<td class="num {cls_(us["summary"][s]["ret"])}">{us["summary"][s]["ret"]:+.2%}</td>')
        + '</tr>'
        for s, nm in US_SUMMARY.items() if s in us["summary"])
    theme_of = {}
    for name, t in r["themes"].items():
        for s in t.get("us") or []:
            theme_of.setdefault(s, []).append(name)

    def jp_stocks(etf, n=4):
        """ETFに対応するテーマの銘柄を、評価（成長度60%＋割安度40%）の高い順に"""
        seen, out = set(), []
        for x in sorted((m for th in theme_of.get(etf, []) for m in r["details"].get(th, [])),
                        key=lambda m: -m["total"] if pd.notna(m["total"]) else 1):
            if x["code"] not in seen:
                seen.add(x["code"])
                out.append(f'{esc(x["name"])}({x["code"]}){spec_badge(x)}'
                           + (f'<small> 評価{x["total"]:.0f}</small>' if pd.notna(x["total"]) else ""))
        return "<br>".join(out[:n])

    etf_rows = "".join(
        f'<tr><td>{s}</td><td>{esc(US_ETF_NAMES.get(s, ""))}</td>'
        f'<td class="num {cls_(x["ret"])}" data-v="{x["ret"]}">{x["ret"]:+.2%}</td>'
        f'<td class="num {cls_(x["excess"])}" data-v="{x["excess"]}">{x["excess"]:+.2%}</td>'
        f'<td class="themes">{esc("・".join(theme_of.get(s, [])))}</td>'
        f'<td class="jp">{jp_stocks(s)}</td></tr>'
        for s, x in sorted(us["etf"].items(), key=lambda kv: -kv[1]["ret"]) if s != US_BENCH)

    # 決算サプライズ
    sp = r["surprises"]

    def pct(v):
        return '<td class="num" data-v="">-</td>' if v is None else \
            f'<td class="num {cls_(v)}" data-v="{v}">{v:+.0f}%</td>'

    def score(v):
        return '<td class="num" data-v="">-</td>' if v is None else f'<td class="num" data-v="{v}">{v:.0f}</td>'

    def sp_badges(tags):
        return "".join(
            f'<span class="badge {"news" if t.startswith(("上方", "増配", "営業益 +")) else "calm"}">{esc(t)}</span>'
            for t in tags)

    def sp_row(c):
        return (
        f'<tr><td>{c["time"]:%m/%d %H:%M}</td><td>{c["code"]}</td><td class="name">{esc(c["name"])}{spec_badge(c)}</td>'
        f'<td class="num score" data-v="{c["points"]}">{c["points"]:.1f}</td>'
        f'{pct(None if c.get("reaction") is None else c["reaction"] * 100)}'
        f'<td class="wrapcell">{sp_badges(c["tags"])}</td>'
        f'{pct(c["sales_pct"])}{pct(c["op_pct"])}{score(c["growth"])}{score(c["value"])}{score(c["total"])}'
        f'{score(c["per_used"])}{margin_cells(c.get("margin"))}<td class="themes">{theme_label(c)}</td>'
        f'<td class="wrapcell docs">' + "".join(f'<a href="{esc(u)}" target="_blank" rel="noopener" title="{esc(t)}">{esc(t)}</a>'
                                                   for t, u in c["docs"][:2]) + '</td></tr>')

    # 開示日ごとの表（picks は日付の新しい順・同じ日は点数順に並んでいる）
    by_day = {}
    for c in sp["picks"]:
        by_day.setdefault(c["time"].date(), []).append(c)
    sp_head = ('<thead><tr><th>開示</th><th>コード</th><th>銘柄</th><th class="num" data-dir="desc">点数</th><th class="num">開示後</th><th>サプライズ</th>'
               '<th class="num">売上<br>前年比</th><th class="num">営業益<br>前年比</th><th class="num">成長度</th><th class="num">割安度</th><th class="num">評価</th><th class="num">PER</th>'
               f'{MARGIN_HEAD}<th>テーマ／業種</th><th>開示資料</th></tr></thead>')
    sp_tables = "".join(
        f'<h3>{d:%m/%d}（{"月火水木金土日"[d.weekday()]}）の開示 {len(cs)}銘柄</h3>'
        f'<div class="wrap"><table class="sortable sp">{sp_head}<tbody>{"".join(sp_row(c) for c in cs)}</tbody></table></div>'
        for d, cs in by_day.items()) or '<p class="note">該当なし</p>'
    warn_rows = "、".join(f'{esc(c["name"])}({c["code"]}: {"・".join(c["tags"])})' for c in sp["warn"])

    # 業績 × トレンド × 需給 がそろう銘柄
    def yesno(b):
        return f'<td data-v="{int(bool(b))}">{"上" if b else "下"}</td>'

    rd_rows = "".join(
        f'<tr><td>{x["code"]}</td><td class="name">{esc(x["name"])}{spec_badge(x)}</td>'
        f'<td class="num score" data-v="{x["screen"]}">{x["screen"]:.0f}</td>'
        f'{score(x.get("total"))}{score(x.get("growth"))}{score(x.get("value"))}{score(x.get("per_used"))}'
        f'{pct(None if x.get("ret20") is None else x["ret20"] * 100)}{yesno(x.get("above25"))}'
        f'<td class="wrapcell">{sp_badges(x.get("surprise") or [])}</td>'
        f'{margin_cells(x.get("margin"))}<td class="themes">{theme_label(x)}</td></tr>'
        for x in r["rounders"])

    ev_rows = "".join(
        f'<tr><td>{e["start"]:%m/%d}</td><td>{"開催中" if e["days"] == 0 else str(e["days"]) + "日後"}</td>'
        f'<td class="wrapcell">{esc(e["name"])}</td><td class="themes">{esc("・".join(e.get("themes") or []))}</td></tr>'
        for e in r["events"])

    # 決算が近い銘柄（全テーマ横断）
    soon = {}
    for name, ex in r["extras"].items():
        for d, nm in ex["earnings"]:
            soon.setdefault((d, nm), []).append(name)
    earn_rows = "".join(
        f'<tr><td>{(r["today"] + dt.timedelta(days=d)):%m/%d}</td><td>{d}日後</td><td>{esc(nm)}</td>'
        f'<td class="themes">{esc("・".join(ts))}</td></tr>'
        for (d, nm), ts in sorted(soon.items()))

    def prev_cell(name):
        p = r["news_prev"].get(name)
        if not p:
            return '<td class="num" data-v="">-</td>'
        return f'<td class="num" data-v="{p[0]}">{p[0]:.1f}倍<br><small>{p[1][5:16]}</small></td>'

    news_items = r["news_rows"][:25]
    news_tbl = "".join(
        f'<tr><td>{esc(x["名前"])}</td><td class="themes">{x["種類"]}</td>'
        f'<td class="num score" data-v="{x["倍率"]}">{x["倍率"]:.1f}倍</td>'
        f'<td class="num" data-v="{x["件数"]}">{x["件数"]:.0f}</td>{prev_cell(x["名前"])}'
        f'<td class="wrapcell">{heads(x["headlines"])}</td></tr>'
        for x in news_items)
    weekend_note = ("<b>今日は週末のため記事が少なく、倍率は全体に低めです。平日の値は「前回の平日計測」の列を見てください。</b>"
                    if is_weekend(r["today"]) else "")

    return f"""
<h2>前日の米国市場（{us["date"] or "-"} {us_label}）</h2>
<p class="sub">朝のチェック用。米国の業種・分野ETFの騰落率と、S&amp;P500に対する超過分。対応するテーマは themes.yaml の us: で設定。</p>
<div class="wrap" style="display:flex;gap:24px;flex-wrap:wrap">
<table style="width:auto"><thead><tr><th>指標</th><th class="num">値</th><th class="num">前日比</th></tr></thead><tbody>{summ}</tbody></table>
<table class="sortable" style="width:auto;flex:1"><thead><tr><th>ETF</th><th>分野</th><th class="num" data-dir="desc">騰落率</th><th class="num">対S&amp;P500</th><th>日本のテーマ</th><th>日本の関連銘柄（評価順）</th></tr></thead>
<tbody>{etf_rows or '<tr><td colspan="6" class="note">取得できませんでした</td></tr>'}</tbody></table>
</div>

<h2>決算サプライズ注目銘柄（直近{SURPRISE_DAYS}営業日分：{sp["since"]:%m/%d %H:%M} 以降の開示）</h2>
<p class="sub">直近{SURPRISE_DAYS}営業日の大引け後〜今までの適時開示（TDnet）から、上方修正・増配・大幅増益のあった銘柄。
点数 = 上方修正+3・増配+2・営業益+30%以上+2（+10%以上+1）・売上+10%以上+0.5。下方修正・TOB対象は除外。
<b>開示後</b>＝開示直前の終値（大引け後の開示はその日、場中の開示は前日の終値）から最新の終値までの騰落。すでに大きく上がっていれば材料は織り込み済みの可能性。
成長度・割安度・評価はyfinanceの年次業績などから算出（全銘柄内の相対値）。開示PDFから自動で読み取った数値なので、必ず原文で確認してください。
日付の新しい順に、開示日ごとに分けて点数の高い順に表示（1日{SURPRISE_PER_DAY}銘柄まで）。</p>
{sp_tables}
{f'<p class="note"><b>要注意（下方修正・減配など）:</b> {warn_rows}</p>' if warn_rows else ''}

<h2>業績・トレンド・需給がそろう銘柄</h2>
<p class="sub">条件：成長度70以上かつ増益（2年以上連続、または直近四半期が増益）・割安度35以上・株価が75日線より上。
並び順 = 評価 + 決算サプライズ点数×4 + 25日線より上なら+3 + 需給（上がりやすい+5／下がりやすい−5）。
信用残は東証「銘柄別信用取引残高」（{r["margin_date"] or "-"} 申込み現在）。需給は信用倍率だけでなく、買残が平均出来高の何日分か・1週間の増減も見て判定。</p>
<div class="wrap"><table class="sortable"><thead><tr><th>コード</th><th>銘柄</th><th class="num" data-dir="desc">並び<br>順</th><th class="num">評価</th><th class="num">成長度</th><th class="num">割安度</th><th class="num">PER</th><th class="num">20日<br>騰落</th><th>25日線</th><th>決算サプライズ</th>{MARGIN_HEAD}<th>テーマ／業種</th></tr></thead>
<tbody>{rd_rows or '<tr><td colspan="14" class="note">該当なし</td></tr>'}</tbody></table></div>

<h2>いち早く注目すべきテーマ</h2>
<p class="sub">先回りスコア = ニュースの増え方35% + イベント・決算の近さ25% + 前日の米国の対応ETF（S&amp;P500比）20% + 株価がまだ動いていない度合い20%。
注目度（総合）が低いのに先回りスコアが高いテーマが「これから来るかもしれない」候補。</p>
<div class="wrap"><table class="sortable">
<thead><tr><th>テーマ</th><th class="num" data-dir="desc">先回り</th><th class="num">注目度</th><th class="num" title="対応する米国ETFの前日騰落率 − S&amp;P500">米国<br>(対S&amp;P)</th><th>シグナルと最新ニュース</th></tr></thead>
<tbody>{rows}</tbody></table></div>

<h2>今後2週間のイベント</h2>
<p class="sub">events.yaml に登録した予定。新しい予定は events.yaml に追記してください。</p>
<div class="wrap"><table><thead><tr><th>日付</th><th>まで</th><th>イベント</th><th>関連テーマ</th></tr></thead>
<tbody>{ev_rows or '<tr><td colspan="4" class="note">予定なし</td></tr>'}</tbody></table></div>

<h2>2週間以内に決算発表する銘柄</h2>
<p class="sub">yfinance の決算予定日（会社発表前は推定日の場合あり）。</p>
<div class="wrap"><table><thead><tr><th>日付</th><th>まで</th><th>銘柄</th><th>テーマ</th></tr></thead>
<tbody>{earn_rows or '<tr><td colspan="4" class="note">該当なし</td></tr>'}</tbody></table></div>

<h2>ニュースが増えているテーマ・ワード</h2>
<p class="sub">{r["measured_at"]} 計測。テーマ（themes.yaml）と監視ワード（watchwords.yaml。新テーマ候補）をまとめて、ニュース倍率の高い順に上位25件。
倍率 = 直近{r["window"] * 24}時間の1日あたり記事数 ÷ その前7日の1日平均（週明け・連休明けは期間を自動で延長）。毎回の値は news_history.csv に保存。{weekend_note}</p>
<div class="wrap"><table class="sortable"><thead><tr><th>名前</th><th>種類</th><th class="num" data-dir="desc">倍率</th><th class="num">{r["window"] * 24}h件数</th><th class="num">前回の<br>平日計測</th><th>最新ニュース</th></tr></thead>
<tbody>{news_tbl or '<tr><td colspan="6" class="note">ニュース取得なし（--no-news で実行）</td></tr>'}</tbody></table></div>
"""


def index_chart(c, days=60, w=640, h=220):
    """日経平均の折れ線（直近 days 営業日・25日線つき）。前日と今日の点に値を添える（SVG）"""
    ma = c.rolling(25).mean()
    s, ma = c.tail(days), ma.tail(days)
    lo, hi = min(s.min(), ma.min()), max(s.max(), ma.max())
    lo, hi = lo - (hi - lo) * .08, hi + (hi - lo) * .12
    left, right, top, bottom = 4, 78, 14, 24
    x = lambda i: left + i * (w - left - right) / (len(s) - 1)
    y = lambda v: top + (hi - v) / (hi - lo) * (h - top - bottom)
    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(s))
    mal = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(ma) if pd.notna(v))
    area = f"{x(0):.1f},{h - bottom} {line} {x(len(s) - 1):.1f},{h - bottom}"
    grid = "".join(
        f'<line x1="{left}" x2="{w - right + 4}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
        f'<text x="{w - right + 8}" y="{y(v) + 4:.1f}" class="ax">{v:,.0f}</text>'
        for v in np.linspace(lo + (hi - lo) * .1, hi - (hi - lo) * .1, 4))
    n = len(s) - 1
    pts = (f'<circle cx="{x(n - 1):.1f}" cy="{y(s.iloc[-2]):.1f}" r="4" class="prev"/>'
           f'<circle cx="{x(n):.1f}" cy="{y(s.iloc[-1]):.1f}" r="5.5" class="now"/>')
    return (f'<svg viewBox="0 0 {w} {h}" class="ichart" role="img" '
            f'aria-label="日経平均 直近{days}営業日の推移">'
            '<defs><linearGradient id="ig" x1="0" x2="0" y1="0" y2="1">'
            '<stop offset="0" style="stop-color:var(--accent);stop-opacity:.28"/>'
            '<stop offset="1" style="stop-color:var(--accent);stop-opacity:0"/></linearGradient></defs>'
            f'{grid}<polygon points="{area}" fill="url(#ig)"/>'
            f'<polyline points="{mal}" class="ma"/><polyline points="{line}" class="ln"/>{pts}'
            f'<text x="{left}" y="{h - 6}" class="ax">{s.index[0]:%m/%d}</text>'
            f'<text x="{x(n):.1f}" y="{h - 6}" class="ax" text-anchor="end">{s.index[-1]:%m/%d}</text></svg>')


def hero_html(r):
    """ファーストビュー：サイトの説明と、日経平均の今日・前日の終値とチャート"""
    esc = html.escape
    wd = "月火水木金土日"
    jp, us, df = r.get("jp"), r["us"], r["df"]

    def arrow(v):
        return ("up", "▲") if v > 0 else ("down", "▼") if v < 0 else ("", "±")

    if jp:
        chg = jp["last"] - jp["prev"]
        ret = chg / jp["prev"]
        k, a = arrow(chg)
        c6 = jp["close"]
        off_high = jp["last"] / c6.max() - 1
        label = "現在値（取引中）" if jp["live"] else "終値"
        card = f"""<div class="icard">
<div class="ihead"><span class="iname">日経平均株価</span><span class="sub">{jp["date"]:%Y/%m/%d}（{wd[jp["date"].weekday()]}）{label}</span></div>
<div class="inow">{jp["last"]:,.2f}<span class="iunit">円</span></div>
<div class="ichg {k}">{a} {chg:+,.2f}円（{ret:+.2%}）</div>
<div class="icmp">
 <div><span class="sub">前日終値 {jp["prev_date"]:%m/%d}（{wd[jp["prev_date"].weekday()]}）</span><b>{jp["prev"]:,.2f}円</b></div>
 <div><span class="sub">半年の高値</span><b>{c6.max():,.0f}円</b><small class="{arrow(off_high)[0]}"> 高値から{off_high:+.1%}</small></div>
</div>
{index_chart(c6)}
<div class="ilegend"><span><i class="lg ln"></i>日経平均（直近3か月）</span><span><i class="lg ma"></i>25日移動平均</span><span><i class="dot prev"></i>前日</span><span><i class="dot now"></i>{"現在" if jp["live"] else "今日"}</span></div>
</div>"""
    else:
        card = '<div class="icard"><p class="note">日経平均を取得できませんでした。</p></div>'

    minis = "".join(
        f'<div class="mini"><span class="sub">{nm}</span><b>{us["summary"][s]["close"]:,.2f}</b>'
        f'<small class="{arrow(us["summary"][s]["ret"])[0]}">{us["summary"][s]["ret"]:+.2%}</small></div>'
        for s, nm in (("JPY=X", "ドル円"), ("^GSPC", "S&amp;P500（前日）"), ("^SOX", "SOX半導体（前日）"))
        if s in us["summary"])

    top = df.iloc[0] if len(df) else None
    early = df.sort_values("先回り", ascending=False).iloc[0] if len(df) else None
    n_sp = len(r["surprises"]["picks"])
    points = "".join(p for p in (
        f'<li><span class="sub">いま最も勢いのあるテーマ</span><b>{esc(top["テーマ"])}</b><small>総合 {top["総合"]:.0f}点</small></li>' if top is not None else "",
        f'<li><span class="sub">これから動きそうなテーマ</span><b>{esc(early["テーマ"])}</b><small>先回り {early["先回り"]:.0f}点</small></li>' if early is not None else "",
        f'<li><span class="sub">決算サプライズ（直近{SURPRISE_DAYS}営業日）</span><b>{n_sp}銘柄</b><small>上方修正・増配・大幅増益</small></li>'))

    return f"""<header class="hero">
<div class="htext">
<p class="eyebrow">日本株 × テーマ投資 ・ 毎営業日更新</p>
<h1>日本株 テーマ注目度ランキング</h1>
<p class="lead">いま東京市場でお金が集まっているテーマと銘柄はどれか。株価・売買代金・ニュース件数・決算開示（TDnet）・信用残を毎日集計して、<b>勢いのあるテーマ</b>、<b>これから動きそうなテーマ</b>、<b>決算サプライズ銘柄</b>をひと目でわかるようにまとめた日本株の分析サイトです。</p>
<ul class="points">{points}</ul>
<div class="minis">{minis}</div>
<p class="note">{r["date"]:%Y/%m/%d} 終値ベース。投資助言ではありません。売買の判断はご自身の責任でお願いします。</p>
</div>
{card}
</header>"""


def write_html(df, date, history, details, stocks, missing, early_section="", tabs=None, hero=""):
    tabs = tabs or {}

    def cls(x):
        return "" if pd.isna(x) else "up" if x > 0 else "down" if x < 0 else ""

    def num(v, fmt, extra=""):
        if v is None or pd.isna(v):
            return '<td class="num" data-v="">-</td>'
        if fmt.endswith("%}") and abs(v) >= 10:  # +1000% 以上の外れ値は桁を詰めて表示（並べ替えは実数で）
            return f'<td class="num {extra}" data-v="{v}" title="{fmt.format(v)}">{"+" if v > 0 else "-"}999%超</td>'
        return f'<td class="num {extra}" data-v="{v}">{fmt.format(v)}</td>'

    def stock_cells(x):
        streak = x["op_streak"]
        per = num(x["per_used"], "{:.1f}" + ("※" if x["suspect"] else ""), "warn" if x["suspect"] else "")
        return (num(x["ret"], "{:+.2%}", cls(x["ret"]))
                + num(x["op_cagr"], "{:+.0%}", cls(x["op_cagr"]))
                + num(streak, "{:.0f}年", "good" if streak == 3 else "")
                + num(x["rev_cagr"], "{:+.0%}", cls(x["rev_cagr"]))
                + num(x["q_earn"], "{:+.0%}", cls(x["q_earn"]))
                + num(x["q_rev"], "{:+.0%}", cls(x["q_rev"]))
                + per + num(x["pbr"], "{:.2f}") + num(x["roe"], "{:.1%}")
                + num(x.get("eq_ratio"), "{:.0%}") + num(x.get("op_eq"), "{:.1%}") + num(x["div"], "{:.2f}%")
                + num(x["growth"], "{:.0f}", "score") + num(x["value"], "{:.0f}")
                + num(x["total"], "{:.0f}", "score") + margin_cells(x.get("margin")))

    stock_head = ('<th class="num">騰落率</th>'
                  '<th class="num" title="直近3期の営業利益の年平均成長率">営業益<br>3年</th>'
                  '<th class="num" title="営業利益が前期を上回った連続年数（最大3）">連続<br>増益</th>'
                  '<th class="num" title="直近3期の売上の年平均成長率">売上<br>3年</th>'
                  '<th class="num" title="直近四半期の利益 前年同期比">直近Q<br>利益</th>'
                  '<th class="num" title="直近四半期の売上 前年同期比">直近Q<br>売上</th>'
                  '<th class="num">PER</th><th class="num">PBR</th><th class="num">ROE</th>'
                  '<th class="num" title="自己資本 ÷ 総資産（直近の年次）">自己資本<br>比率</th>'
                  '<th class="num" title="営業利益 ÷ 自己資本（直近の年次）">簡易<br>OP</th><th class="num">配当</th>'
                  '<th class="num">成長度</th><th class="num">割安度</th>'
                  '<th class="num" data-dir="desc" title="成長度60%＋割安度40%">評価</th>'
                  + MARGIN_HEAD)

    body = []
    for i, r in df.iterrows():
        nr = "-" if pd.isna(r["ニュース倍率"]) else f'{r["ニュース倍率"]:.1f}倍 ({r["記事数"]:.0f}件)'
        members = "".join(
            f'<tr><td>{x["code"]}</td><td class="name">{html.escape(x["name"])}{spec_badge(x)}</td>{stock_cells(x)}</tr>'
            for x in details[r["テーマ"]])
        body.append(f"""
<tr>
 <td class="num" data-v="{i}">{i}</td>
 <td><details><summary>{html.escape(r["テーマ"])}</summary>
  <table class="members sortable"><thead><tr><th>コード</th><th>銘柄</th>{stock_head}</tr></thead><tbody>{members}</tbody></table>
 </details></td>
 <td class="num score" data-v="{r["総合"]}" style="--s:{r["総合"]:.0f}">{r["総合"]:.0f}</td>
 <td class="num {cls(r["前日比"])}" data-v="{r["前日比"]}">{r["前日比"]:+.0f}</td>
 <td class="num {cls(r["5日比"])}" data-v="{r["5日比"]}">{r["5日比"]:+.0f}</td>
 <td class="num" data-v="{r["売買代金倍率"]}">{r["売買代金倍率"]:.2f}倍<br><small class="{cls(r["売買代金倍率"] - 1)}">{r["売買代金倍率"] - 1:+.0%}</small></td>
 <td class="num {cls(r["対TOPIX"])}" data-v="{r["対TOPIX"]}">{r["対TOPIX"]:+.1%}</td>
 <td class="num" data-v="{r["上昇比率"]}">{r["上昇比率"]:.0%}</td>
 <td class="num" data-v="{"" if pd.isna(r["ニュース倍率"]) else r["ニュース倍率"]}">{nr}</td>
 {num(r["成長度"], "{:.0f}", "score")}
 {num(r["割安度"], "{:.0f}")}
 <td>{sparkline(history[r["テーマ"]])}</td>
</tr>""")

    stocks = sorted(stocks, key=lambda x: -x["total"] if pd.notna(x["total"]) else 1)
    all_rows = "".join(
        f'<tr><td>{x["code"]}<br><small class="mkt">{x.get("market") or "-"}</small></td>'
        f'<td class="name">{html.escape(x["name"])}{spec_badge(x)}</td><td class="themes">{theme_label(x)}</td>{stock_cells(x)}</tr>'
        for x in stocks)

    note = f'<p class="note">データ取得できなかった銘柄: {", ".join(missing)}</p>' if missing else ""
    page = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>日本株 テーマ注目度ランキング｜日経平均と注目テーマ・決算サプライズ銘柄</title>
<meta name="description" content="{SITE_DESC}">
<style>
:root{{--bg:#fafaf9;--fg:#1c1917;--muted:#78716c;--line:#e7e5e4;--up:#c2410c;--down:#1d4ed8;--accent:#0f766e;--warn:#b45309}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#1c1917;--fg:#f5f5f4;--muted:#a8a29e;--line:#44403c;--up:#fb923c;--down:#60a5fa;--accent:#2dd4bf;--warn:#fbbf24}}}}
:root[data-theme="dark"]{{--bg:#1c1917;--fg:#f5f5f4;--muted:#a8a29e;--line:#44403c;--up:#fb923c;--down:#60a5fa;--accent:#2dd4bf;--warn:#fbbf24}}
body{{background:var(--bg);color:var(--fg);font-family:system-ui,"Hiragino Sans","Yu Gothic UI",sans-serif;margin:0;padding:24px 16px}}
main{{max-width:1200px;margin:auto}} h1{{font-size:1.4rem;margin:0 0 4px}} h2{{font-size:1.1rem;margin:40px 0 4px}}
.note,small,.sub{{color:var(--muted)}} .wrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:.8rem}}
th,td{{padding:6px 3px;border-bottom:1px solid var(--line);vertical-align:top;white-space:nowrap}}
td.wrapcell{{white-space:normal;min-width:7em}} td.name{{white-space:normal;min-width:6em}} .badge{{white-space:nowrap}} .badge.calm{{white-space:normal}}
th{{text-align:left;color:var(--muted);font-weight:500}} .sortable th{{cursor:pointer;user-select:none}} .sortable th:hover{{color:var(--fg)}} .sortable th[data-dir=desc]::after{{content:" ▼"}} .sortable th[data-dir=asc]::after{{content:" ▲"}}
.num{{text-align:right;font-variant-numeric:tabular-nums}} .up{{color:var(--up)}} .down{{color:var(--down)}} .warn{{color:var(--warn)}} .good{{color:var(--accent);font-weight:600}}
.score{{font-weight:700}} td.score[style]{{background:linear-gradient(90deg,color-mix(in srgb,var(--accent) 25%,transparent) calc(var(--s)*1%),transparent 0)}}
summary{{cursor:pointer}} .members{{margin:6px 0 10px}} .members td,.members th{{border-bottom:1px solid var(--line);padding:3px 6px;font-size:.8rem}}
.themes{{color:var(--muted);font-size:.8rem;white-space:normal;min-width:6em}} .jp{{font-size:.8rem;white-space:normal;min-width:12em}}
.badge{{display:inline-block;border:1px solid var(--line);border-radius:4px;padding:1px 6px;margin:1px 2px;font-size:.78rem}} .badge.news{{border-color:var(--up);color:var(--up)}} .badge.ev{{border-color:var(--accent);color:var(--accent)}} .badge.calm{{color:var(--muted)}} .badge.bad{{border-color:var(--down);color:var(--down)}} .spark polyline{{fill:none;stroke:var(--accent);stroke-width:1.5}} .spark .mid{{stroke:var(--line);stroke-dasharray:2 2}}
.tabs{{position:sticky;top:0;z-index:5;background:var(--bg);display:flex;gap:4px;flex-wrap:wrap;padding:8px 0;border-bottom:1px solid var(--line);margin:12px 0 0}}
.tabs button{{font:inherit;font-size:.9rem;padding:6px 12px;border:1px solid var(--line);border-radius:6px;background:transparent;color:var(--muted);cursor:pointer}}
.tabs button[aria-selected=true]{{color:var(--fg);border-color:var(--accent);background:color-mix(in srgb,var(--accent) 12%,transparent);font-weight:600}}
.tab[hidden]{{display:none}}
#all-table td.num small{{white-space:normal;display:inline-block;min-width:5em}} #all-table td.wrapcell{{min-width:6em}} #all-table td.themes{{min-width:5em}}
.sp td.docs{{min-width:8em}} .sp td.docs a{{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin-bottom:4px}}
.diag{{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}} .diag input{{font:inherit;flex:1;min-width:220px;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}}
.diag button{{font:inherit;padding:8px 18px;border:0;border-radius:6px;background:var(--accent);color:#fff;cursor:pointer}} .diag button:disabled{{opacity:.5;cursor:wait}}
.hero{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.1fr);gap:28px;align-items:start;padding:8px 0 24px}}
@media (max-width:860px){{.hero{{grid-template-columns:minmax(0,1fr)}}}}
.eyebrow{{margin:0 0 6px;color:var(--accent);font-weight:600;font-size:.85rem;letter-spacing:.04em}}
.hero h1{{font-size:clamp(1.6rem,3.4vw,2.3rem);line-height:1.25;margin:0 0 12px;text-wrap:balance}}
.lead{{line-height:1.8;margin:0 0 16px}}
.points{{list-style:none;padding:0;margin:0 0 14px;display:grid;gap:8px}}
.points li{{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:baseline;gap:2px 12px;padding:10px 12px;border:1px solid var(--line);border-radius:8px;border-left:3px solid var(--accent)}}
.points li .sub{{grid-column:1/-1;font-size:.78rem}} .points li b{{font-size:1.05rem}}
.minis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin:0 0 10px}}
.mini{{display:flex;flex-direction:column;padding:8px 10px;border:1px solid var(--line);border-radius:8px}} .mini .sub{{font-size:.75rem}} .mini b{{font-size:1.05rem;font-variant-numeric:tabular-nums}}
.icard{{border:1px solid var(--line);border-radius:12px;padding:18px 18px 12px;background:color-mix(in srgb,var(--accent) 4%,var(--bg))}}
.ihead{{display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px 12px;align-items:baseline}} .iname{{font-weight:700}}
.inow{{font-size:clamp(2.2rem,5vw,3.2rem);font-weight:700;font-variant-numeric:tabular-nums;line-height:1.15;margin-top:6px}} .iunit{{font-size:1rem;margin-left:4px;color:var(--muted);font-weight:500}}
.ichg{{font-size:1.15rem;font-weight:700;font-variant-numeric:tabular-nums}}
.icmp{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px;margin:12px 0 4px}} .icmp div{{display:flex;flex-direction:column;padding:8px 10px;border-radius:8px;background:var(--bg);border:1px solid var(--line)}} .icmp .sub{{font-size:.75rem}} .icmp b{{font-variant-numeric:tabular-nums}}
.ichart{{width:100%;height:auto;display:block;margin-top:8px}} .ichart .grid{{stroke:var(--line)}} .ichart .ax{{fill:var(--muted);font-size:11px}}
.ichart .ln{{fill:none;stroke:var(--accent);stroke-width:2.2;stroke-linejoin:round}} .ichart .ma{{fill:none;stroke:var(--muted);stroke-width:1.2;stroke-dasharray:4 3}}
.ichart .now{{fill:var(--accent);stroke:var(--bg);stroke-width:2}} .ichart .prev{{fill:var(--bg);stroke:var(--accent);stroke-width:2}}
.ilegend{{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:.75rem;color:var(--muted)}} .ilegend span{{display:inline-flex;align-items:center;gap:5px}}
.lg{{display:inline-block;width:16px;height:0;border-top:2px solid var(--accent)}} .lg.ma{{border-top:1.5px dashed var(--muted)}}
.dot{{display:inline-block;width:9px;height:9px;border-radius:50%;border:2px solid var(--accent)}} .dot.now{{background:var(--accent)}}
</style></head><body><main>
{hero}
<p class="sub">{date} 終値ベース ・ 表の見出しクリックで並べ替え</p>
<nav class="tabs" role="tablist">
<button role="tab" data-tab="morning">朝のチェック</button>
<button role="tab" data-tab="diag">銘柄診断</button>
<button role="tab" data-tab="light">需給が軽い銘柄</button>
<button role="tab" data-tab="dips">押し目・出遅れ</button>
<button role="tab" data-tab="ups">上昇トレンド</button>
<button role="tab" data-tab="quality">割安×高収益</button>
<button role="tab" data-tab="themes">テーマ注目度</button>
<button role="tab" data-tab="all">全銘柄</button>
</nav>
<section class="tab" id="tab-morning">{early_section}</section>
<section class="tab" id="tab-diag" hidden>
<h2>銘柄診断：需給が軽く、業績も伸びていて、上昇トレンドにあるか</h2>
<p class="sub">銘柄コードを入力して「診断する」を押すと、最新の株価・業績・信用残を取得して判定します（テーマ外の銘柄も可）。
業績＝成長度60以上／需給＝「下がりやすい」でなく信用買残が出来高の2日分未満・投機的過熱でない／
トレンド＝25日線・75日線の上で両方の線が上向き（過熱＝25日線より15%以上上・RSI 75超）。</p>
<form id="diag-form" class="diag"><input id="diag-codes" placeholder="例: 7974 6857 285A 4475" autocomplete="off">
<button type="submit">診断する</button></form>
<div id="diag-result"></div>
</section>
<section class="tab" id="tab-light" hidden>{tabs.get("light", "")}</section>
<section class="tab" id="tab-dips" hidden>{tabs.get("dips", "")}</section>
<section class="tab" id="tab-ups" hidden>{tabs.get("ups", "")}</section>
<section class="tab" id="tab-quality" hidden>{tabs.get("quality", "")}</section>
<section class="tab" id="tab-themes" hidden>
<h2>注目度ランキング</h2>
<p class="sub">総合 = 株価スコア80% + ニュース20%（50が平常、70超で注目）・ 推移は株価スコアの30営業日</p>
<div class="wrap"><table class="sortable">
<thead><tr><th>#</th><th>テーマ（クリックで銘柄）</th><th class="num">総合</th><th class="num">前日比</th><th class="num">5日比</th>
<th class="num">売買代金<br>対20日平均</th><th class="num">対TOPIX</th><th class="num">上昇比率</th><th class="num">ニュース倍率</th><th class="num">成長度</th><th class="num">割安度</th><th>推移</th></tr></thead>
<tbody>{"".join(body)}</tbody>
</table></div>
</section>

<section class="tab" id="tab-all" hidden>
<h2>全銘柄の業績・割安度（{len(stocks)}銘柄）</h2>
<p class="sub">対象 = 東証プライム・スタンダードの全内国株式（日経225の構成銘柄もすべて含む）＋ themes.yaml のテーマ銘柄。
テーマ外の銘柄はテーマ欄に東証の業種を表示。財務指標はテーマ銘柄は毎日、それ以外は最大{FUND_MAX_AGE}日前の取得分を使用。
<b>評価 = 成長度60% + 割安度40%</b>（初期表示は評価の高い順）。成長度 = 営業利益3年成長率30%・連続増益20%・売上3年成長率20%・直近四半期の利益15%・売上15%（前年同期比）。割安度 = PER（低い）・PBR（低い）・ROE（高い）・配当利回り（高い）。いずれも全銘柄内の順位から0〜100で算出。
※ = 予想PERが実績の1/2.5未満でデータ誤りの疑い（割安度は実績PERで計算）。見出しクリックで並べ替え。</p>
<p><select id="all-market"><option value="">すべての市場</option><option>プライム</option><option>スタンダード</option><option value="theme">テーマ銘柄のみ</option></select>
<input id="all-q" type="search" placeholder="コード・銘柄名・業種で絞り込み" style="width:16em"> <span id="all-count" class="sub"></span></p>
<div class="wrap"><table class="sortable" id="all-table">
<thead><tr><th>コード<br>市場</th><th>銘柄</th><th>テーマ／業種</th>{stock_head}</tr></thead>
<tbody>{all_rows}</tbody>
</table></div>{note}
</section>
<p class="note">投資助言ではありません。データ: Yahoo Finance（yfinance、遅延や誤りを含む場合あり）、Googleニュース、東証（適時開示・信用取引残高）。</p>
</main>
<script>
(() => {{
  const btns = [...document.querySelectorAll(".tabs button")];
  const show = name => {{
    if (!document.getElementById("tab-" + name)) name = "morning";
    btns.forEach(b => b.setAttribute("aria-selected", b.dataset.tab === name));
    document.querySelectorAll(".tab").forEach(s => s.hidden = s.id !== "tab-" + name);
    try {{ localStorage.setItem("tab", name); }} catch (e) {{}}
  }};
  btns.forEach(b => b.addEventListener("click", () => {{ show(b.dataset.tab); history.replaceState(null, "", "#" + b.dataset.tab); }}));
  let start = location.hash.slice(1);
  if (!start) {{ try {{ start = localStorage.getItem("tab") || "morning"; }} catch (e) {{ start = "morning"; }} }}
  show(start);
}})();
function makeSortable(t) {{
  const head = t.tHead.rows[0];
  [...head.cells].forEach((th, i) => th.addEventListener("click", e => {{
    e.stopPropagation();
    const dir = th.dataset.dir === "desc" ? "asc" : "desc";
    [...head.cells].forEach(h => delete h.dataset.dir);
    th.dataset.dir = dir;
    const val = r => {{
      const c = r.cells[i];
      if (c.dataset.v === undefined) return c.textContent.trim();
      return c.dataset.v === "" ? null : +c.dataset.v;
    }};
    const tb = t.tBodies[0];
    [...tb.rows].sort((a, b) => {{
      const x = val(a), y = val(b);
      if (x === null) return 1;
      if (y === null) return -1;
      const d = typeof x === "number" ? x - y : String(x).localeCompare(String(y), "ja");
      return dir === "asc" ? d : -d;
    }}).forEach(r => tb.appendChild(r));
  }}));
}}
document.querySelectorAll("table.sortable").forEach(makeSortable);

// 全銘柄タブの絞り込み（市場・キーワード）
(() => {{
  const t = document.getElementById("all-table"), sel = document.getElementById("all-market"),
        q = document.getElementById("all-q"), cnt = document.getElementById("all-count");
  if (!t) return;
  const apply = () => {{
    const m = sel.value, k = q.value.trim().toLowerCase();
    let n = 0;
    for (const r of t.tBodies[0].rows) {{
      const themeCell = r.cells[2];
      const okM = !m || (m === "theme" ? !themeCell.querySelector(".sector") : r.cells[0].querySelector(".mkt").textContent === m);
      const okK = !k || (r.cells[0].textContent + r.cells[1].textContent + themeCell.textContent).toLowerCase().includes(k);
      r.hidden = !(okM && okK);
      n += !r.hidden;
    }}
    cnt.textContent = n + "銘柄";
  }};
  sel.addEventListener("change", apply);
  q.addEventListener("input", apply);
  apply();
}})();

// 銘柄診断：run.bat で起動したローカルサーバー（http://localhost:8765）に問い合わせる
(() => {{
  const form = document.getElementById("diag-form");
  if (!form) return;
  const input = document.getElementById("diag-codes"), out = document.getElementById("diag-result"),
        btn = form.querySelector("button");
  try {{ input.value = localStorage.getItem("diag-codes") || ""; }} catch (e) {{}}
  form.addEventListener("submit", async ev => {{
    ev.preventDefault();
    const codes = input.value.trim();
    if (!codes) {{ out.innerHTML = '<p class="down">銘柄コードを入力してください（例: 7974 6857 285A）</p>'; return; }}
    try {{ localStorage.setItem("diag-codes", codes); }} catch (e) {{}}
    if (location.protocol === "file:") {{
      out.innerHTML = '<p class="down">診断は run.bat で起動したサーバー経由で開いたときだけ使えます。' +
        'ブラウザで <a href="http://localhost:8765/#diag">http://localhost:8765/</a> を開いてください。</p>';
      return;
    }}
    btn.disabled = true;
    out.innerHTML = '<p class="sub">最新データを取得して計算中です…（1銘柄あたり数秒）</p>';
    try {{
      const res = await fetch("/api/diagnose?codes=" + encodeURIComponent(codes));
      const data = await res.json();
      out.innerHTML = data.html || '<p class="down">' + (data.error || "エラーが発生しました") + "</p>";
      out.querySelectorAll("table.sortable").forEach(makeSortable);
    }} catch (e) {{
      out.innerHTML = '<p class="down">サーバーに接続できません。run.bat の黒い画面が開いているか確認してください。</p>';
    }} finally {{ btn.disabled = false; }}
  }});
}})();
</script></body></html>"""
    (BASE / "report.html").write_text(page, encoding="utf-8")
    # 公開用（レンタルサーバーに置く静的ページ）。銘柄診断はローカルサーバーが要るので外す
    pub = page.replace('<button role="tab" data-tab="diag">銘柄診断</button>\n', "")
    pub = re.sub(r'<section class="tab" id="tab-diag" hidden>.*?</section>\n', "", pub, flags=re.S)
    title = re.search(r"<title>(.*?)</title>", pub).group(1)
    pub = pub.replace("</title>\n", "</title>\n"
                      f'<link rel="canonical" href="{SITE_URL}">\n'
                      f'<meta property="og:type" content="website"><meta property="og:url" content="{SITE_URL}">\n'
                      f'<meta property="og:title" content="{title}"><meta property="og:description" content="{SITE_DESC}">\n'
                      '<meta property="og:locale" content="ja_JP"><meta name="twitter:card" content="summary">\n', 1)
    out = BASE / "public"
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(pub, encoding="utf-8")
    (out / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{SITE_URL}</loc><lastmod>{dt.date.today():%Y-%m-%d}</lastmod><changefreq>daily</changefreq></url>\n"
        "</urlset>\n", encoding="utf-8")
    (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}sitemap.xml\n", encoding="utf-8")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-news", action="store_true", help="ニュース件数を取得しない")
    args = ap.parse_args()

    r = build(load_themes(), not args.no_news)
    print_table(r["df"], r["date"])
    print_early(r)
    if r["missing"]:
        print(f"\n※データ取得できなかった銘柄: {', '.join(r['missing'])}")
    save_history(r["df"], r["date"], r["today"])
    r["measured_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    r["news_rows"] = news_rows(r, r["measured_at"])
    r["news_prev"] = load_prev_weekday_news(r["today"])
    save_news_history(r["news_rows"])
    tabs = screens_html(r["screens"], r["date"], r["margin_date"])
    tabs["quality"] = quality_html(r["screens"]["quality"], len(r["stocks"]), r["date"])
    write_html(r["df"], r["date"], r["history"], r["details"], r["stocks"], r["missing"], early_html(r), tabs, hero_html(r))
    print(f"\n保存: report.html / public/index.html / history.csv")


if __name__ == "__main__":
    main()
