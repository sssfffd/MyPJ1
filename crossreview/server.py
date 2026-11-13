"""UI: 표준 라이브러리 http.server 로 띄우는 로컬 전용(127.0.0.1) 화면."""

from __future__ import print_function

import binascii
import datetime
import json
import os
import shutil
import string
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from .gitcmd import GitError, Repo
from .resolve import ResolveError
from .collect import DiffOptions
from .bundle import build_side, build_bundle
from .export import render_html, write_html, save_run, save_llm  # noqa: F401 (write_html 재노출)
from . import llm

MAX_BODY = 256 << 20
DEFAULT_OUT = "crossreview_out"


# ---------------------------------------------------------------- 요청 처리 (서버와 무관한 순수 함수)

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


def list_refs(repo_path, rev=None, git="git"):
    """브랜치/태그/최근 커밋 목록 (UI 의 선택 상자용)."""
    repo = Repo(repo_path or ".", git=git)

    def refs(ns):
        return [r for r in repo.lines(["for-each-ref", "--sort=-committerdate",
                                       "--format=%(refname:short)", ns])
                if not r.endswith("/HEAD") and r != "origin"]
    code, out, _ = repo.run_raw(["symbolic-ref", "--quiet", "--short", "HEAD"])
    target = rev or "HEAD"
    if not repo.ok(["rev-parse", "--verify", "--quiet", target + "^{commit}"]):
        target = "HEAD"
    commits = []
    code2, log, _ = repo.run_raw(["log", "-n", "100", "--format=%h%x1f%p%x1f%s%x1f%an%x1f%ad",
                                  "--date=short", target])
    if code2 == 0:
        for line in log.decode("utf-8", "replace").splitlines():
            p = (line.split("\x1f") + [""] * 5)[:5]
            commits.append({"sha": p[0], "merge": len(p[1].split()) > 1, "subject": p[2],
                            "author": p[3], "date": p[4]})
    return {"repo": repo.path, "current": out.decode().strip() if code == 0 else None,
            "branches": refs("refs/heads"), "remotes": refs("refs/remotes"),
            "tags": refs("refs/tags")[:200], "commits_of": target, "commits": commits}


def browse(path):
    """폴더 선택 창용 디렉터리 목록."""
    if not path:
        path = os.getcwd()
    if path == "::drives":
        drives = ["%s:\\" % d for d in string.ascii_uppercase if os.path.isdir("%s:\\" % d)]
        return {"path": "", "parent": None, "is_repo": False,
                "dirs": [{"name": d, "path": d, "repo": False} for d in drives]}
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        raise ValueError("폴더가 없습니다: %s" % path)
    parent = os.path.dirname(path)
    if parent == path:                               # 루트
        parent = "::drives" if os.name == "nt" else None
    dirs = []
    try:
        names = sorted(os.listdir(path), key=lambda n: n.lower())
    except OSError as e:
        raise ValueError("폴더를 열 수 없습니다: %s" % e)
    for n in names:
        full = os.path.join(path, n)
        if n.startswith(".") or n.startswith("$") or not os.path.isdir(full):
            continue
        dirs.append({"name": n, "path": full, "repo": os.path.exists(os.path.join(full, ".git"))})
    return {"path": path, "parent": parent, "dirs": dirs,
            "is_repo": os.path.exists(os.path.join(path, ".git"))}


def _stamp():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


