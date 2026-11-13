"""UI: 표준 라이브러리 http.server 로 띄우는 로컬 전용(127.0.0.1) 화면."""

from __future__ import print_function

import base64
import binascii
import collections
import datetime
import hmac
import io
import json
import os
import re
import shutil
import socket
import ssl
import string
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

from .gitcmd import GitError, Repo
from .resolve import ResolveError
from .collect import DiffOptions
from .bundle import build_side, build_bundle
from .export import render_html, write_html, save_run, save_llm, unit_name  # noqa: F401
from . import llm

MAX_BODY = 256 << 20
DEFAULT_OUT = "crossreview_out"
RUN_ID_RE = re.compile(r"^[0-9A-Za-z_\-]{1,64}$")


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
    if target.startswith("-") or not repo.ok(["rev-parse", "--verify", "--quiet", target + "^{commit}"]):
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


def _new_run_id():
    return "%s_%s" % (datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
                      binascii.hexlify(os.urandom(2)).decode())


class Runs(object):
    """추출 결과(run) 저장소. run 하나 = out_base/<run_id>/ 폴더 하나.

    서버에 전역 '현재 결과' 를 두지 않고 run_id 로 구분하므로 여러 사람이 동시에 써도 섞이지 않는다.
    """
    KEEP = 16

    def __init__(self, out_base):
        self.out_base = os.path.abspath(out_base or DEFAULT_OUT)
        self.lock = threading.Lock()
        self.cache = collections.OrderedDict()

    def dir(self, run_id):
        if not RUN_ID_RE.match(run_id or ""):
            raise ValueError("잘못된 실행 ID 입니다.")
        return os.path.join(self.out_base, run_id)

    def _remember(self, run_id, bundle):
        self.cache[run_id] = bundle
        self.cache.move_to_end(run_id)
        while len(self.cache) > self.KEEP:
            self.cache.popitem(last=False)

    def create(self, bundle):
        with self.lock:
            run_id = _new_run_id()
            bundle["run_id"] = run_id
            bundle["saved_to"] = os.path.join(self.out_base, run_id)
            save_run(bundle, bundle["saved_to"])
            self._remember(run_id, bundle)
        return bundle

    def get(self, run_id):
        path = os.path.join(self.dir(run_id), "bundle.json")
        with self.lock:
            if run_id in self.cache:
                return self.cache[run_id]
            if not os.path.isfile(path):
                raise ValueError("저장된 결과를 찾을 수 없습니다: %s" % run_id)
            with io.open(path, encoding="utf-8") as fp:
                bundle = json.load(fp)
            bundle["run_id"], bundle["saved_to"] = run_id, os.path.dirname(path)
            self._remember(run_id, bundle)
            return bundle

    def add_llm(self, run_id, index, rec):
        bundle = self.get(run_id)
        with self.lock:
            bundle.setdefault("llm_results", {})[str(index)] = rec
            rec["saved_to"] = os.path.join(self.dir(run_id), "llm",
                                           unit_name(index, bundle["pairs"][index]) + ".md")
            save_llm(bundle, self.dir(run_id), index, rec)
        return rec

    def list(self, limit=50):
        out = []
        if not os.path.isdir(self.out_base):
            return out
        for name in sorted(os.listdir(self.out_base), reverse=True):
            meta = os.path.join(self.out_base, name, "meta.json")
            if RUN_ID_RE.match(name) and os.path.isfile(meta):
                try:
                    with io.open(meta, encoding="utf-8") as fp:
                        m = json.load(fp)
                except (OSError, ValueError):
                    continue
                out.append({"id": name, "title": m.get("title", ""), "summary": m.get("summary"),
                            "llm_results": m.get("llm_results", 0)})
                if len(out) >= limit:
                    break
        return out


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    # --- 서버 모드: 접근 가능한 경로를 --repo-root 아래로 제한
    def check_path(self, path):
        if not self.roots:
            return path or "."
        real = os.path.realpath(os.path.join(self.roots[0], path or ""))
        for root in self.roots:
            if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
                return real
        raise ValueError("허용된 폴더(--repo-root) 밖의 경로입니다: %s" % path)


