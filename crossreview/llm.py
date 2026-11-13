"""LLM 연동 지점. 실제 LLM 호출은 사용자가 작성하는 훅 파일(llm_hook.py)이 담당한다.

훅 파일 규약:
    def review(request):          # request: dict (build_request 참고)
        return "LLM 응답 텍스트"
"""

import os
import sys

from .bundle import file_patch_text

HOOK_NAME = "llm_hook.py"

DEFAULT_INSTRUCTION = u"""\
아래는 동일한 기능을 다른 버전/레포/브랜치에 횡전개한 코드 변경입니다.
[원본 diff] 는 기능을 처음 개발한 쪽의 변경이고, [대상 diff] 는 횡전개한 쪽의 변경입니다.
두 변경이 동일하게 개발되었는지 검토하고, 누락된 변경 / 다르게 구현된 부분 / 대상에만 있는 추가 변경을
파일 경로와 라인 기준으로 지적해 주세요. 버전 차이로 인한 정당한 차이로 보이면 그렇게 표시해 주세요."""


def _side_info(side):
    return {"label": side["label"], "repo": side["repo"], "mode": side["mode"],
            "composed": side["composed"], "notes": side["notes"], "warnings": side["warnings"],
            "segments": [{"how": s["how"], "commits": [c["subject"] for c in s["commits"]]}
                         for s in side["segments"]]}


def build_request(bundle, index, extra_prompt=""):
    """훅에 전달할 dict. 'prompt' 는 바로 쓸 수 있게 조립한 기본 프롬프트(써도 되고 안 써도 됨)."""
    pair = bundle["pairs"][index]
    src = dict((f["path"], f) for f in bundle["source"]["files"]).get(pair["source_path"])
    dst = dict((f["path"], f) for f in bundle["target"]["files"]).get(pair["target_path"])
    sp, tp = file_patch_text(src), file_patch_text(dst)
    extra = (extra_prompt or "").strip()
    parts = [DEFAULT_INSTRUCTION, ""]
    for side, key in ((bundle["source"], u"원본"), (bundle["target"], u"대상")):
        for w in side["warnings"]:
            parts.append(u"[%s 구간 경고] %s" % (key, w))
    parts += [u"[원본 diff]", sp or u"(원본에는 이 파일의 변경이 없음)", "",
              u"[대상 diff]", tp or u"(대상에는 이 파일의 변경이 없음 - 이식 누락 가능성)"]
    if extra:
        parts += ["", u"[추가 요청]", extra]
    return {
        "prompt": u"\n".join(parts),
        "instruction": DEFAULT_INSTRUCTION,
        "extra_prompt": extra,
        "pair": pair,
        "source_file": src, "target_file": dst,
        "source_patch": sp, "target_patch": tp,
        "source_info": _side_info(bundle["source"]),
        "target_info": _side_info(bundle["target"]),
    }


def find_hook(explicit=None):
    """훅 파일 위치: --llm-hook > 환경변수 CROSSREVIEW_LLM_HOOK > 현재 폴더 > 실행 파일 옆."""
    cands = [explicit, os.environ.get("CROSSREVIEW_LLM_HOOK"),
             os.path.join(os.getcwd(), HOOK_NAME),
             os.path.join(os.path.dirname(os.path.abspath(sys.argv[0] or ".")), HOOK_NAME)]
    for c in cands:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


def call_hook(path, request):
    """훅 파일을 매번 새로 읽어 실행한다 (수정 후 재시작 불필요)."""
    scope = {"__name__": "crossreview_llm_hook", "__file__": path}
    with open(path, "rb") as fp:
        code = compile(fp.read(), path, "exec")
    here = os.path.dirname(path)
    sys.path.insert(0, here)              # 훅이 같은 폴더의 모듈을 import 할 수 있게
    try:
        exec(code, scope)
        fn = scope.get("review")
        if not callable(fn):
            raise RuntimeError("%s 에 review(request) 함수가 없습니다." % path)
        out = fn(request)
    finally:
        if here in sys.path:
            sys.path.remove(here)
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    return out if isinstance(out, str) else str(out)
