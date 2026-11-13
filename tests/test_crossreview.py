# -*- coding: utf-8 -*-
"""시나리오 테스트. 임시 git 레포를 만들어 실제 git 으로 검증한다.

실행: python -m unittest discover -s tests -v
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crossreview import Repo, ResolveError, resolve_side  # noqa: E402
from crossreview.bundle import build_side, build_bundle, iter_review_units, file_patch_text  # noqa: E402
from crossreview.cli import main  # noqa: E402

BASE_FILE = "".join("line %d\n" % i for i in range(1, 41))


class TmpRepo(object):
    def __init__(self, root, name):
        self.path = os.path.join(root, name)
        os.mkdir(self.path)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@t")
        self.git("config", "commit.gpgsign", "false")
        self.n = 0

    def git(self, *args):
        return subprocess.check_output(("git", "-C", self.path) + args,
                                       stderr=subprocess.STDOUT).decode().strip()

    def write(self, rel, text):
        full = os.path.join(self.path, rel)
        if not os.path.isdir(os.path.dirname(full)):
            os.makedirs(os.path.dirname(full))
        with io.open(full, "w", encoding="utf-8", newline="\n") as fp:
            fp.write(text)

    def read(self, rel):
        with io.open(os.path.join(self.path, rel), encoding="utf-8") as fp:
            return fp.read()

    def sub(self, rel, old, new):
        text = self.read(rel)
        assert old in text, old
        self.write(rel, text.replace(old, new))

    def commit(self, msg):
        self.n += 1
        self.git("add", "-A")
        env = "2024-01-01T00:%02d:00" % self.n
        subprocess.check_output(
            ["git", "-C", self.path, "commit", "-q", "-m", msg, "--date", env],
            env=dict(os.environ, GIT_COMMITTER_DATE=env))
        return self.git("rev-parse", "HEAD")


def added(side, path=None):
    out = []
    for f in side["files"]:
        if path and f["path"] != path:
            continue
        for h in f["hunks"]:
            out += [l[1:] for l in h["lines"] if l.startswith("+")]
    return out


class Scenarios(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.r = TmpRepo(self.root, "r")
        self.r.write("app.c", BASE_FILE)
        self.r.write("util.c", BASE_FILE)
        self.r.commit("init")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # 1) 커밋 여러 개짜리 PR: 직전 커밋이 아니라 PR 전체가 잡혀야 한다
    def test_multi_commit_pr(self):
        r = self.r
        r.git("checkout", "-q", "-b", "feat")
        r.sub("app.c", "line 5\n", "line 5 WIP\n"); r.commit("wip")
        r.sub("app.c", "line 5 WIP\n", "line 5 FEATURE\n"); r.commit("fix typo")
        r.sub("util.c", "line 30\n", "line 30 FEATURE\n"); r.commit("util")
        side = build_side(r.path, "s", head="feat", base="main")
        self.assertEqual(side["mode"], "pr")
        self.assertEqual(len(side["segments"][0]["commits"]), 3)
        self.assertEqual(sorted(added(side)), ["line 30 FEATURE", "line 5 FEATURE"])  # WIP 는 안 보임

    # 2) base 가 앞으로 나갔고, base 를 PR 브랜치로 머지까지 한 경우
    def test_base_moved_and_merged_into_pr(self):
        r = self.r
        r.git("checkout", "-q", "-b", "feat")
        r.sub("app.c", "line 5\n", "line 5 FEATURE\n"); r.commit("feat")
        r.git("checkout", "-q", "main")
        r.sub("util.c", "line 10\n", "line 10 OTHER\n"); r.commit("other work on main")
        r.git("checkout", "-q", "feat")
        r.git("merge", "-q", "--no-edit", "main")
        r.sub("app.c", "line 20\n", "line 20 FEATURE\n"); r.commit("feat2")
        side = build_side(r.path, "s", head="feat", base="main")
        self.assertEqual([f["path"] for f in side["files"]], ["app.c"])     # main 쪽 변경 미포함
        self.assertTrue(any("머지 커밋" in n for n in side["notes"]))

    # 3) 이미 머지된 PR: 머지 커밋을 찾아 PR 전체를 복원
    def test_already_merged_pr(self):
        r = self.r
        r.git("checkout", "-q", "-b", "feat")
        r.sub("app.c", "line 5\n", "line 5 A\n"); r.commit("a")
        r.sub("app.c", "line 6\n", "line 6 B\n"); r.commit("b")
        r.git("checkout", "-q", "main")
        r.sub("util.c", "line 10\n", "line 10 OTHER\n"); r.commit("other")
        r.git("merge", "-q", "--no-ff", "--no-edit", "feat")
        merge = r.git("rev-parse", "HEAD")
        side = build_side(r.path, "s", head="feat", base="main")
        self.assertEqual(side["mode"], "merged-pr")
        self.assertEqual(added(side), ["line 5 A", "line 6 B"])
        self.assertTrue(side["warnings"])
        # 머지 커밋을 직접 줘도 동일
        side2 = build_side(r.path, "s", commits=[merge])
        self.assertEqual(added(side2), ["line 5 A", "line 6 B"])

    def test_fast_forward_merged_is_error(self):
        r = self.r
        r.git("checkout", "-q", "-b", "feat")
        r.sub("app.c", "line 5\n", "line 5 A\n"); r.commit("a")
        r.git("checkout", "-q", "main")
        r.git("merge", "-q", "--ff-only", "feat")
        with self.assertRaises(ResolveError):
            resolve_side(Repo(r.path), head="feat", base="main")

    # 4) PR 이 2번 머지된 경우 (본 PR + 수정 PR, 사이에 다른 작업이 낌)
    def _two_prs(self):
        r = self.r
        r.git("checkout", "-q", "-b", "pr1")
        r.sub("app.c", "line 5\n", "line 5 FEAT-wrong\n"); r.commit("PROJ-1 feature")
        r.git("checkout", "-q", "main")
        r.git("merge", "-q", "--no-ff", "-m", "Merge PR 1", "pr1")
        m1 = r.git("rev-parse", "HEAD")
        r.sub("app.c", "line 35\n", "line 35 UNRELATED\n")
        r.sub("util.c", "line 10\n", "line 10 UNRELATED\n"); r.commit("unrelated work")
        r.git("checkout", "-q", "-b", "pr2")
        r.sub("app.c", "line 5 FEAT-wrong\n", "line 5 FEAT\n")
        r.sub("util.c", "line 30\n", "line 30 FEAT\n"); r.commit("PROJ-1 fix review comments")
        r.git("checkout", "-q", "main")
        r.git("merge", "-q", "--no-ff", "-m", "Merge PR 2", "pr2")
        return m1, r.git("rev-parse", "HEAD")

    def test_two_prs_explicit(self):
        m1, m2 = self._two_prs()
        side = build_side(self.r.path, "s", commits=[m1, m2])
        self.assertEqual(side["composed"], "net")
        self.assertEqual(sorted(added(side)), ["line 30 FEAT", "line 5 FEAT"])   # wrong/UNRELATED 없음
        self.assertEqual(len(side["segments"]), 2)

    def test_two_prs_grep(self):
        self._two_prs()
        side = build_side(self.r.path, "s", grep="PROJ-1", head="main")
        self.assertEqual(side["mode"], "grep")
        self.assertEqual(side["composed"], "net")
        self.assertEqual(sorted(added(side)), ["line 30 FEAT", "line 5 FEAT"])

    # 5) revert 후 재머지
    def test_revert_and_reapply(self):
        r = self.r
        r.sub("app.c", "line 5\n", "line 5 V1\n"); c1 = r.commit("PROJ-2 feature")
        r.sub("util.c", "line 10\n", "line 10 UNRELATED\n"); r.commit("unrelated")
        r.git("revert", "--no-edit", c1)
        r.sub("util.c", "line 20\n", "line 20 UNRELATED\n"); r.commit("unrelated 2")
        r.sub("app.c", "line 5\n", "line 5 V2\n"); r.commit("PROJ-2 feature (again)")
        side = build_side(r.path, "s", grep="PROJ-2", head="main")
        self.assertEqual(added(side), ["line 5 V2"])
        self.assertTrue(any("revert" in n for n in side["notes"]))

    # 6) 같은 내용이 중복 커밋된 경우 경고
    def test_duplicate_patch_warning(self):
        r = self.r
        r.git("checkout", "-q", "-b", "feat")
        r.sub("app.c", "line 5\n", "line 5 X\n"); c = r.commit("x")
        r.git("revert", "--no-edit", c)
        r.git("cherry-pick", "--allow-empty", c)
        side = build_side(r.path, "s", head="feat", base="main")
        self.assertTrue(any("중복" in w for w in side["warnings"]))
        self.assertEqual(added(side), ["line 5 X"])

    # 7) 합성 불가(사이에 낀 변경과 같은 줄 충돌) -> sequential fallback
    def test_sequential_fallback(self):
        r = self.r
        r.sub("app.c", "line 5\n", "line 5 A\n"); c1 = r.commit("K-1 a")
        r.sub("app.c", "line 5 A\n", "line 5 A other\n"); r.commit("someone else")
        r.sub("app.c", "line 5 A other\n", "line 5 A other B\n"); c3 = r.commit("K-1 b")
        side = build_side(r.path, "s", commits=[c1, c3])
        self.assertEqual(side["composed"], "sequential")
        self.assertEqual(len(side["files"]), 1)
        self.assertEqual([h["segment"] for h in side["files"][0]["hunks"]], [0, 1])
        self.assertTrue(side["warnings"])

    # 8) 레포를 건드리지 않는다
    def test_repo_untouched(self):
        m1, m2 = self._two_prs()
        before = (self.r.git("status", "--porcelain"), self.r.git("count-objects", "-v"))
        build_side(self.r.path, "s", commits=[m1, m2])
        self.assertEqual(before, (self.r.git("status", "--porcelain"),
                                  self.r.git("count-objects", "-v")))

    # 9) 서로 다른 레포 간 비교 + 경로 매핑 + CLI
    def test_cross_repo_bundle_and_cli(self):
        r = self.r
        r.git("checkout", "-q", "-b", "feat")
        r.sub("app.c", "line 5\n", "line 5 FEATURE\n")
        r.sub("util.c", "line 30\n", "line 30 FEATURE\n")
        r.write("한글 파일.c", u"새 파일\n")
        r.commit("feature")

        t = TmpRepo(self.root, "t")                   # 다른 버전: 디렉터리 구조/주변 코드가 다름
        t.write("src/app.c", "// v2 header\n" + BASE_FILE)
        t.write("lib/util.c", BASE_FILE)
        t.commit("init v2")
        t.git("checkout", "-q", "-b", "port")
        t.sub("src/app.c", "line 5\n", "line 5 FEATURE\n")
        t.sub("lib/util.c", "line 30\n", "line 30 FEATURE-typo\n")     # 잘못 이식
        t.write("extra.c", "x\n")
        t.commit("port 1")
        t.sub("src/app.c", "line 6\n", "line 6 tmp\n"); t.commit("tmp")
        t.sub("src/app.c", "line 6 tmp\n", "line 6\n"); t.commit("undo tmp")

        out = os.path.join(self.root, "out.json")
        split = os.path.join(self.root, "split")
        code = main(["--src-repo", r.path, "--src-base", "main", "--src-head", "feat",
                     "--dst-repo", t.path, "--dst-base", "main", "--dst-head", "port",
                     "--path-map", "app.c=src/app.c", "-o", out, "--split-dir", split])
        self.assertEqual(code, 0)
        with io.open(out, encoding="utf-8") as fp:
            b = json.load(fp)
        by_src = dict((p["source_path"], p) for p in b["pairs"])
        self.assertEqual(by_src["app.c"]["match_by"], "path_map")
        self.assertTrue(by_src["app.c"]["identical"])            # 라인 번호가 달라도 동일 판정
        self.assertEqual(by_src["util.c"]["match_by"], "basename")
        self.assertFalse(by_src["util.c"]["identical"])
        self.assertLess(by_src["util.c"]["similarity"], 1.0)
        self.assertEqual(by_src[u"한글 파일.c"]["kind"], "source_only")
        self.assertEqual(b["pairs"][-1]["kind"], "target_only")
        self.assertEqual(b["summary"], {"paired": 2, "identical": 1, "source_only": 1,
                                        "target_only": 1, "warnings": 0})
        self.assertEqual(len(os.listdir(split)), 4)
        units = list(iter_review_units(b, skip_identical=True))
        self.assertEqual(len(units), 3)
        self.assertIn("+line 30 FEATURE-typo", file_patch_text(units[0][2]))

    # 10) UI: 추출 요청 처리와 HTML 리포트
    def test_ui_request_and_html(self):
        from crossreview.server import run_request, render_html, write_html
        r = self.r
        r.git("checkout", "-q", "-b", "feat")
        r.sub("app.c", "line 5\n", "line 5 </script><b>X\n"); r.commit("feature")
        side = {"repo": r.path, "base": "main", "head": "feat"}
        b = run_request({"src": side, "dst": side, "context": 1, "path_maps": []})
        self.assertEqual(b["summary"]["identical"], 1)
        html = render_html(b, token="tok", hook={"path": None, "out": "x"})
        self.assertIn('var TOKEN = "tok";', html)
        self.assertNotIn("</script><b>X", html)                # 스크립트 탈출 방지
        out = os.path.join(self.root, "r.html")
        write_html(b, out)
        with io.open(out, encoding="utf-8") as fp:
            self.assertIn("var TOKEN = null;", fp.read())
        with self.assertRaises(ResolveError):
            run_request({"src": side, "dst": side, "path_maps": ["bad"]})

    # 11) 레포/브랜치 선택용 목록, 파일 저장, LLM 훅
    def test_refs_browse_export_llm(self):
        from crossreview.server import run_request, list_refs, browse
        from crossreview.export import save_run, save_llm
        from crossreview import llm
        r = self.r
        r.git("checkout", "-q", "-b", "feat")
        r.sub("app.c", "line 5\n", "line 5 X\n"); r.commit("feature")
        r.git("tag", "v1.0")
        refs = list_refs(r.path)
        self.assertEqual(refs["current"], "feat")
        self.assertEqual(sorted(refs["branches"]), ["feat", "main"])
        self.assertEqual(refs["tags"], ["v1.0"])
        self.assertEqual(refs["commits"][0]["subject"], "feature")
        d = browse(self.root)
        self.assertEqual([(x["name"], x["repo"]) for x in d["dirs"]], [("r", True)])

        side = {"repo": r.path, "base": "main", "head": "feat"}
        b = run_request({"src": side, "dst": side})
        out = os.path.join(self.root, "out")
        save_run(b, out)
        self.assertEqual(sorted(os.listdir(out)), ["bundle.json", "diffs", "meta.json", "report.html"])
        self.assertEqual(sorted(os.listdir(os.path.join(out, "diffs"))),
                         ["0001_app.c.source.diff", "0001_app.c.target.diff"])

        req = llm.build_request(b, 0, u"예외 처리만 봐줘")
        self.assertIn("+line 5 X", req["prompt"])
        self.assertIn(u"예외 처리만 봐줘", req["prompt"])
        hook = os.path.join(self.root, "llm_hook.py")
        with io.open(hook, "w", encoding="utf-8") as fp:
            fp.write(u"def review(request):\n    return u'결과: ' + request['pair']['source_path']\n")
        self.assertEqual(llm.find_hook(hook), hook)
        text = llm.call_hook(hook, req)
        self.assertEqual(text, u"결과: app.c")
        rec = {"result": text, "extra_prompt": req["extra_prompt"], "at": "t"}
        b["llm_results"] = {"0": rec}
        path = save_llm(b, out, 0, rec)
        with io.open(path, encoding="utf-8") as fp:
            self.assertIn(u"결과: app.c", fp.read())
        with io.open(os.path.join(out, "bundle.json"), encoding="utf-8") as fp:
            self.assertEqual(json.load(fp)["llm_results"]["0"]["result"], text)

    # 12) 웹 서버 모드: run 저장소(여러 사용자 분리), 경로 제한
    def test_runs_store_and_path_restriction(self):
        from crossreview.server import run_request, Runs, _Server
        r = self.r
        r.git("checkout", "-q", "-b", "feat")
        r.sub("app.c", "line 5\n", "line 5 X\n"); r.commit("feature")
        side = {"repo": r.path, "base": "main", "head": "feat"}
        runs = Runs(os.path.join(self.root, "store"))
        a = runs.create(run_request({"src": side, "dst": side}))
        b = runs.create(run_request({"src": side, "dst": side}))
        self.assertNotEqual(a["run_id"], b["run_id"])
        runs.add_llm(a["run_id"], 0, {"result": "R", "extra_prompt": "", "at": "t"})
        fresh = Runs(os.path.join(self.root, "store"))            # 재시작 후 디스크에서 복원
        self.assertEqual(fresh.get(a["run_id"])["llm_results"]["0"]["result"], "R")
        self.assertNotIn("llm_results", fresh.get(b["run_id"]))
        self.assertEqual(sorted(x["id"] for x in fresh.list()), sorted([a["run_id"], b["run_id"]]))
        for bad in ("../x", "", "a/b"):
            with self.assertRaises(ValueError):
                fresh.get(bad)

        class Fake(object):
            roots = [os.path.realpath(self.root)]
            check_path = _Server.check_path
        f = Fake()
        self.assertEqual(f.check_path("r"), os.path.realpath(r.path))
        for bad in ("..", "/etc", os.path.join(r.path, "..", "..")):
            with self.assertRaises(ValueError):
                f.check_path(bad)

    def test_rename_binary_and_root_commit(self):
        r = self.r
        root = r.git("rev-list", "--max-parents=0", "HEAD")
        side = build_side(r.path, "s", commits=[root])            # 루트 커밋
        self.assertEqual(side["stats"]["files"], 2)
        r.git("checkout", "-q", "-b", "feat")
        r.git("mv", "util.c", "helper.c")
        with open(os.path.join(r.path, "img.bin"), "wb") as fp:
            fp.write(bytes(bytearray(range(256))))
        with open(os.path.join(r.path, "kr.c"), "wb") as fp:
            fp.write(u"// 한글 주석\n".encode("cp949"))
        r.commit("rename + binary + cp949")
        side = build_side(r.path, "s", head="feat", base="main")
        files = dict((f["path"], f) for f in side["files"])
        self.assertEqual(files["helper.c"]["status"], "renamed")
        self.assertEqual(files["helper.c"]["old_path"], "util.c")
        self.assertTrue(files["img.bin"]["binary"])
        self.assertEqual(files["kr.c"]["hunks"][0]["lines"], [u"+// 한글 주석"])


if __name__ == "__main__":
    unittest.main()
