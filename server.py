"""ダッシュボードを表示し、「銘柄診断」の実行ボタンを受け付けるローカルサーバー

  http://localhost:8765/              … report.html（ダッシュボード）
  http://localhost:8765/api/diagnose?codes=7974+6857  … 診断結果（JSON: {"html": ...}）

自分のパソコン（127.0.0.1）からだけ接続できる。この画面を閉じると診断ボタンは使えなくなる。
"""

import json
import os
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
PORT = 8765
URL = f"http://localhost:{PORT}/"
LOCK = threading.Lock()  # 診断は1件ずつ（yfinanceへの同時アクセスを避ける）


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path in ("/", "/report.html"):
            return self._send(200, (BASE / "report.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/diagnose":
            import diagnose
            codes = diagnose.parse_codes(urllib.parse.parse_qs(url.query).get("codes", [""])[0])
            if not codes:
                return self._send(400, json.dumps({"error": "銘柄コードを入力してください（例: 7974 6857 285A）"},
                                                  ensure_ascii=False), "application/json; charset=utf-8")
            print(f"診断: {' '.join(codes)}")
            try:
                with LOCK:
                    body = {"html": diagnose.render(diagnose.diagnose(codes))}
                return self._send(200, json.dumps(body, ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                traceback.print_exc()
                return self._send(500, json.dumps({"error": f"診断中にエラーが発生しました: {e}"}, ensure_ascii=False),
                                  "application/json; charset=utf-8")
        self._send(404, "not found", "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):  # アクセスログは出さない
        pass


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        # すでに起動している（前回の run.bat の画面が開いたまま）→ そのサーバーを開く
        print("サーバーはすでに起動しています。ブラウザで開きます。")
        webbrowser.open(URL)
        return
    print(f"ダッシュボード: {URL}")
    print("この画面を開いている間、「銘柄診断」タブの実行ボタンが使えます。終了するにはこの画面を閉じてください。")
    if not os.environ.get("NO_BROWSER"):
        webbrowser.open(URL)
    server.serve_forever()


if __name__ == "__main__":
    main()
