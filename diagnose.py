"""銘柄診断：入力された銘柄コードについて「業績が伸びている／需給が軽い／上昇トレンド」を判定する

判定の条件はダッシュボードのタブと同じ
  業績  : 成長度60以上（成長度はダッシュボードの全銘柄の中での相対値）
  需給  : 需給判定が「下がりやすい」でない・信用買残が出来高の2日分未満・投機的過熱でない
  トレンド: 株価が25日線と75日線の上・25日線が75日線の上・両方の線が上向き
           過熱＝25日線より15%以上上、またはRSI 75超、または投機的過熱
"""

import datetime as dt
import html
import io
import json
import re
import sys
from pathlib import Path

import pandas as pd

import margin
import squeeze
import theme_score as ts

BASE = Path(__file__).resolve().parent
LIST_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"


def listed_names():
    """東証の上場銘柄一覧から {コード: 銘柄名}（1日1回取得して保存）"""
    path = BASE / "listed_names.json"
    if path.exists():
        cache = json.loads(path.read_text(encoding="utf-8"))
        if cache.get("date") == str(dt.date.today()):
            return cache["names"]
    try:
        body = margin._get(LIST_PAGE, timeout=20).decode("utf-8", "replace")
        href = re.findall(r'href="([^"]+data_j\.xlsx?)"', body)[0]
        df = pd.read_excel(io.BytesIO(margin._get("https://www.jpx.co.jp" + href)), dtype={"コード": str})
        names = dict(zip(df["コード"], df["銘柄名"]))
        path.write_text(json.dumps({"date": str(dt.date.today()), "names": names}, ensure_ascii=False),
                        encoding="utf-8")
        return names
    except Exception as e:
        print(f"  上場銘柄一覧の取得失敗: {e}", file=sys.stderr)
        return json.loads(path.read_text(encoding="utf-8"))["names"] if path.exists() else {}


def parse_codes(text):
    """「7974, 6857 285a」→ ["7974", "6857", "285A"]（重複は除く）"""
    out = []
    for tok in re.split(r"[\s,、，]+", text.strip().upper()):
        tok = tok.replace(".T", "")
        if re.fullmatch(r"\d{3}[0-9A-Z]", tok) and tok not in out:
            out.append(tok)
    return out[:30]


def judge(x):
    """1銘柄の3条件の判定"""
    m = x.get("margin")
    g = x.get("growth")
    # 業績
    if g is None or pd.isna(g):
        growth = ("na", "業績データなし")
    else:
        detail = f"成長度{g:.0f}"
        if x.get("op_cagr") is not None and not pd.isna(x.get("op_cagr")):
            detail += f"・営業益3年{x['op_cagr']:+.0%}"
        if x.get("op_streak") is not None and not pd.isna(x.get("op_streak")):
            detail += f"・{x['op_streak']:.0f}年連続増益"
        growth = ("ok" if g >= 60 else "ng", detail)
    # 需給
    if x.get("speculative"):
        supply = ("ng", f"投機的過熱（{x['speculative']}）")
    elif not m:
        supply = ("na", "信用残データなし（信用取引の対象外など）")
    else:
        days = m["days"] or 0
        detail = f"{m['direction']}・買残 出来高{days:.1f}日分"
        if m.get("cost_gap") is not None:
            detail += f"・信用買い{m['cost_gap']:+.0%}"
        ok = m["direction"] != "下がりやすい" and days < 2
        supply = ("ok" if ok else "ng", detail)
    # トレンド
    if x.get("rsi") is None:
        trend = ("na", "株価データ不足")
    else:
        up = all(x.get(k) for k in ("above25", "above75", "ma25_over_75", "ma25_up", "ma75_up"))
        hot = x["gap25"] > 0.15 or x["rsi"] > 75 or bool(x.get("speculative"))
        pos = f"25日線{x['gap25']:+.0%}・RSI{x['rsi']:.0f}・高値比{x['from_high']:+.0%}"
        if up and not hot:
            trend = ("ok", "上昇中・過熱なし（" + pos + "）")
        elif up and hot:
            trend = ("warn", "上昇中だが過熱（" + pos + "）")
        elif x.get("above75") and not x.get("above25"):
            trend = ("warn", "上昇トレンド中の一時的な下げ（" + pos + "）")
        elif x.get("above75"):
            trend = ("warn", "上昇途中（移動平均線の向きがまだそろっていない）（" + pos + "）")
        else:
            trend = ("ng", "75日線より下（下落・横ばい）（" + pos + "）")

    states = [growth[0], supply[0], trend[0]]
    if states == ["ok", "ok", "ok"]:
        verdict = ("ok", "3条件すべて満たす")
    elif growth[0] == "ok" and supply[0] == "ok" and trend[0] == "warn":
        verdict = ("warn", "押し目待ち（過熱）" if "過熱" in trend[1]
                   else "押し目候補" if "一時的な下げ" in trend[1] else "トレンド確認待ち")
    else:
        miss = sum(s in ("ng", "na") for s in states) + (trend[0] == "warn")
        verdict = ("ng", f"条件不足（{miss}つ）")
    return {"growth": growth, "supply": supply, "trend": trend, "verdict": verdict}


