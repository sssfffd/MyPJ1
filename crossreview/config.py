# -*- coding: utf-8 -*-
"""실행 설정 파일 crossreview.ini.

인자 없이 실행(더블클릭)하면 이 파일대로 서버를 띄운다. 파일이 없으면 기본값으로 만든다.
위치: crossreview.pyz 와 같은 폴더 (python -m crossreview 로 실행하면 현재 폴더).
"""

import configparser
import io
import os
import sys

NAME = "crossreview.ini"
SECTION = "crossreview"

TEMPLATE = u"""\
; crossreview 실행 설정
; crossreview.pyz 를 실행(더블클릭 또는 python crossreview.pyz)하면 이 설정대로 웹 화면이 열립니다.
; 값을 바꾼 뒤 프로그램을 다시 실행하면 적용됩니다.

[crossreview]
; 다른 PC 에서 IP 주소로 접속 허용 (yes / no)
;   no 이면 이 PC 에서만 접속 가능
lan = yes

; 포트 (다른 PC 에서는 http://이PC의IP:포트/ 로 접속)
port = 8765

; 화면에서 선택할 수 있는 레포가 있는 폴더. 이 폴더 밖은 열람/추출 불가.
; 여러 개면 ; 로 구분 (예: D:\\repos ; E:\\work)
; 비워 두면 사용자 홈 폴더
repo_root =

; 접속 시 로그인 (사용자:비밀번호). 비워 두면 로그인 없음
auth =

; HTTPS 인증서 / 개인키 (PEM). 비워 두면 HTTP
cert =
key =

; 결과 저장 폴더 (비워 두면 이 폴더 아래 crossreview_out)
out_dir =

; LLM 훅 파일 (비워 두면 이 폴더의 llm_hook.py)
llm_hook =

; 실행할 때 이 PC 에서 브라우저를 자동으로 열기 (yes / no)
open_browser = yes
"""

DEFAULTS = {"lan": "yes", "port": "8765", "repo_root": "", "auth": "", "cert": "", "key": "",
            "out_dir": "", "llm_hook": "", "open_browser": "yes"}


def base_dir():
    """crossreview.pyz 가 있는 폴더. 소스에서 실행하면 현재 폴더."""
    arg0 = os.path.abspath(sys.argv[0] or "")
    if arg0.lower().endswith(".pyz") or os.path.isfile(os.path.join(arg0, "__main__.py")):
        return os.path.dirname(arg0) if os.path.isfile(arg0) else arg0
    return os.getcwd()


def load(path=None, create=True):
    """설정을 읽어 dict 로 돌려준다. 경로 값은 설정 파일 위치 기준으로 절대경로화한다."""
    path = path or os.path.join(base_dir(), NAME)
    created = False
    if not os.path.isfile(path) and create:
        try:
            with io.open(path, "w", encoding="utf-8", newline="") as fp:
                fp.write(TEMPLATE.replace("\n", os.linesep))
            created = True
        except OSError:
            pass
    cp = configparser.ConfigParser(defaults=DEFAULTS, inline_comment_prefixes=None)
    if os.path.isfile(path):
        with io.open(path, encoding="utf-8-sig") as fp:
            cp.read_file(fp)
    if not cp.has_section(SECTION):
        cp.add_section(SECTION)
    c = cp[SECTION]
    here = os.path.dirname(os.path.abspath(path))

    def p(v):
        v = (v or "").strip().strip('"')
        return os.path.normpath(os.path.join(here, os.path.expanduser(v))) if v else None

    try:
        port = int(c.get("port"))
    except ValueError:
        raise ValueError("%s: port 값이 숫자가 아닙니다: %s" % (path, c.get("port")))
    return {
        "path": os.path.abspath(path), "created": created,
        "lan": c.getboolean("lan"),
        "port": port,
        "repo_root": [p(r) for r in c.get("repo_root").split(";") if r.strip()],
        "auth": c.get("auth").strip() or None,
        "cert": p(c.get("cert")), "key": p(c.get("key")),
        "out_dir": p(c.get("out_dir")) or os.path.join(here, "crossreview_out"),
        "llm_hook": p(c.get("llm_hook")) or os.path.join(here, "llm_hook.py"),
        "open_browser": c.getboolean("open_browser"),
    }
