"""원본 쪽 파일과 횡전개 대상 쪽 파일을 짝짓는다."""

import difflib
import posixpath


def changed_lines(f):
    """추가/삭제 라인만 (공백 정규화) 추출. 컨텍스트와 라인 번호는 무시한다."""
    out = []
    for h in f["hunks"]:
        for l in h["lines"]:
            if l[:1] in "+-" and l[1:].strip():
                out.append(l[0] + " ".join(l[1:].split()))
    return out


def similarity(a, b):
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    if len(a) + len(b) > 20000:          # 매우 큰 diff 는 집합 기반 근사
        sa, sb = set(a), set(b)
        return round(len(sa & sb) / float(len(sa | sb)), 4)
    return round(difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 4)


def _map_path(path, path_maps):
    for src, dst in path_maps:
        if path.startswith(src):
            return dst + path[len(src):]
    return path


def pair_files(src_files, dst_files, path_maps=None):
    """반환: pair 목록.

    kind      : paired | source_only | target_only
    match_by  : path | path_map | basename
    identical : 변경 라인이 (공백 제외) 완전히 동일 -> LLM 리뷰 생략 후보
    similarity: 변경 라인 유사도 0~1 (낮을수록 우선 리뷰 대상)
    """
    path_maps = path_maps or []
    dst_left = dict((f["path"], f) for f in dst_files)
    pairs, src_left = {}, []

    def add(s, d, by):
        a, b = changed_lines(s), changed_lines(d)
        pairs[s["path"]] = {
            "kind": "paired", "match_by": by,
            "source_path": s["path"], "target_path": d["path"],
            "identical": a == b and s["binary"] == d["binary"],
            "similarity": similarity(a, b),
            "status_mismatch": s["status"] != d["status"],
        }

    for s in src_files:
        want = _map_path(s["path"], path_maps)
        d = dst_left.pop(want, None)
        if d is not None:
            add(s, d, "path" if want == s["path"] else "path_map")
        else:
            src_left.append(s)

    # 경로가 달라진 경우: 파일명이 양쪽에서 유일하면 짝으로 본다
    def by_base(files):
        m = {}
        for f in files:
            m.setdefault(posixpath.basename(f["path"]), []).append(f)
        return m
    sb, db = by_base(src_left), by_base(dst_left.values())
    for s in src_left:
        name = posixpath.basename(s["path"])
        if len(sb[name]) == 1 and len(db.get(name, [])) == 1:
            d = db[name][0]
            del dst_left[d["path"]]
            add(s, d, "basename")

    result = []
    for s in src_files:
        result.append(pairs.get(s["path"]) or {
            "kind": "source_only", "match_by": None, "source_path": s["path"],
            "target_path": None, "identical": False, "similarity": 0.0,
            "status_mismatch": False})
    for d in dst_files:
        if d["path"] in dst_left:
            result.append({
                "kind": "target_only", "match_by": None, "source_path": None,
                "target_path": d["path"], "identical": False, "similarity": 0.0,
                "status_mismatch": False})
    return result