# ---------------------------------------------------------------- HTTP

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
        sv = self.server
        page = render_html(sv.bundle, sv.token, {"path": llm.find_hook(sv.hook), "out": sv.out_base})
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Token") != self.server.token:
            return self._json(403, {"error": "forbidden"})
        fn = getattr(self, "api_" + self.path[len("/api/"):].replace("/", "_"), None) \
            if self.path.startswith("/api/") else None
        if fn is None:
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._json(413, {"error": "요청이 너무 큽니다."})
            req = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            self._json(200, fn(req))
        except (GitError, ResolveError, RuntimeError, ValueError, OSError) as e:
            self._json(200, {"error": str(e)})
        except Exception as e:           # 훅 오류 등: 화면에 원인을 보여준다
            self._json(200, {"error": "%s: %s" % (type(e).__name__, e)})

    # --- API
    def api_refs(self, req):
        return list_refs(req.get("repo"), req.get("rev"), git=self.server.git)

    def api_browse(self, req):
        return browse(req.get("path"))

    def api_run(self, req):
        sv = self.server
        bundle = run_request(req, git=sv.git)
        if req.get("out_dir"):
            sv.out_base = req["out_dir"]
        with sv.lock:
            sv.bundle = bundle
            sv.out_dir = os.path.join(sv.out_base, _stamp())
            bundle["saved_to"] = save_run(bundle, sv.out_dir)
        return bundle

    def api_load(self, req):
        """'JSON 열기' 로 불러온 bundle 을 서버에도 알려 LLM 분석/저장이 가능하게 한다."""
        sv = self.server
        if not all(k in req for k in ("pairs", "source", "target")):
            raise ValueError("crossreview bundle 형식이 아닙니다.")
        with sv.lock:
            sv.bundle = req
            saved = req.get("saved_to")
            sv.out_dir = saved if saved and os.path.isdir(saved) else None
        return {"ok": True}

    def api_llm(self, req):
        sv = self.server
        bundle = sv.bundle
        if bundle is None:
            raise ValueError("먼저 diff 를 추출하세요.")
        idx = int(req.get("index", -1))
        if not 0 <= idx < len(bundle["pairs"]):
            raise ValueError("잘못된 파일 번호입니다.")
        request = llm.build_request(bundle, idx, req.get("extra_prompt"))
        hook = llm.find_hook(sv.hook)
        if hook is None:
            return {"no_hook": True, "prompt": request["prompt"]}
        text = llm.call_hook(hook, request)
        rec = {"result": text, "extra_prompt": request["extra_prompt"], "hook": hook,
               "at": datetime.datetime.now().replace(microsecond=0).isoformat()}
        with sv.lock:
            if sv.out_dir is None:
                sv.out_dir = os.path.join(sv.out_base, _stamp())
                bundle["saved_to"] = save_run(bundle, sv.out_dir)
            bundle.setdefault("llm_results", {})[str(idx)] = rec
            rec["saved_to"] = save_llm(bundle, sv.out_dir, idx, rec)
            save_llm(bundle, sv.out_dir, idx, rec)       # saved_to 를 포함해 다시 기록
        return rec


def _open_window(url, as_tab):
    """가능하면 Edge/Chrome 의 앱 창(주소창 없는 독립 창)으로, 안 되면 기본 브라우저 탭으로 연다."""
    if not as_tab:
        cands = [shutil.which(n) for n in ("msedge", "chrome", "google-chrome", "chromium",
                                           "chromium-browser")]
        for env in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData"):
            root = os.environ.get(env)
            if root:
                cands += [os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"),
                          os.path.join(root, "Google", "Chrome", "Application", "chrome.exe")]
        for exe in cands:
            if exe and os.path.isfile(exe):
                try:
                    subprocess.Popen([exe, "--app=" + url, "--window-size=1500,950"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                    return
                except OSError:
                    pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


def serve(port=8765, open_browser=True, initial_bundle=None, git="git", hook=None,
          out_base=None, as_tab=False):
    try:
        httpd = _Server(("127.0.0.1", port), _Handler)
    except OSError:
        httpd = _Server(("127.0.0.1", 0), _Handler)      # 포트 사용 중이면 빈 포트
    httpd.token = binascii.hexlify(os.urandom(16)).decode()
    httpd.lock = threading.Lock()
    httpd.git = git
    httpd.hook = hook
    httpd.out_base = os.path.abspath(out_base or DEFAULT_OUT)
    httpd.bundle = initial_bundle
    saved = (initial_bundle or {}).get("saved_to")
    httpd.out_dir = saved if saved and os.path.isdir(saved) else None
    url = "http://127.0.0.1:%d/" % httpd.server_address[1]
    print("crossreview UI: %s   (종료: Ctrl+C 또는 이 창 닫기)" % url, file=sys.stderr)
    print("LLM 훅: %s" % (llm.find_hook(hook) or "없음 (llm_hook.py 를 만들면 연결됩니다)"), file=sys.stderr)
    sys.stderr.flush()
    if open_browser:
        _open_window(url, as_tab)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