class _Handler(BaseHTTPRequestHandler):
    server_version = "crossreview"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _allowed(self):
        """True 면 통과. 아니면 응답을 보내고 False."""
        sv = self.server
        if sv.local:
            # 다른 사이트가 로컬 서버를 호출하지 못하도록 Host 를 확인한다 (DNS rebinding 방지)
            port = sv.server_address[1]
            if self.headers.get("Host") not in ("127.0.0.1:%d" % port, "localhost:%d" % port):
                self._send(403, b"forbidden", "text/plain")
                return False
        if sv.auth:
            got = self.headers.get("Authorization") or ""
            want = "Basic " + base64.b64encode(sv.auth.encode("utf-8")).decode()
            if not hmac.compare_digest(got.encode("utf-8"), want.encode("utf-8")):
                self._send(401, b"login required", "text/plain",
                           {"WWW-Authenticate": 'Basic realm="crossreview", charset="UTF-8"'})
                return False
        return True

    def do_GET(self):
        if not self._allowed():
            return
        url = urlparse(self.path)
        if url.path != "/":
            return self._send(404, b"not found", "text/plain")
        sv = self.server
        run_id = (parse_qs(url.query).get("run") or [sv.initial_run])[0]
        bundle = None
        if run_id:
            try:
                bundle = sv.runs.get(run_id)
            except ValueError:
                bundle = None
        cfg = {"path": llm.find_hook(sv.hook), "out": sv.runs.out_base, "server": not sv.local}
        self._send(200, render_html(bundle, sv.token, cfg).encode("utf-8"),
                   "text/html; charset=utf-8")

    def do_POST(self):
        if not self._allowed():
            return
        if self.headers.get("X-Token") != self.server.token:       # CSRF 방지
            return self._json(403, {"error": "페이지를 새로고침한 뒤 다시 시도하세요."})
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
        sv = self.server
        return list_refs(sv.check_path(req.get("repo")), req.get("rev"), git=sv.git)

    def api_browse(self, req):
        sv = self.server
        path = req.get("path")
        if not sv.roots:
            return browse(path)
        if path == "::roots" or (not path and len(sv.roots) > 1):
            return {"path": "", "parent": None, "is_repo": False,
                    "dirs": [{"name": r, "path": r, "repo": os.path.exists(os.path.join(r, ".git"))}
                             for r in sv.roots]}
        d = browse(sv.check_path(path))
        if d["path"] in sv.roots:                      # 허용 폴더 위로는 올라갈 수 없다
            d["parent"] = "::roots" if len(sv.roots) > 1 else None
        return d

    def api_run(self, req):
        sv = self.server
        for key in ("src", "dst"):
            side = req.get(key) or {}
            side["repo"] = sv.check_path(side.get("repo"))
            req[key] = side
        return sv.runs.create(run_request(req, git=sv.git))

    def api_load(self, req):
        """'JSON 열기' 로 불러온 bundle 을 새 run 으로 저장한다 (LLM 분석/공유 링크용)."""
        if not all(k in req for k in ("pairs", "source", "target")):
            raise ValueError("crossreview bundle 형식이 아닙니다.")
        b = self.server.runs.create(req)
        return {"run_id": b["run_id"], "saved_to": b["saved_to"]}

    def api_runs(self, req):
        return {"runs": self.server.runs.list()}

    def api_llm(self, req):
        sv = self.server
        bundle = sv.runs.get(req.get("run_id") or "")
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
        return sv.runs.add_llm(bundle["run_id"], idx, rec)


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


def lan_addresses():
    """이 PC 의 IP 주소 목록 (다른 PC 에서 접속할 때 쓸 주소)."""
    found = []
    try:                                   # 기본 경로로 나가는 인터페이스의 IP (실제 패킷은 보내지 않는다)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            found.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        found += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    out = []
    for ip in found:
        if ip not in out and not ip.startswith("127.") and ip != "0.0.0.0":
            out.append(ip)
    return out


def serve(port=8765, open_browser=True, initial_bundle=None, git="git", hook=None,
          out_base=None, as_tab=False, host="127.0.0.1", roots=None, auth=None,
          cert=None, key=None):
    """UI 서버 실행.

    host 가 127.0.0.1(기본)이면 내 PC 전용. 그 외(예: 0.0.0.0)면 팀이 접속하는 '웹 서버 모드'.
    """
    local = host in ("127.0.0.1", "localhost", "::1")
    roots = [os.path.realpath(r) for r in (roots or [])]
    if not local:
        if not roots:
            raise ValueError("웹 서버 모드(--host)에서는 --repo-root 로 레포가 있는 폴더를 지정해야 합니다.")
        for r in roots:
            if not os.path.isdir(r):
                raise ValueError("--repo-root 폴더가 없습니다: %s" % r)
        if auth and ":" not in auth:
            raise ValueError("--auth 형식은 사용자:비밀번호 입니다.")
    try:
        httpd = _Server((host, port), _Handler)
    except OSError:
        if not local:
            raise ValueError("포트 %d 를 열 수 없습니다." % port)
        httpd = _Server((host, 0), _Handler)             # 포트 사용 중이면 빈 포트
    scheme = "http"
    if cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    httpd.token = binascii.hexlify(os.urandom(16)).decode()
    httpd.local = local
    httpd.roots = roots
    httpd.auth = auth
    httpd.git = git
    httpd.hook = hook
    httpd.runs = Runs(out_base)
    httpd.initial_run = httpd.runs.create(initial_bundle)["run_id"] if initial_bundle else None

    err = sys.stderr
    port = httpd.server_address[1]
    if local:
        url = "%s://127.0.0.1:%d/" % (scheme, port)
        print("crossreview UI: %s   (종료: Ctrl+C 또는 이 창 닫기)" % url, file=err)
    else:
        hosts = lan_addresses() if host in ("0.0.0.0", "::") else [host]
        url = "%s://127.0.0.1:%d/" % (scheme, port)
        print("crossreview 웹 서버 실행 중 (종료: Ctrl+C 또는 이 창 닫기)", file=err)
        print("  이 PC 에서      : %s" % url, file=err)
        for h in hosts:
            print("  다른 PC 에서    : %s://%s:%d/" % (scheme, h, port), file=err)
        if not hosts:
            print("  IP 주소를 찾지 못했습니다. ipconfig(Windows) / ip addr(Linux) 로 확인하세요.", file=err)
        print("  접속이 안 되면 이 PC 방화벽에서 TCP %d 포트 인바운드를 허용하세요." % port, file=err)
    print("저장 폴더: %s" % httpd.runs.out_base, file=err)
    print("LLM 훅: %s" % (llm.find_hook(hook) or "없음 (llm_hook.py 를 만들면 연결됩니다)"), file=err)
    if not local:
        print("웹 서버 모드: 접근 가능 폴더 = %s" % ", ".join(roots), file=err)
        if not auth:
            print("WARNING: --auth 가 없어 이 주소에 접속 가능한 누구나 사용할 수 있습니다.", file=err)
        if not cert:
            print("WARNING: HTTPS(--cert/--key)가 아니므로 통신이 암호화되지 않습니다.", file=err)
    err.flush()
    if open_browser:
        _open_window(url, as_tab)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
