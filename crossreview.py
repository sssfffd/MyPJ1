# -*- coding: utf-8 -*-
"""
crossreview - 횡전개 코드 리뷰 도구 (내부 로직 전부가 이 파일 하나에 있다)

    python crossreview.py          실행 (브라우저 화면이 자동으로 열림)
    python crossreview.py build    실행 파일 빌드 -> crossreview.pyz
                                   (PyInstaller 가 설치된 PC 면 dist/crossreview.exe 도 함께)

    python crossreview.pyz         빌드된 실행 파일 실행 (Windows 는 더블클릭)

화면은 같은 폴더의 ui.html 이다 (빌드하면 실행 파일 안에 포함). 표준 라이브러리와 git 만 사용한다.

비교 방식
    원본 레포의 [기준 브랜치 ~ 작업 브랜치] 변경과 대상 레포의 [기준 브랜치 ~ 작업 브랜치] 변경을
    파일 단위로 짝지어 나란히 보여준다.
    변경 = git diff 기준...작업  (기준 브랜치에서 갈라진 지점부터 작업 브랜치까지의 전체 변경.
           작업 브랜치에 커밋이 여러 개여도 PR 전체가 한 번에 잡힌다)

코드 구성 (위에서부터)
    1. 설정
    2. LLM 연동          <- call_llm() 을 사내 LLM 호출로 바꾸면 된다
    3. git / diff
    4. 원본-대상 비교
    5. 결과 저장
    6. 웹 서버 (화면이 호출하는 API)
    7. 실행 / 빌드
"""

import datetime
import difflib
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import webbrowser
import zipapp
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# =============================================================================
# 1. 설정
# =============================================================================

HOST = "0.0.0.0"     # "0.0.0.0": 다른 PC 에서 http://이PC의IP:포트/ 로 접속 가능 / "127.0.0.1": 이 PC 만
PORT = 8765          # 사용 중이면 다음 번호를 자동으로 쓴다
OPEN_BROWSER = True  # 실행 시 브라우저 자동 열기

# 실행 파일(crossreview.pyz / .exe) 또는 이 스크립트가 있는 폴더. settings.json 과 결과 폴더가 여기에 생긴다.
_HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
PYZ = _HERE if os.path.isfile(_HERE) else None      # crossreview.pyz 안에서 실행 중이면 그 파일 경로
APP_DIR = os.path.dirname(PYZ) if PYZ else _HERE
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")

# 화면의 ⚙ 설정에서 바꿀 수 있는 값 (settings.json 에 저장, 이 PC 에서 접속했을 때만 변경 가능)
DEFAULT_SETTINGS = {
    "repo_roots": [os.path.expanduser("~")],          # 이 폴더들 아래의 git 레포를 목록에 보여준다
    "output_dir": os.path.join(APP_DIR, "crossreview_out"),
}

DIFF_CONTEXT = 3                     # diff 앞뒤로 보여줄 줄 수
SOURCE_ENCODINGS = ("utf-8", "cp949")  # 소스 파일 인코딩 후보 (순서대로 시도)


# =============================================================================
# 2. LLM 연동
# =============================================================================

# 화면의 "프롬프트" 칸 기본값. 화면에서 고친 내용은 브라우저에 저장되고 요청마다 함께 넘어온다.
# {file} {source_diff} {target_diff} {extra} 자리에 값이 채워진다.
DEFAULT_PROMPT = u"""\
아래는 동일한 기능을 다른 버전/레포/브랜치에 횡전개한 코드 변경입니다.
[원본] 은 기능을 처음 개발한 쪽의 변경이고, [대상] 은 횡전개한 쪽의 변경입니다.

두 변경이 동일하게 개발되었는지 검토해 주세요.
- 원본에는 있는데 대상에서 누락된 변경
- 다르게 구현된 부분
- 대상에만 있는 추가 변경
을 라인 기준으로 지적하고, 버전 차이로 인한 정당한 차이로 보이면 그렇게 표시해 주세요.

파일: {file}

[원본]
{source_diff}

[대상]
{target_diff}

{extra}"""


