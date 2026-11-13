"""결과를 파일로 저장한다: bundle.json, report.html, 파일별 .diff, LLM 결과(.md)."""

import io
import json
import os
import pkgutil
import re

from .bundle import file_patch_text, iter_review_units


def _js(obj):
    """<script> 안에 안전하게 넣을 수 있는 JSON."""
    return (json.dumps(obj, ensure_ascii=False)
            .replace("</", "<\\/").replace("<!--", "<\\!--")
            .replace(u"\u2028", "\\u2028").replace(u"\u2029", "\\u2029"))


def render_html(bundle=None, token=None, hook=None):
    html = pkgutil.get_data(__package__, "ui.html").decode("utf-8")
    html = html.replace("/*__TOKEN__*/null", _js(token), 1)
    html = html.replace("/*__HOOK__*/null", _js(hook), 1)
    return html.replace("/*__BUNDLE__*/null", _js(bundle), 1)


def _write(path, text):
    with io.open(path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(text)


def write_json(obj, path):
    _write(path, json.dumps(obj, ensure_ascii=False, indent=2) + u"\n")


def write_html(bundle, path):
    """서버 없이 브라우저로 바로 열 수 있는 단일 HTML 리포트."""
    _write(path, render_html(bundle))


def unit_name(index, pair):
    """파일 짝의 저장용 이름: 0001_app.c"""
    base = (pair["source_path"] or pair["target_path"]).replace("\\", "/").split("/")[-1]
    return "%04d_%s" % (index + 1, re.sub(r"[^\w.\-]+", "_", base, flags=re.U) or "file")


def _mkdir(path):
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def run_title(bundle):
    def one(side):
        seg = side["segments"][-1]["how"] if side["segments"] else ""
        return "%s [%s]" % (os.path.basename(side["repo"].rstrip("/\\")), seg)
    return "%s  ->  %s" % (one(bundle["source"]), one(bundle["target"]))


def save_summary(bundle, outdir):
    write_json({"title": run_title(bundle), "generated_at": bundle.get("generated_at"),
                "summary": bundle.get("summary"),
                "llm_results": len(bundle.get("llm_results") or {})},
               os.path.join(outdir, "meta.json"))
    write_json(bundle, os.path.join(outdir, "bundle.json"))
    write_html(bundle, os.path.join(outdir, "report.html"))


def save_run(bundle, outdir):
    """추출 결과 한 벌을 outdir 에 저장한다.

    bundle.json / report.html / diffs/NNNN_<파일>.source.diff, .target.diff
    """
    _mkdir(outdir)
    diffs = _mkdir(os.path.join(outdir, "diffs"))
    for i, (pair, s, d) in enumerate(iter_review_units(bundle)):
        name = unit_name(i, pair)
        if s is not None:
            _write(os.path.join(diffs, name + ".source.diff"), file_patch_text(s))
        if d is not None:
            _write(os.path.join(diffs, name + ".target.diff"), file_patch_text(d))
    save_summary(bundle, outdir)
    return os.path.abspath(outdir)


def save_llm(bundle, outdir, index, record):
    """LLM 결과 1건을 llm/NNNN_<파일>.md 로 저장하고 bundle.json / report.html 도 갱신한다."""
    pair = bundle["pairs"][index]
    path = os.path.join(_mkdir(os.path.join(_mkdir(outdir), "llm")), unit_name(index, pair) + ".md")
    _write(path, u"# LLM 리뷰\n\n- 원본: %s\n- 대상: %s\n- 시각: %s\n\n## 추가 프롬프트\n\n%s\n\n## 결과\n\n%s\n" % (
        pair["source_path"] or "(없음)", pair["target_path"] or "(없음)", record.get("at", ""),
        record.get("extra_prompt") or "(없음)", record.get("result", "")))
    save_summary(bundle, outdir)
    return os.path.abspath(path)
