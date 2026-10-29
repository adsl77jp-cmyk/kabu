"""適時開示（TDnet）から「決算サプライズ」候補を拾う

前の取引日の大引け（15:00）以降〜実行時点までに出た開示のうち、
決算短信・業績予想の修正・配当予想の修正（増配）を集め、PDFの1〜2ページ目を読んで
  - 上方修正 / 下方修正（表題、または本文の「増減率」の符号で判定）
  - 増配
  - 決算短信の営業利益・売上高の前年同期比
を取り出し、サプライズ点数を付ける。

点数: 上方修正 +3 / 増配 +2 / 営業益 +30%以上 +2・+10%以上 +1 / 売上 +10%以上 +0.5
      下方修正 -3 / 営業減益 -1 / 減配 -2
PDFの解析結果は disclosures_cache.json に保存し、同じ開示は2回読まない。
"""

import datetime as dt
import io
import json
import re
import sys
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pypdf import PdfReader

BASE = Path(__file__).resolve().parent
LIST_URL = "https://www.release.tdnet.info/inbs/"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
ROW = re.compile(
    r'kjTime" noWrap>(\d\d:\d\d)</td>\s*<td[^>]*kjCode" noWrap>(\w+)</td>\s*'
    r'<td[^>]*kjName" noWrap>([^<]*)</td>\s*<td[^>]*kjTitle"[^>]*><a href="([^"]+\.pdf)"[^>]*>([^<]*)</a>', re.S)
NUM = re.compile(r"△?-?\d[\d,]*(?:\.\d+)?|[－―-]{1}(?=\s)")
MAX_PDFS = 700  # 決算期は1日数百件。3営業日分を見るので多めに