def build_prompt(template, pair, source_file, target_file, extra):
    """화면에서 받은 프롬프트 템플릿에 diff 를 채워 넣는다."""
    values = {
        "file": pair["source_path"] or pair["target_path"],
        "source_diff": patch_text(source_file) or u"(원본에는 이 파일의 변경이 없음)",
        "target_diff": patch_text(target_file) or u"(대상에는 이 파일의 변경이 없음 - 이식 누락 가능성)",
        "extra": (u"[추가 요청]\n" + extra.strip()) if extra and extra.strip() else u"",
    }
    text = template or DEFAULT_PROMPT
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text.strip() + u"\n"


def call_llm(prompt):
    """★ 사내 LLM 호출을 여기에 구현한다. 프롬프트 문자열을 받아 응답 문자열을 돌려주면 된다.

    예) 표준 라이브러리로 HTTP API 호출

        import urllib.request
        body = json.dumps({"model": "...", "messages": [{"role": "user", "content": prompt}]})
        req = urllib.request.Request("http://llm.internal/v1/chat/completions", body.encode("utf-8"),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]

    여기서 예외를 던지면 화면에 오류 메시지로 표시된다.
    """
    return (u"(LLM 이 아직 연결되지 않았습니다. crossreview.py 의 call_llm() 을 구현하세요.)\n\n"
            u"전송될 프롬프트 길이: %d자 - 화면의 '보낸 프롬프트' 에서 내용을 확인할 수 있습니다." % len(prompt))


# =============================================================================
# 3. git / diff
# =============================================================================

class UserError(Exception):
    """화면에 그대로 보여줄 오류."""


def git(repo, *args):
    """git 명령 실행 -> stdout(bytes). 실패하면 UserError."""
    cmd = ["git", "-C", repo, "-c", "core.quotepath=false", "-c", "color.ui=false"] + list(args)
    env = dict(os.environ, LC_ALL="C", GIT_OPTIONAL_LOCKS="0", GIT_PAGER="cat")
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        raise UserError(u"git 을 실행할 수 없습니다. git 이 설치되어 있는지 확인하세요.")
    if p.returncode != 0:
        raise UserError(u"git %s 실패: %s" % (" ".join(args[:2]), p.stderr.decode("utf-8", "replace").strip()))
    return p.stdout


def git_lines(repo, *args):
    out = git(repo, *args).decode("utf-8", "replace").strip()
    return out.split("\n") if out else []


def git_ok(repo, *args):
    try:
        git(repo, *args)
        return True
    except UserError:
        return False


def find_repos(roots, max_depth=3, max_dirs=5000):
    """roots 아래의 git 레포 목록. 레포를 찾으면 그 안으로는 더 내려가지 않는다."""
    skip = {"node_modules", "__pycache__", "venv", ".venv", "AppData", "Library",
            "Windows", "Program Files", "Program Files (x86)"}
    found, budget = [], [max_dirs]

    def walk(path, depth):
        if budget[0] <= 0:
            return
        budget[0] -= 1
        if os.path.exists(os.path.join(path, ".git")):
            found.append(path)
            return
        if depth >= max_depth:
            return
        try:
            names = sorted(os.listdir(path), key=str.lower)
        except OSError:
            return
        for name in names:
            full = os.path.join(path, name)
            if name[:1] in ".$" or name in skip or not os.path.isdir(full) or os.path.islink(full):
                continue
            walk(full, depth + 1)

    for root in roots:
        if os.path.isdir(root):
            walk(os.path.abspath(root), 0)
    return found


