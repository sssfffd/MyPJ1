"""CLI: python -m crossreview --help"""

from __future__ import print_function

import argparse
import io
import json
import os
import sys

from .gitcmd import GitError
from .resolve import ResolveError
from .collect import DiffOptions
from .bundle import build_side, build_bundle, iter_review_units, file_patch_text

EPILOG = """\
구간 지정 방법 (src/dst 각각, 우선순위 순):
  --X-range A..B   구간 직접 지정 (반복 가능, 과거->최신 순)
  --X-commit C     커밋 1개 (머지 커밋이면 PR 전체, 반복 가능)
  --X-grep KEY     커밋 메시지에 KEY(이슈 번호 등)가 있는 커밋을 모두 모음
  --X-head/--X-base  PR 모드: merge-base(base, head)..head  (기본)

UI:
  python -m crossreview                 인자 없이 실행 -> 로컬 UI (브라우저)
  python -m crossreview ... --ui        추출 결과를 바로 UI 로 확인
  python -m crossreview ... --html r.html   서버 없이 열 수 있는 HTML 리포트

예시:
  # 원본 PR(v1) 과 횡전개 PR(v2) 비교 - 서로 다른 레포
  python -m crossreview --src-repo ../v1 --src-base origin/main --src-head feature/login \\
                        --dst-repo ../v2 --dst-base origin/main --dst-head feature/login -o out.json

  # 같은 레포의 다른 브랜치, 원본은 이미 머지된 머지 커밋
  python -m crossreview --src-commit 1a2b3c4 --dst-base origin/release-2.0 --dst-head HEAD

  # 원본 기능이 PR 2번(수정 PR 포함)에 걸쳐 들어간 경우
  python -m crossreview --src-grep PROJ-123 --src-head origin/main \\
                        --dst-base origin/release-2.0 --dst-head HEAD
"""


def _side_args(p, prefix, title):
    g = p.add_argument_group(title)
    g.add_argument("--%s-repo" % prefix, default=".", metavar="PATH", help="로컬 레포 경로 (기본: .)")
    g.add_argument("--%s-head" % prefix, metavar="REV", help="PR 브랜치/커밋 (기본: HEAD)")
    g.add_argument("--%s-base" % prefix, metavar="REV", help="PR 대상 브랜치 (예: origin/main)")
    g.add_argument("--%s-commit" % prefix, action="append", metavar="REV")
    g.add_argument("--%s-range" % prefix, action="append", metavar="A..B")
    g.add_argument("--%s-grep" % prefix, metavar="KEY")
    g.add_argument("--%s-pathspec" % prefix, action="append", metavar="PATH",
                   help="이 경로만 대상으로 (git pathspec, 반복 가능)")
    g.add_argument("--%s-label" % prefix, metavar="TEXT")