def diagnose(codes):
    """銘柄コードのリストを診断して、結果の行（dict）のリストを返す"""
    themes = ts.load_themes()
    theme_names = {c: n for t in themes.values() for c, n in t["tickers"].items()}
    theme_of = {}
    for th, t in themes.items():
        for c in t["tickers"]:
            theme_of.setdefault(c, []).append(th)
    names = listed_names()
    unknown = [c for c in codes if c not in names and c not in theme_names]

    close, volume = ts.fetch_prices(set(codes))
    # 成長度・割安度はダッシュボードの銘柄群（プライム・スタンダード全体）と合わせた中での相対値
    fund = ts.stock_scores(ts.fetch_fundamentals(set(theme_names) | set(codes) | ts.fresh_cached_codes(),
                                                 daily=set(theme_names) | set(codes)))
    try:
        mg = margin.load()
    except Exception:
        mg = None

    rows = []
    for c in codes:
        x = {"code": c, "name": names.get(c) or theme_names.get(c) or c, "themes": theme_of.get(c, [])}
        if f"{c}.T" not in close or close[f"{c}.T"].dropna().empty:
            x["error"] = "株価データを取得できませんでした（コードの誤り・上場廃止の可能性）"
            rows.append(x)
            continue
        if c in fund.index:
            x.update({k: (None if pd.isna(v) else v) for k, v in fund.loc[c].to_dict().items()})
        ts.with_market(x, c, close, volume, mg)
        x.update(judge(x))
        rows.append(x)
    try:
        sq, sq_dates = squeeze.analyze(rows, close, mg)
    except Exception as e:
        print(f"  踏み上げ・空売りの推測に失敗: {e}", file=sys.stderr)
        sq, sq_dates = {}, {}
    return {"rows": rows, "unknown": unknown, "date": str(close.index[-1].date()),
            "margin_date": mg["date"] if mg else None, "squeeze": sq, "squeeze_dates": sq_dates}


