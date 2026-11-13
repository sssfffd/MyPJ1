"""UI: 표준 라이브러리 http.server 로 띄우는 로컬 전용(127.0.0.1) 화면 + 정적 HTML 내보내기."""

from __future__ import print_function

import binascii
import io
import json
import os
import pkgutil
import sys
import webbrowser

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn
except ImportError:                      # pragma: no cover (python2)
    raise

from .gitcmd import GitError
from .resolve import ResolveError
from .collect import DiffOptions
from .bundle import build_side, build_bundle

MAX_BODY = 1 << 20


def _js(obj):
    """<script> 안에 안전하게 넣을 수 있는 JSON."""
    return (json.dumps(obj, ensure_ascii=False)
            .replace("</", "<\\/").replace("<!--", "<\\!--")
            .replace(u"\u2028", "\\u2028").replace(u"\u2029", "\\u2029"))


def render_html(bundle=None, token=None):
    html = pkgutil.get_data(__package__, "ui.html").decode("utf-8")
    html = html.replace("/*__TOKEN__*/null", _js(token), 1)
    return html.replace("/*__BUNDLE__*/null", _js(bundle), 1)


def write_html(bundle, path):
    """서버 없이 브라우저로 바로 열 수 있는 단일 HTML 리포트."""
    with io.open(path, "w", encoding="utf-8") as fp:
        fp.write(render_html(bundle))


def run_request(req, git="git"):
    """UI 의 추출 요청(dict) -> bundle."""
    def side(key, label):
        s = req.get(key) or {}
        opts = DiffOptions(context=int(req.get("context", 3)),
                           function_context=bool(req.get("function_context")),
                           ignore_space=bool(req.get("ignore_space")),
                           pathspecs=s.get("pathspecs"))
        return build_side(s.get("repo") or ".", label, head=s.get("head"), base=s.get("base"),
                          commits=s.get("commits"), ranges=s.get("ranges"), grep=s.get("grep"),
                          opts=opts, git=git)
    maps = []
    for m in req.get("path_maps") or []:
        if "=" not in m:
            raise ResolveError("경로 매핑 형식은 SRC=DST 입니다: %s" % m)
        maps.append(tuple(m.split("=", 1)))
    return build_bundle(side("src", "source"), side("dst", "target"), maps)


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class _Handler(BaseHTTPRequestHandler):
    server_version = "crossreview"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _host_ok(self):
        # 다른 사이트가 로컬 서버를 호출하지 못하도록 Host 를 확인한다 (DNS rebinding 방지)
        port = self.server.server_address[1]
        return self.headers.get("Host") in ("127.0.0.1:%d" % port, "localhost:%d" % port)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        if self.path.split("?")[0] != "/":
            return self._send(404, b"not found", "text/plain")
        page = render_html(self.server.initial_bundle, self.server.token)
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Token") != self.server.token:
            return self._json(403, {"error": "forbidden"})
        if self.path != "/api/run":
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._json(413, {"error": "요청이 너무 큽니다."})
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            self._json(200, run_request(req, git=self.server.git))
        except (GitError, ResolveError, RuntimeError, ValueError) as e:
            self._json(200, {"error": str(e)})
        except Exception as e:           # 화면에 원인을 보여준다
            self._json(200, {"error": "내부 오류: %r" % (e,)})


def serve(port=8765, open_browser=True, initial_bundle=None, git="git"):
    try:
        httpd = _Server(("127.0.0.1", port), _Handler)
    except OSError:
        httpd = _Server(("127.0.0.1", 0), _Handler)      # 포트 사용 중이면 빈 포트
    httpd.token = binascii.hexlify(os.urandom(16)).decode()
    httpd.initial_bundle = initial_bundle
    httpd.git = git
    url = "http://127.0.0.1:%d/" % httpd.server_address[1]
    print("crossreview UI: %s   (종료: Ctrl+C)" % url, file=sys.stderr)
    sys.stderr.flush()
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