def build_parser():
    p = argparse.ArgumentParser(
        prog="crossreview", description="횡전개 코드 리뷰용 diff 수집기",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    _side_args(p, "src", "원본 (기능을 처음 개발한 쪽)")
    _side_args(p, "dst", "대상 (횡전개한 쪽, 리뷰 대상)")
    p.add_argument("--path-map", action="append", default=[], metavar="SRC=DST",
                   help="경로 prefix 매핑 (예: src/v1/=src/v2/), 반복 가능")
    p.add_argument("-U", "--context", type=int, default=3, help="컨텍스트 라인 수 (기본 3)")
    p.add_argument("-W", "--function-context", action="store_true", help="함수 전체를 컨텍스트로")
    p.add_argument("-b", "--ignore-space", action="store_true", help="공백 변경 무시")
    p.add_argument("--encoding", action="append", metavar="ENC",
                   help="소스 인코딩 후보 (기본: utf-8, cp949 순서로 시도)")
    p.add_argument("--include-body", action="store_true", help="커밋 메시지 본문 포함")
    p.add_argument("-o", "--output", metavar="FILE", help="bundle JSON 출력 (기본: stdout)")
    p.add_argument("--split-dir", metavar="DIR",
                   help="파일 짝마다 JSON 1개씩 추가 출력 (LLM 호출 단위)")
    p.add_argument("--skip-identical", action="store_true",
                   help="--split-dir 출력에서 변경 내용이 동일한 짝은 제외")
    p.add_argument("--html", metavar="FILE",
                   help="브라우저로 바로 열 수 있는 단일 HTML 리포트 출력")
    p.add_argument("--ui", action="store_true",
                   help="로컬 UI 실행 (인자 없이 실행해도 UI 가 뜬다)")
    p.add_argument("--port", type=int, default=8765, help="UI 포트 (기본 8765, 127.0.0.1 전용)")
    p.add_argument("--no-browser", action="store_true", help="UI 실행 시 브라우저를 열지 않음")
    p.add_argument("--out-dir", metavar="DIR",
                   help="결과 한 벌 저장: bundle.json, report.html, diffs/*.diff "
                        "(UI 에서는 저장 폴더의 기본값, 기본 ./crossreview_out)")
    p.add_argument("--llm-hook", metavar="FILE", help="LLM 훅 파일 (기본: ./llm_hook.py)")
    p.add_argument("--browser-tab", action="store_true",
                   help="UI 를 앱 창 대신 기본 브라우저 탭으로 열기")
    p.add_argument("--strict", action="store_true", help="warning 이 있으면 exit code 2")
    p.add_argument("--git", default="git", help="git 실행 파일 경로")
    return p


def _dump(obj, path):
    with io.open(path, "w", encoding="utf-8") as fp:
        fp.write(json.dumps(obj, ensure_ascii=False, indent=2))
        fp.write(u"\n")


SIDE_KEYS = ("head", "base", "commit", "range", "grep")


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    args = build_parser().parse_args(argv)
    has_spec = any(getattr(args, "%s_%s" % (p, k)) for p in ("src", "dst") for k in SIDE_KEYS)
    if not argv:
        args.ui = True                      # 인자 없이 실행(더블클릭) -> UI
    if args.ui and not has_spec:
        from .server import serve
        return serve(args.port, not args.no_browser, git=args.git, hook=args.llm_hook,
                     out_base=args.out_dir, as_tab=args.browser_tab)
    maps = []
    for m in args.path_map:
        if "=" not in m:
            print("error: --path-map 형식은 SRC=DST", file=sys.stderr)
            return 1
        maps.append(tuple(m.split("=", 1)))

    def side(prefix, label):
        g = lambda n: getattr(args, "%s_%s" % (prefix, n))
        opts = DiffOptions(context=args.context, function_context=args.function_context,
                           ignore_space=args.ignore_space, pathspecs=g("pathspec"),
                           encodings=args.encoding, include_body=args.include_body)
        return build_side(g("repo"), g("label") or label, head=g("head"), base=g("base"),
                          commits=g("commit"), ranges=g("range"), grep=g("grep"),
                          opts=opts, git=args.git)

    try:
        bundle = build_bundle(side("src", "source"), side("dst", "target"), maps)
    except (GitError, ResolveError, RuntimeError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1

    if args.output:
        _dump(bundle, args.output)
    elif not (args.html or args.ui or args.out_dir):
        text = json.dumps(bundle, ensure_ascii=False, indent=2)
        getattr(sys.stdout, "buffer", sys.stdout).write((text + "\n").encode("utf-8"))

    if args.split_dir:
        if not os.path.isdir(args.split_dir):
            os.makedirs(args.split_dir)
        for i, (pair, s, d) in enumerate(iter_review_units(bundle, args.skip_identical), 1):
            _dump({"pair": pair, "source_file": s, "target_file": d,
                   "source_patch": file_patch_text(s), "target_patch": file_patch_text(d)},
                  os.path.join(args.split_dir, "%04d.json" % i))

    # 사람이 볼 요약은 stderr 로
    err = sys.stderr
    for key in ("source", "target"):
        sd = bundle[key]
        print("[%s] mode=%s composed=%s commits=%d files=%d (+%d -%d)" % (
            key, sd["mode"], sd["composed"], sum(len(s["commits"]) for s in sd["segments"]),
            sd["stats"]["files"], sd["stats"]["additions"], sd["stats"]["deletions"]), file=err)
        for s in sd["segments"]:
            print("    segment: %s" % s["how"], file=err)
        for n in sd["notes"]:
            print("    note: %s" % n, file=err)
        for w in sd["warnings"]:
            print("    WARNING: %s" % w, file=err)
    sm = bundle["summary"]
    print("[pairs] paired=%d (identical=%d) source_only=%d target_only=%d" % (
        sm["paired"], sm["identical"], sm["source_only"], sm["target_only"]), file=err)
    if args.out_dir:
        from .export import save_run
        bundle["saved_to"] = save_run(bundle, args.out_dir)
        print("[saved] %s" % bundle["saved_to"], file=sys.stderr)
    if args.html:
        from .export import write_html
        write_html(bundle, args.html)
    if args.ui:
        from .server import serve
        return serve(args.port, not args.no_browser, initial_bundle=bundle, git=args.git,
                     hook=args.llm_hook, as_tab=args.browser_tab)
    return 2 if (args.strict and sm["warnings"]) else 0