def render(result):
    """診断結果の表（HTML）"""
    esc = html.escape
    icon = {"ok": "✅", "warn": "⚠️", "ng": "❌", "na": "－"}
    cls = {"ok": "good", "warn": "warn", "ng": "down", "na": "note"}

    def cell(state_text):
        s, t = state_text
        return f'<td class="wrapcell" data-v="{["ng", "na", "warn", "ok"].index(s)}"><span class="{cls[s]}">{icon[s]}</span> {esc(t)}</td>'

    def n(v, fmt):
        return '<td class="num" data-v="">-</td>' if v is None or pd.isna(v) else f'<td class="num" data-v="{v}">{fmt.format(v)}</td>'

    rows = []
    for x in result["rows"]:
        if x.get("error"):
            rows.append(f'<tr><td>{x["code"]}</td><td>{esc(x["name"])}</td><td colspan="12" class="down">{esc(x["error"])}</td></tr>')
            continue
        v = x["verdict"]
        rows.append(
            f'<tr><td>{x["code"]}</td><td class="name">{esc(x["name"])}{ts.spec_badge(x)}</td>'
            f'<td data-v="{["ng", "na", "warn", "ok"].index(v[0])}"><b class="{cls[v[0]]}">{icon[v[0]]} {esc(v[1])}</b></td>'
            + cell(x["growth"]) + cell(x["supply"]) + cell(x["trend"])
            + n(x.get("price"), "{:,.0f}") + n(x.get("total"), "{:.0f}") + n(x.get("per_used"), "{:.1f}")
            + n(x.get("ret20"), "{:+.0%}") + ts.margin_cells(x.get("margin"))
            + f'<td>{esc(str(x.get("earn_date") or "-"))}</td>'
            + f'<td class="themes">{esc("・".join(x["themes"]))}</td></tr>')
    note = ""
    if result["unknown"]:
        note = f'<p class="note">東証の上場一覧に見つからないコード: {", ".join(result["unknown"])}</p>'
    return (f'<p class="sub">{result["date"]} 終値ベース・信用残は東証公表（{result["margin_date"] or "-"} 時点）。'
            '成長度・評価はダッシュボードの全銘柄と合わせた中での相対値。</p>'
            '<div class="wrap"><table class="sortable"><thead><tr><th>コード</th><th>銘柄</th>'
            '<th data-dir="desc">判定</th><th>業績</th><th>需給</th><th>トレンド</th>'
            '<th class="num">株価</th><th class="num">評価</th><th class="num">PER</th><th class="num">20日</th>'
            + ts.MARGIN_HEAD + '<th>決算予定</th><th>テーマ</th></tr></thead><tbody>'
            + "".join(rows) + '</tbody></table></div>' + note + render_squeeze(result))


