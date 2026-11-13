"""양쪽 데이터를 묶어 LLM 에 넘길 bundle(dict) 을 만든다."""

import datetime

from .gitcmd import Repo
from .resolve import resolve_side
from .collect import collect_side, DiffOptions
from .pairing import pair_files

SCHEMA_VERSION = 1


def build_side(repo_path, label, head=None, base=None, commits=None, ranges=None,
               grep=None, opts=None, git="git"):
    repo = Repo(repo_path, git=git)
    res = resolve_side(repo, head=head, base=base, commits=commits, ranges=ranges, grep=grep)
    return collect_side(repo, res, opts or DiffOptions(), label=label, base_ref=base)


def build_bundle(source, target, path_maps=None):
    """source/target: build_side() 결과."""
    pairs = pair_files(source["files"], target["files"], path_maps)
    kinds = [p["kind"] for p in pairs]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
        "summary": {
            "paired": kinds.count("paired"),
            "identical": sum(1 for p in pairs if p["identical"]),
            "source_only": kinds.count("source_only"),
            "target_only": kinds.count("target_only"),
            "warnings": len(source["warnings"]) + len(target["warnings"]),
        },
        "source": source,
        "target": target,
        "pairs": pairs,
    }


def file_patch_text(f):
    """파일 dict -> unified diff 텍스트 (LLM 프롬프트에 그대로 넣을 수 있는 형태)."""
    if f is None:
        return ""
    out = ["--- a/%s" % (f["old_path"] or f["path"]), "+++ b/%s" % f["path"]]
    if f["binary"]:
        out.append("(binary file)")
    for h in f["hunks"]:
        out.append("@@ -%d,%d +%d,%d @@ %s" % (h["old_start"], h["old_lines"],
                                              h["new_start"], h["new_lines"], h["section"]))
        out += h["lines"]
    return "\n".join(out) + "\n"


def iter_review_units(bundle, skip_identical=False):
    """리뷰 단위(파일 짝)를 하나씩 꺼낸다: (pair, source_file|None, target_file|None)."""
    src = dict((f["path"], f) for f in bundle["source"]["files"])
    dst = dict((f["path"], f) for f in bundle["target"]["files"])
    for p in bundle["pairs"]:
        if skip_identical and p["identical"]:
            continue
        yield p, src.get(p["source_path"]), dst.get(p["target_path"])