def _get(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def _nfkc(s):
    return unicodedata.normalize("NFKC", s).strip()


def fetch_list(day):
    """その日の開示一覧（全ページ）"""
    items = []
    for page in range(1, 30):
        try:
            body = _get(f"{LIST_URL}I_list_{page:03d}_{day:%Y%m%d}.html").decode("utf-8", "replace")
        except Exception:
            break
        rows = ROW.findall(body)
        for t, code, name, pdf, title in rows:
            items.append({"time": dt.datetime.combine(day, dt.time.fromisoformat(t)),
                          "code": code[:4] if len(code) == 5 else code,
                          "name": _nfkc(name), "title": _nfkc(title), "url": LIST_URL + pdf})
        if not rows or f"I_list_{page + 1:03d}_" not in body:
            break
    return items


def classify(title):
    if "説明資料" in title or "補足" in title or "訂正" in title:
        return None
    if "決算短信" in title:
        return "決算"
    if "上方修正" in title:
        return "上方修正"
    if "下方修正" in title:
        return "下方修正"
    if "業績予想" in title and "修正" in title:
        return "業績修正"
    if "増配" in title:
        return "増配"
    if "配当予想" in title and "修正" in title:
        return "配当修正"
    return None


def _to_num(tok):
    tok = tok.replace(",", "")
    if tok in ("－", "―", "-"):
        return None
    neg = tok.startswith("△") or tok.startswith("-")
    try:
        v = float(tok.lstrip("△-"))
    except ValueError:
        return None
    return -v if neg else v


def _pdf_text(url, pages=2):
    reader = PdfReader(io.BytesIO(_get(url, timeout=30)))
    return _nfkc("\n".join((p.extract_text() or "") for p in reader.pages[:pages]))


def _direction(text):
    """業績・配当予想の修正が上方か下方か"""
    if "上方修正" in text and "下方修正" not in text:
        return 1
    if "下方修正" in text and "上方修正" not in text:
        return -1
    signs = []
    for m in re.finditer(r"増減率", text):
        nums = [_to_num(t) for t in NUM.findall(text[m.end():m.end() + 120])]
        signs += [1 if v > 0 else -1 for v in nums if v not in (None, 0)][:4]
    if not signs:
        return 0
    s = sum(signs)
    return 1 if s > 0 else -1 if s < 0 else 0


def _results(text):
    """決算短信1ページ目の経営成績：(売上高の前年比%, 営業利益の前年比%)"""
    start = text.find("経営成績")
    for line in text[start:start + 1500].splitlines():
        # 行頭の「2027年2月期第2四半期」などの期の表記や日付を除いてから数字を読む
        line = re.sub(r"\d{1,4}\s*年\s*\d{1,2}\s*月\s*(期|\d{1,2}\s*日)\s*(第\s*\d\s*四半期|中間期)?(\s*\(累計\))?", " ", line)
        line = re.sub(r"△\s+", "△", line)  # 「△ 129」→「△129」
        toks = NUM.findall(line + " ")
        if len(toks) >= 8:
            nums = [_to_num(t) for t in toks]
            return nums[1], nums[3]
    return None, None


def parse(item):
    kind = item["kind"]
    try:
        text = _pdf_text(item["url"])
    except Exception as e:
        return {"error": str(e)}
    out = {}
    if kind == "決算":
        out["sales_pct"], out["op_pct"] = _results(text)
        out["revised"] = bool(re.search(r"業績予想からの修正の有無\s*:?\s*有", text))
    elif kind in ("業績修正", "上方修正", "下方修正"):
        out["dir"] = {"上方修正": 1, "下方修正": -1}.get(kind) or _direction(text)
    elif kind in ("増配", "配当修正"):
        out["dir"] = 1 if kind == "増配" or "増配" in text else (-1 if "減配" in text else _direction(text))
    return out


def collect(since, until=None):
    """since（前の取引日の15:00）以降の開示から、会社ごとのサプライズ点数を出す"""
    until = until or dt.datetime.now()
    items = []
    day = since.date()
    while day <= until.date():
        items += [x for x in fetch_list(day) if since <= x["time"] <= until]
        day += dt.timedelta(days=1)
    # 「P-」は TOKYO PRO Market（プロ投資家向けで個人は売買できない）なので除く
    items = [x for x in items if not x["name"].startswith("P-")]
    for x in items:
        x["kind"] = classify(x["title"])
    targets = [x for x in items if x["kind"]][:MAX_PDFS]

    cache_path = BASE / "disclosures_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    todo = [x for x in targets if x["url"] not in cache]
    if todo:
        print(f"適時開示のPDF解析中… {len(todo)}件", file=sys.stderr)
        with ThreadPoolExecutor(4) as ex:
            for x, res in zip(todo, ex.map(parse, todo)):
                cache[x["url"]] = res
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    companies = {}
    for x in targets:
        res = cache.get(x["url"], {})
        c = companies.setdefault(x["code"], {"code": x["code"], "name": x["name"], "time": x["time"],
                                             "points": 0.0, "tags": [], "docs": [],
                                             "op_pct": None, "sales_pct": None})
        c["docs"].append((x["title"], x["url"]))
        c["time"] = min(c["time"], x["time"])
        kind = x["kind"]
        if kind == "決算":
            op, sales = res.get("op_pct"), res.get("sales_pct")
            c["op_pct"], c["sales_pct"] = op, sales
            if op is not None:
                if op >= 30:
                    c["points"] += 2
                    c["tags"].append(f"営業益 +{op:.0f}%")
                elif op >= 10:
                    c["points"] += 1
                    c["tags"].append(f"営業益 +{op:.0f}%")
                elif op < 0:
                    c["points"] -= 1
                    c["tags"].append(f"営業益 {op:.0f}%")
            if sales is not None and sales >= 10:
                c["points"] += 0.5
        elif kind in ("業績修正", "上方修正", "下方修正"):
            d = res.get("dir", 0)
            if d > 0 and "上方修正" not in c["tags"]:
                c["points"] += 3
                c["tags"].append("上方修正")
            elif d < 0 and "下方修正" not in c["tags"]:
                c["points"] -= 3
                c["tags"].append("下方修正")
            elif d == 0:
                c["tags"].append("業績予想の修正（方向は要確認）")
        elif kind in ("増配", "配当修正"):
            d = res.get("dir", 0)
            if d > 0 and "増配" not in c["tags"]:
                c["points"] += 2
                c["tags"].append("増配")
            elif d < 0:
                c["points"] -= 2
                c["tags"].append("減配")
    # TOBの対象になった会社は、株価がTOB価格に張り付きやすく上値が限られるので印を付ける
    tob = {x["code"] for x in items if "公開買付" in x["title"] and ("賛同" in x["title"] or "意見表明" in x["title"])}
    for c in companies.values():
        c["tob"] = c["code"] in tob
        if c["tob"]:
            c["tags"].append("TOB対象")
    return sorted(companies.values(), key=lambda c: (-c["points"], -(c["op_pct"] or -999)))