def render_squeeze(result):
    """踏み上げ・空売りの推測の表（HTML）"""
    sq, d = result.get("squeeze") or {}, result.get("squeeze_dates") or {}
    if not sq:
        return ""
    esc = html.escape
    icon = {"ok": "🔥", "warn": "⚠️", "ng": "🔻", "na": "－"}
    cls = {"ok": "good", "warn": "warn", "ng": "down", "na": "note"}

    def small(lines):
        return "".join(f"<br><small>{esc(t)}</small>" for t in lines)

    def sh(v):  # 株数
        return "-" if v is None else f"{v:,.0f}株"

    rows = []
    for x in result["rows"]:
        o = sq.get(x["code"])
        if not o:
            continue
        m = x.get("margin") or {}
        # 空売りの量
        amount = [f"信用売残 {sh(m.get('sell'))}" + (f"（出来高{o['sell_days']:.1f}日分）" if o["sell_days"] is not None else "")]
        if o["jsf"]:
            amount.append(f"日証金 貸株残 {sh(o['jsf']['lend'])}・融資残 {sh(o['jsf']['loan'])}")
        sp = o["sp"]
        amount.append(f"大口の空売り {sp['total']:.2%}（{len(sp['holders'])}社）" if sp["holders"] else "大口の空売り報告なし")
        amount += [f"・{h['who'][:24]} {h['ratio']:.2%}" for h in sp["holders"][:3]]
        # 空売り側の苦しさ
        s = o["shina"]
        if s:
            press = [f"逆日歩 {s['rate'] / (s['days'] or 1):.2f}円/日" if s.get("rate") else "逆日歩なし"]
            if s.get("excess"):
                press.append(f"貸株超過 {sh(s['excess'])}")
            if s.get("limit"):
                press.append(f"制限：{s['limit']}")
        else:
            press = ["逆日歩なし（貸株の不足なし）"]
        if m.get("sell_cost"):
            press.append(f"空売り平均 {m['sell_cost']:,.0f}円（株価{m['sell_gap']:+.0%}）")
        # 日証金の動き
        j = o["jsf"]
        flow = ([f"{j['from'][4:6]}/{j['from'][6:]}〜{j['to'][4:6]}/{j['to'][6:]}（{j['days']}日分）",
                 f"新規の空売り {sh(j['new'])}", f"買い戻し {sh(j['ret'])}",
                 f"新規の信用買い {sh(j['loan_new'])}・返済売り {sh(j['loan_ret'])}"] if j else ["データなし"])
        # 1分足
        it = o["it"]
        if it:
            mins = [f"{it['date']}（前日比{it['day_ret']:+.1%}）" if it["day_ret"] is not None else it["date"]]
            mins += [f"急騰 {t[-5:]} {r:+.1%}・出来高{v:.0f}倍" for t, r, v in it["up"][:3]]
            mins += [f"売り込み {t[-5:]} {r:+.1%}・出来高{v:.0f}倍" for t, r, v in it["down"][:3]]
            if it["up_share"] is not None:
                mins.append(f"陽線の出来高 {it['up_share']:.0%}")
            mins.append(f"5日間で急騰{it['up_5d']}回・売り込み{it['down_5d']}回")
        else:
            mins = ["1分足を取得できず"]
        sqs, shs = o["squeeze"], o["short"]
        rows.append(
            f'<tr><td>{x["code"]}</td><td>{esc(x["name"])}</td>'
            f'<td class="wrapcell" data-v="{o["sq_score"]}"><b class="{cls[sqs[0]]}">{icon[sqs[0]]} {esc(sqs[1])}</b>{small(o["sq_notes"])}</td>'
            f'<td class="wrapcell" data-v="{o["sh_score"]}"><b class="{cls[shs[0]]}">{icon[shs[0]]} {esc(shs[1])}</b>{small(o["sh_notes"])}</td>'
            f'<td class="wrapcell">{small(amount)[4:]}</td><td class="wrapcell">{small(press)[4:]}</td>'
            f'<td class="wrapcell">{small(flow)[4:]}</td><td class="wrapcell">{small(mins)[4:]}</td></tr>')

    def ymd(s):
        return f"{s[:4]}/{s[4:6]}/{s[6:]}" if s and len(s) == 8 and s.isdigit() else (s or "-")

    return ('<h3>踏み上げ・空売りの推測</h3>'
            f'<p class="sub">日証金 {ymd(d.get("jsf"))} 申込分・逆日歩 {ymd(d.get("shina"))}・'
            f'空売り残高報告 {ymd(d.get("short"))} 公表分まで（{ymd(d.get("short_first"))} 以降を保存）・'
            f'1分足 {d.get("minute") or "-"}。'
            '板や歩み値には「空売り」「買い戻し」の区別が載らないため、公表データと値動きから推測したものです。確定ではありません。</p>'
            '<div class="wrap"><table class="sortable"><thead><tr><th>コード</th><th>銘柄</th>'
            '<th data-dir="desc" title="空売りの量・空売り側の苦しさ・実際の買い戻しの兆候から判定">踏み上げ</th>'
            '<th title="日証金の貸株新規、信用売残の増加、大口の空売り報告、1分足の売り込みから判定">新規の空売り</th>'
            '<th title="信用売残（東証）、日証金の貸株残、発行済株式の0.5%以上を持つ大口の空売り">空売りの量</th>'
            '<th title="逆日歩が高い・制限がある・株価が空売りの平均より上だと、買い戻しを急がされやすい">空売り側の苦しさ</th>'
            '<th title="日証金の貸借取引：貸株新規＝新規の空売り、貸株返済＝買い戻し（制度信用のみ・約2営業日遅れ）">日証金の動き</th>'
            '<th title="普段の5分の値動きの3倍以上（最低1%）を、出来高3倍以上で動いた場面">1分足</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'
            '<p class="note">日証金のデータは制度信用だけで、一般信用や機関投資家どうしの貸株は含みません。'
            '大口の空売り報告は残高が0.1%以上変わったときだけ出るため、保存を始める前からの残高は分からないことがあります。</p>')