def list_branches(repo):
    """브랜치 목록 + 자동 추천. 추천 규칙:
        기준 = origin/HEAD 가 가리키는 브랜치 > origin/main > origin/master > main > master > develop
        작업 = 현재 브랜치. 단 현재 브랜치가 이미 기준에 다 들어가 있으면(main 을 체크아웃해 둔 경우 등)
               기준에 아직 머지되지 않은 가장 최근 브랜치
    """
    def refs(namespace):
        return [r for r in git_lines(repo, "for-each-ref", "--sort=-committerdate",
                                     "--format=%(refname:short)", namespace)
                if not r.endswith("/HEAD") and r != "origin"]

    remotes, locals_ = refs("refs/remotes"), refs("refs/heads")
    current = git_lines(repo, "branch", "--show-current")
    current = current[0] if current else None

    base = None
    try:
        base = git_lines(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")[0]
    except (UserError, IndexError):
        for cand in ("origin/main", "origin/master", "main", "master", "develop"):
            if cand in remotes or cand in locals_:
                base = cand
                break

    head, reason = current, u"현재 체크아웃된 브랜치"
    if base and (not current or git_ok(repo, "merge-base", "--is-ancestor", current, base)):
        candidates = git_lines(repo, "for-each-ref", "--sort=-committerdate", "--no-merged=" + base,
                               "--format=%(refname:short)", "refs/heads", "refs/remotes")
        candidates = [c for c in candidates if not c.endswith("/HEAD")]
        if candidates:
            head, reason = candidates[0], u"%s 에 아직 머지되지 않은 가장 최근 브랜치" % base
    return {"local": locals_, "remote": remotes, "current": current,
            "suggest": {"head": head, "base": base, "reason": reason}}


def _decode(data):
    for enc in SOURCE_ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", "replace")


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


def _parse_file(chunk):
    """'diff --git' 하나 분량 -> 파일 dict."""
    lines = _decode(chunk).split("\n")
    m = re.match(r"^diff --git a/(.*) b/(.*)$", lines[0])
    old_path = new_path = m.group(2) if m else lines[0]
    status, binary, hunks, cur = "modified", False, [], None
    for line in lines[1:]:
        line = line.rstrip("\r")
        h = HUNK_RE.match(line)
        if h:
            cur = {"old_start": int(h.group(1)), "new_start": int(h.group(3)),
                   "header": line, "lines": []}
            hunks.append(cur)
        elif cur is not None:
            if line[:1] in (" ", "+", "-", "\\"):
                cur["lines"].append(line)
        elif line.startswith("new file mode"):
            status = "added"
        elif line.startswith("deleted file mode"):
            status = "deleted"
        elif line.startswith("rename from "):
            old_path, status = line[12:], "renamed"
        elif line.startswith("rename to "):
            new_path = line[10:]
        elif line.startswith("--- a/"):
            old_path = line[6:]
        elif line.startswith("+++ b/"):
            new_path = line[6:]
        elif line.startswith("Binary files"):
            binary = True
    if status == "deleted":
        new_path = old_path
    adds = sum(1 for h in hunks for l in h["lines"] if l[:1] == "+")
    dels = sum(1 for h in hunks for l in h["lines"] if l[:1] == "-")
    return {"path": new_path, "old_path": old_path if old_path != new_path else None,
            "status": status, "binary": binary, "additions": adds, "deletions": dels, "hunks": hunks}


def collect_diff(repo, base, head):
    """기준 브랜치에서 갈라진 지점 ~ 작업 브랜치 의 파일별 변경."""
    for name, value in ((u"기준 브랜치", base), (u"작업 브랜치", head)):
        if not value or value.startswith("-") or not git_ok(repo, "rev-parse", "--verify", "--quiet",
                                                            value + "^{commit}"):
            raise UserError(u"%s '%s' 를 %s 에서 찾을 수 없습니다." % (name, value or "", repo))
    if not git_lines(repo, "merge-base", base, head):
        raise UserError(u"'%s' 와 '%s' 의 공통 조상이 없습니다." % (base, head))
    raw = git(repo, "diff", "-M", "-U%d" % DIFF_CONTEXT, "--no-ext-diff", "--no-textconv",
              "--src-prefix=a/", "--dst-prefix=b/", base + "..." + head)
    files = [_parse_file(c) for c in re.split(br"(?m)^(?=diff --git )", raw) if c.strip()]
    commits = git_lines(repo, "log", "--format=%h %s (%an)", base + ".." + head)
    return {"repo": repo, "base": base, "head": head, "commits": commits, "files": files}


def patch_text(f):
    """파일 dict -> unified diff 텍스트 (프롬프트용)."""
    if not f:
        return u""
    out = [u"--- a/%s" % (f["old_path"] or f["path"]), u"+++ b/%s" % f["path"]]
    if f["binary"]:
        out.append(u"(binary file)")
    for h in f["hunks"]:
        out.append(h["header"])
        out += h["lines"]
    return u"\n".join(out)


# =============================================================================
# 4. 원본-대상 비교
# =============================================================================

def _changed_lines(f):
    """추가/삭제 라인만, 공백 정규화. 라인 번호와 컨텍스트는 무시한다."""
    return [l[0] + " ".join(l[1:].split()) for h in f["hunks"] for l in h["lines"]
            if l[:1] in "+-" and l[1:].strip()]


def pair_files(src_files, dst_files):
    """원본 파일과 대상 파일을 짝짓는다: 같은 경로 > 파일명이 양쪽에서 하나뿐이면 파일명."""
    dst_by_path = dict((f["path"], f) for f in dst_files)
    used, pairs = set(), []

    def make(s, d):
        same = s is not None and d is not None and _changed_lines(s) == _changed_lines(d)
        sim = 1.0 if same else (round(difflib.SequenceMatcher(None, _changed_lines(s), _changed_lines(d),
                                                              autojunk=False).ratio(), 3)
                                if s is not None and d is not None else 0.0)
        kind = "same" if same else ("diff" if s is not None and d is not None
                                    else ("source_only" if d is None else "target_only"))
        return {"kind": kind, "similarity": sim,
                "source_path": s["path"] if s else None, "target_path": d["path"] if d else None}

    unmatched = []
    for s in src_files:
        d = dst_by_path.get(s["path"])
        if d is not None:
            used.add(d["path"])
            pairs.append(make(s, d))
        else:
            unmatched.append(s)
    base = os.path.basename
    for s in unmatched:
        cands = [d for d in dst_files if d["path"] not in used and base(d["path"]) == base(s["path"])]
        same_name_src = [x for x in unmatched if base(x["path"]) == base(s["path"])]
        if len(cands) == 1 and len(same_name_src) == 1:
            used.add(cands[0]["path"])
            pairs.append(make(s, cands[0]))
        else:
            pairs.append(make(s, None))
    pairs += [make(None, d) for d in dst_files if d["path"] not in used]
    order = {"diff": 0, "source_only": 1, "target_only": 2, "same": 3}
    pairs.sort(key=lambda p: (order[p["kind"]], p["similarity"]))
    return pairs


def compare(source, target):
    src = collect_diff(source["repo"], source["base"], source["head"])
    dst = collect_diff(target["repo"], target["base"], target["head"])
    pairs = pair_files(src["files"], dst["files"])
    summary = dict((k, sum(1 for p in pairs if p["kind"] == k))
                   for k in ("same", "diff", "source_only", "target_only"))
    return {"source": src, "target": dst, "pairs": pairs, "summary": summary, "llm": {}}


# =============================================================================
# 5. 결과 저장
#    <output_dir>/<실행ID>/result.json   화면이 다시 열 때 쓰는 전체 데이터
#                         source.diff    원본 전체 diff
#                         target.diff    대상 전체 diff
#                         llm/NN_파일.md  LLM 결과 (프롬프트 포함)
# =============================================================================

_lock = threading.Lock()
RUN_ID_RE = re.compile(r"^[\w\-]+$")


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with io.open(SETTINGS_FILE, encoding="utf-8") as fp:
            s.update(json.load(fp))
    except (OSError, ValueError):
        pass
    return s


def save_settings(new):
    s = load_settings()
    roots = [r.strip() for r in new.get("repo_roots", []) if r.strip()]
    if roots:
        s["repo_roots"] = roots
    if new.get("output_dir", "").strip():
        s["output_dir"] = new["output_dir"].strip()
    _write(SETTINGS_FILE, json.dumps(s, ensure_ascii=False, indent=2))
    return s


def _write(path, text):
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(text)


def run_dir(run_id):
    if not RUN_ID_RE.match(run_id or ""):
        raise UserError(u"잘못된 결과 ID 입니다.")
    return os.path.join(load_settings()["output_dir"], run_id)


def save_result(result):
    name = lambda side: re.sub(r"[^\w\-]+", "_", os.path.basename(result[side]["repo"]))[:30]
    run_id = "%s_%s_vs_%s" % (datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
                              name("source"), name("target"))
    folder = run_dir(run_id)
    result["run_id"], result["folder"] = run_id, folder
    with _lock:
        for side, fname in (("source", "source.diff"), ("target", "target.diff")):
            _write(os.path.join(folder, fname),
                   u"\n".join(patch_text(f) for f in result[side]["files"]) + u"\n")
        _write(os.path.join(folder, "result.json"), json.dumps(result, ensure_ascii=False))
    return result


def load_result(run_id):
    path = os.path.join(run_dir(run_id), "result.json")
    if not os.path.isfile(path):
        raise UserError(u"저장된 결과를 찾을 수 없습니다: %s" % run_id)
    with io.open(path, encoding="utf-8") as fp:
        return json.load(fp)


def save_llm(run_id, index, record):
    with _lock:
        result = load_result(run_id)
        pair = result["pairs"][index]
        fname = "%02d_%s.md" % (index + 1, re.sub(r"[^\w.\-]+", "_",
                                                  os.path.basename(pair["source_path"] or pair["target_path"])))
        record["file"] = os.path.join(run_dir(run_id), "llm", fname)
        _write(record["file"], u"# %s\n\n- 시각: %s\n\n## 결과\n\n%s\n\n## 보낸 프롬프트\n\n```\n%s\n```\n" % (
            pair["source_path"] or pair["target_path"], record["at"], record["answer"], record["prompt"]))
        result["llm"][str(index)] = record
        _write(os.path.join(run_dir(run_id), "result.json"), json.dumps(result, ensure_ascii=False))
    return record


def list_runs(limit=50):
    folder = load_settings()["output_dir"]
    if not os.path.isdir(folder):
        return []
    names = [n for n in os.listdir(folder)
             if RUN_ID_RE.match(n) and os.path.isfile(os.path.join(folder, n, "result.json"))]
    return sorted(names, reverse=True)[:limit]


# =============================================================================
# 6. 웹 서버 - 화면(ui.html)이 호출하는 API
#    모든 API 는 POST /api/<이름>, 요청/응답 모두 JSON. 오류는 {"error": "메시지"}
# =============================================================================

def api_init(req, local):
    s = load_settings()
    return {"repos": find_repos(s["repo_roots"]), "settings": s, "can_edit_settings": local,
            "runs": list_runs(), "default_prompt": DEFAULT_PROMPT}


def api_branches(req, local):
    return list_branches(_allowed_repo(req.get("repo")))


def api_compare(req, local):
    for side in ("source", "target"):
        req[side]["repo"] = _allowed_repo(req[side].get("repo"))
    return save_result(compare(req["source"], req["target"]))


def api_llm(req, local):
    result = load_result(req.get("run_id"))
    index = int(req.get("index", -1))
    if not 0 <= index < len(result["pairs"]):
        raise UserError(u"잘못된 파일 번호입니다.")
    pair = result["pairs"][index]
    find = lambda side, path: next((f for f in result[side]["files"] if f["path"] == path), None)
    prompt = build_prompt(req.get("prompt"), pair, find("source", pair["source_path"]),
                          find("target", pair["target_path"]), req.get("extra", u""))
    answer = call_llm(prompt)
    record = {"answer": answer, "prompt": prompt,
              "at": datetime.datetime.now().replace(microsecond=0).isoformat(" ")}
    return save_llm(result["run_id"], index, record)


def api_result(req, local):
    return load_result(req.get("run_id"))


def api_settings(req, local):
    if not local:
        raise UserError(u"설정은 서버가 실행 중인 PC 에서만 바꿀 수 있습니다.")
    return save_settings(req)


API = {"init": api_init, "branches": api_branches, "compare": api_compare, "llm": api_llm,
       "result": api_result, "settings": api_settings}


def _allowed_repo(path):
    """설정의 레포 폴더 아래에 있는 git 레포만 허용한다 (다른 PC 에서 아무 폴더나 읽지 못하게)."""
    real = os.path.realpath(path or "")
    for root in load_settings()["repo_roots"]:
        root = os.path.realpath(root)
        if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
            if not os.path.exists(os.path.join(real, ".git")):
                raise UserError(u"git 레포가 아닙니다: %s" % path)
            return real
    raise UserError(u"설정된 레포 폴더 밖의 경로입니다: %s" % path)


class Handler(BaseHTTPRequestHandler):
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

    def _host_ok(self):
        # 다른 웹사이트가 이 서버를 몰래 호출하지 못하게, IP / localhost / 이 PC 이름으로 접속한 경우만 허용
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host in ("localhost", socket.gethostname().lower()):
            return True
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return False

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        if self.path.split("?")[0] != "/":
            return self._send(404, b"not found", "text/plain")
        try:
            self._send(200, read_ui(), "text/html; charset=utf-8")
        except (OSError, KeyError):
            self._send(500, u"ui.html 을 찾을 수 없습니다.".encode("utf-8"), "text/plain; charset=utf-8")

    def do_POST(self):
        # 화면(ui.html)만 붙이는 헤더. 다른 사이트의 폼 전송 등은 이 헤더를 붙일 수 없다.
        if not self._host_ok() or self.headers.get("X-Crossreview") != "1":
            return self._send(403, b"forbidden", "text/plain")
        fn = API.get(self.path[len("/api/"):]) if self.path.startswith("/api/") else None
        if fn is None:
            return self._send(404, b"not found", "text/plain")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            local = self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
            res = fn(req, local)
        except UserError as e:
            res = {"error": str(e)}
        except Exception as e:                       # call_llm 오류 등도 화면에 보여준다
            res = {"error": u"%s: %s" % (type(e).__name__, e)}
        self._send(200, json.dumps(res, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")


# =============================================================================
# 7. 실행 / 빌드
# =============================================================================

def lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))          # 실제로 패킷을 보내지 않고 나가는 인터페이스만 확인
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        ips += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    return [ip for i, ip in enumerate(ips) if ip not in ips[:i] and not ip.startswith("127.")]


def serve():
    server = None
    for port in range(PORT, PORT + 20):
        try:
            server = ThreadingHTTPServer((HOST, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        print(u"포트 %d~%d 를 모두 열 수 없습니다." % (PORT, PORT + 19))
        return 1
    port = server.server_address[1]
    print(u"crossreview 실행 중   (종료: Ctrl+C 또는 이 창 닫기)")
    print(u"  이 PC      : http://127.0.0.1:%d/" % port)
    if HOST != "127.0.0.1":
        for ip in lan_ips():
            print(u"  다른 PC    : http://%s:%d/" % (ip, port))
    if OPEN_BROWSER:
        threading.Timer(0.5, webbrowser.open, ["http://127.0.0.1:%d/" % port]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
    return 0


def read_ui():
    """ui.html 내용. 스크립트 옆 / crossreview.pyz 내부 / exe 내부 순으로 찾는다."""
    if PYZ:
        with zipfile.ZipFile(PYZ) as z:
            return z.read("ui.html")
    with open(os.path.join(getattr(sys, "_MEIPASS", APP_DIR), "ui.html"), "rb") as fp:
        return fp.read()


def build():
    """실행 파일 빌드.
        crossreview.pyz       항상 만든다 (표준 라이브러리 zipapp). Python 이 있는 PC 면 어디서든 실행된다.
        dist/crossreview.exe  PyInstaller 가 설치돼 있으면 함께 만든다 (Python 없는 PC 용, 빌드한 OS 용으로 나온다).
    """
    here = os.path.dirname(os.path.abspath(__file__))
    target = os.path.join(here, "crossreview.pyz")
    stage = tempfile.mkdtemp(prefix="crossreview-build-")
    try:
        for name in ("crossreview.py", "ui.html"):
            shutil.copy(os.path.join(here, name), os.path.join(stage, name))
        with open(os.path.join(stage, "__main__.py"), "w") as fp:
            fp.write("import sys\nimport crossreview\nsys.exit(crossreview.main())\n")
        zipapp.create_archive(stage, target, interpreter="/usr/bin/env python3", compressed=True)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    print(u"완료: %s" % target)

    try:
        import PyInstaller.__main__ as pyinstaller
    except ImportError:
        print(u"(PyInstaller 가 없어 exe 는 건너뜀. 필요하면 빌드하는 PC 에서: pip install pyinstaller)")
        return 0
    pyinstaller.run([os.path.join(here, "crossreview.py"), "--onefile", "--name", "crossreview",
                     "--add-data", os.path.join(here, "ui.html") + os.pathsep + ".",
                     "--distpath", os.path.join(here, "dist"),
                     "--workpath", os.path.join(here, "build"),
                     "--specpath", os.path.join(here, "build"),
                     "--noconfirm", "--clean"])
    print(u"완료: %s" % os.path.join(here, "dist", "crossreview" + (".exe" if os.name == "nt" else "")))
    return 0


def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    return build() if sys.argv[1:] == ["build"] else serve()


if __name__ == "__main__":
    sys.exit(main())
