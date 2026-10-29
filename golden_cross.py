"""
日足のゴールデンクロス（5日線が25日線を下から上に抜ける）が近い銘柄を抽出する。

対象: 東証プライム・スタンダードの内国株式（theme_score.load_universe の一覧）
株価: yfinance の日足1年分（theme_score.fetch_prices。最終日の終値が欠けた銘柄は60分足で補う）

「近い」の条件（すべて満たすもの）
  1. 今は 5日線 < 25日線（まだクロスしていない）
  2. 5日線が上向き（今日の5日線 > 3営業日前の5日線）で、25日線との差が3営業日前より縮まっている
  3. 翌営業日の終値が「GC価格」以上ならクロスする。GC価格が今日の終値から +MAX_RISE 以内
  4. 直近20日の平均売買代金が MIN_TURNOVER 円以上（流動性の低い銘柄を除く）

GC価格: 翌日の終値を P とすると、5日線 = (直近4日の終値の合計 + P) / 5、25日線 = (直近24日の合計 + P) / 25。
5日線 >= 25日線 となる最小の P を解いたもの。

使い方:  python golden_cross.py [--max-rise 0.03] [--min-turnover 1e8] [--market プライム]
結果は golden_cross_YYYYMMDD.csv に保存し、上位を画面に出す。投資助言ではありません。
"""
import argparse
import sys

import pandas as pd

from theme_score import BASE, BENCH, fetch_prices, load_universe

SHORT, LONG = 5, 25
LOOKBACK = 3          # 5日線の向き・差の縮まりを何営業日前と比べるか
MAX_RISE = 0.03       # GC価格が今日の終値からこの割合以内
MIN_TURNOVER = 1e8    # 直近20日の平均売買代金（円）


def gc_price(c):
    """翌日の終値がこの価格以上なら 5日線 >= 25日線 になる"""
    a = c.iloc[-(SHORT - 1):].sum()
    b = c.iloc[-(LONG - 1):].sum()
    return (b / LONG - a / SHORT) / (1 / SHORT - 1 / LONG)


def screen(close, volume, stocks, max_rise, min_turnover):
    near, crossed = [], []
    for sym in close.columns:
        if sym == BENCH:
            continue
        c = close[sym].dropna()
        if len(c) < LONG + LOOKBACK + 1:
            continue
        v = volume[sym].reindex(c.index).fillna(0)
        turnover = (c * v).iloc[-20:].mean()
        if turnover < min_turnover:
            continue
        s, l = c.rolling(SHORT).mean(), c.rolling(LONG).mean()
        gap, gap_prev = s.iloc[-1] - l.iloc[-1], s.iloc[-1 - LOOKBACK] - l.iloc[-1 - LOOKBACK]
        code = sym.removesuffix(".T")
        info = stocks.get(code, {})
        row = {
            "コード": code, "銘柄名": info.get("name", ""), "市場": info.get("market", ""),
            "業種": info.get("sector", ""), "終値": round(c.iloc[-1], 1),
            "5日線": round(s.iloc[-1], 1), "25日線": round(l.iloc[-1], 1),
            "乖離(5日線-25日線)%": round(gap / l.iloc[-1] * 100, 2),
            "25日線の向き": "上向き" if l.iloc[-1] > l.iloc[-1 - LOOKBACK] else "下向き",
            "売買代金20日平均(億円)": round(turnover / 1e8, 1),
        }
        # 今日クロスした銘柄（参考）
        if gap >= 0 and s.iloc[-2] < l.iloc[-2]:
            crossed.append(row)
            continue
        if gap >= 0:
            continue
        if not (s.iloc[-1] > s.iloc[-1 - LOOKBACK] and gap > gap_prev):
            continue
        p = gc_price(c)
        rise = p / c.iloc[-1] - 1
        if rise > max_rise:
            continue
        row["GC価格(翌日終値)"] = round(p, 1)
        row["GC価格まで%"] = round(rise * 100, 2)
        near.append(row)
    near = pd.DataFrame(near)
    if not near.empty:
        near = near.sort_values("GC価格まで%").reset_index(drop=True)
    return near, pd.DataFrame(crossed)


def main():
    ap = argparse.ArgumentParser(description="日足のゴールデンクロス（5日線×25日線）が近い銘柄を抽出")
    ap.add_argument("--max-rise", type=float, default=MAX_RISE, help="GC価格が終値からこの割合以内（既定 0.03＝3%%）")
    ap.add_argument("--min-turnover", type=float, default=MIN_TURNOVER, help="直近20日の平均売買代金の下限（円、既定 1e8）")
    ap.add_argument("--market", choices=["プライム", "スタンダード"], help="市場を絞る")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    stocks = load_universe()
    if args.market:
        stocks = {k: v for k, v in stocks.items() if v.get("market") == args.market}
    print(f"対象 {len(stocks):,}銘柄の日足を取得中…（数分かかります）")
    close, volume = fetch_prices(list(stocks))
    day = close.index[-1].date()
    near, crossed = screen(close, volume, stocks, args.max_rise, args.min_turnover)

    out = BASE / f"golden_cross_{day:%Y%m%d}.csv"
    near.to_csv(out, index=False, encoding="utf-8-sig")
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 20)
    pd.set_option("display.unicode.east_asian_width", True)
    print(f"\n{day} 終値時点  ゴールデンクロスが近い銘柄: {len(near)}件"
          f"（GC価格まで+{args.max_rise:.0%}以内・売買代金{args.min_turnover / 1e8:g}億円以上）")
    if not near.empty:
        cols = ["コード", "銘柄名", "市場", "終値", "GC価格(翌日終値)", "GC価格まで%", "乖離(5日線-25日線)%",
                "25日線の向き", "売買代金20日平均(億円)"]
        print(near[cols].head(40).to_string(index=False))
    print(f"\n参考: 今日ゴールデンクロスした銘柄 {len(crossed)}件")
    if not crossed.empty:
        print(crossed[["コード", "銘柄名", "終値", "25日線の向き", "売買代金20日平均(億円)"]].head(20).to_string(index=False))
    print(f"\n保存: {out}")


if __name__ == "__main__":
    main()
