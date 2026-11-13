"""'어떤 구간을 diff 할 것인가' 를 결정한다.

결과는 segment 목록이다. segment = {"base": sha, "head": sha, "how": 설명}
 - 일반 PR           : merge-base(base, head) .. head   (커밋이 몇 개든 PR 전체 1구간)
 - 이미 머지된 PR    : 머지 커밋 M 의 M^1 .. M
 - 단일 커밋         : C^1 .. C
 - 여러 번 나뉜 기능 : 구간 여러 개 (collect 단계에서 하나의 net diff 로 합성)
"""

import re

DEFAULT_BASES = ["origin/HEAD", "origin/main", "origin/master", "origin/develop",
                 "main", "master", "develop"]


class ResolveError(Exception):
    pass


def rev(repo, name):
    code, out, _ = repo.run_raw(["rev-parse", "--verify", "--quiet", "--end-of-options",
                                 name + "^{commit}"])
    if code != 0:
        raise ResolveError("'%s' 를 커밋으로 해석할 수 없습니다 (repo: %s). "
                           "fetch 여부와 브랜치/sha 를 확인하세요." % (name, repo.path))
    return out.decode().strip()


def parents(repo, sha):
    return repo.text(["rev-list", "--parents", "-n", "1", sha]).split()[1:]


def empty_tree(repo):
    return repo.run(["hash-object", "-t", "tree", "--stdin"], input=b"").decode().strip()


def first_parent(repo, sha):
    """첫 번째 부모. 루트 커밋이면 빈 tree."""
    ps = parents(repo, sha)
    return ps[0] if ps else empty_tree(repo)


def is_ancestor(repo, a, b):
    return repo.ok(["merge-base", "--is-ancestor", a, b])


def short(sha):
    return sha[:10]


def _seg(base, head, how):
    return {"base": base, "head": head, "how": how}


def _mainline_unit(repo, commit, fp_list, tip):
    """commit 을 mainline(first-parent 라인)에 반영시킨 커밋을 찾는다.

    commit 이 mainline 위에 있으면 자기 자신, PR 내부 커밋이면 그 PR 의 머지 커밋.
    fp_list: tip 의 first-parent 목록 (최신 -> 과거)
    """
    if commit in set(fp_list):
        return commit
    ancestry = set(repo.lines(["rev-list", "--ancestry-path", commit + ".." + tip]))
    for sha in reversed(fp_list):      # 과거 -> 최신: commit 을 포함하는 가장 오래된 mainline 커밋
        if sha in ancestry:
            return sha
    return None


def _detect_base(repo, head_sha):
    for cand in DEFAULT_BASES:
        code, out, _ = repo.run_raw(["rev-parse", "--verify", "--quiet", cand + "^{commit}"])
        if code == 0 and out.decode().strip() != head_sha:
            return cand
    return None


def _stale_base_note(repo, base, notes):
    """base 가 로컬 브랜치이고 upstream 보다 뒤처져 있으면 알려준다."""
    code, out, _ = repo.run_raw(["rev-list", "--count", "%s..%s@{upstream}" % (base, base)])
    if code == 0 and out.strip() not in (b"", b"0"):
        notes.append("base '%s' 가 upstream 보다 %s 커밋 뒤처져 있습니다. "
                     "origin/<branch> 형태로 지정하는 것을 권장합니다."
                     % (base, out.decode().strip()))


def _resolve_pr(repo, head, base, notes, warnings):
    head_sha = rev(repo, head)
    if base is None:
        if len(parents(repo, head_sha)) > 1:
            notes.append("base 미지정 + head 가 머지 커밋 -> 머지 커밋의 first-parent diff 사용")
            return "merge-commit", [_seg(first_parent(repo, head_sha), head_sha,
                                         "merge commit %s^1..%s" % (short(head_sha), short(head_sha)))]
        base = _detect_base(repo, head_sha)
        if base is None:
            raise ResolveError("base 브랜치를 자동으로 찾지 못했습니다. --*-base 를 지정하세요.")
        notes.append("base 미지정 -> '%s' 자동 선택" % base)
    base_sha = rev(repo, base)
    _stale_base_note(repo, base, notes)

    mbs = repo.lines(["merge-base", "--all", base_sha, head_sha])
    if not mbs:
        raise ResolveError("'%s' 와 '%s' 사이에 공통 조상이 없습니다. "
                           "--*-range 로 구간을 직접 지정하세요." % (base, head))
    if len(mbs) > 1:
        warnings.append("merge-base 가 %d개(criss-cross merge)입니다. 첫 번째(%s)를 사용합니다."
                        % (len(mbs), short(mbs[0])))
    mb = mbs[0]

    if mb != head_sha:
        return "pr", [_seg(mb, head_sha, "merge-base(%s, %s)..%s" % (base, head, head))]

    # head 가 이미 base 에 포함됨 = 이미 머지된 PR
    fp = repo.lines(["rev-list", "--first-parent", base_sha])
    unit = _mainline_unit(repo, head_sha, fp, base_sha)
    if unit is None or unit == head_sha or len(parents(repo, unit)) < 2:
        raise ResolveError(
            "'%s' 는 이미 '%s' 에 포함되어 있는데 머지 커밋을 찾을 수 없습니다 "
            "(fast-forward / rebase 머지 또는 head == base). "
            "--*-range <시작>..<끝> 또는 --*-commit 으로 구간을 직접 지정하세요." % (head, base))
    warnings.append("'%s' 는 이미 '%s' 에 머지되어 있습니다. 머지 커밋 %s 의 first-parent diff 를 사용합니다."
                    % (head, base, short(unit)))
    if head_sha not in parents(repo, unit)[1:]:
        warnings.append("머지 커밋 %s 는 '%s' 이후의 커밋도 함께 머지했습니다 (추가 push 후 머지)."
                        % (short(unit), head))
    return "merged-pr", [_seg(first_parent(repo, unit), unit,
                              "merge commit %s^1..%s" % (short(unit), short(unit)))]


def _resolve_range(repo, spec, warnings):
    m = re.match(r"^(.+?)(\.\.\.?)(.+)$", spec)
    if not m:
        raise ResolveError("range 형식 오류: '%s' (A..B 또는 A...B)" % spec)
    a, dots, b = rev(repo, m.group(1)), m.group(2), rev(repo, m.group(3))
    if dots == "..." or not is_ancestor(repo, a, b):
        mbs = repo.lines(["merge-base", a, b])
        if not mbs:
            raise ResolveError("range '%s': 공통 조상이 없습니다." % spec)
        if dots == "..":
            warnings.append("range '%s': 시작점이 끝점의 조상이 아니어서 merge-base(%s)를 시작점으로 사용합니다."
                            % (spec, short(mbs[0])))
        a = mbs[0]
    return _seg(a, b, "range %s" % spec)


def _resolve_commit(repo, name, notes):
    sha = rev(repo, name)
    if len(parents(repo, sha)) > 1:
        notes.append("%s 는 머지 커밋 -> first-parent diff (PR 전체 변경분) 사용" % short(sha))
    return _seg(first_parent(repo, sha), sha, "commit %s" % name)


def _resolve_grep(repo, pattern, head, base, notes, warnings):
    head_sha = rev(repo, head or "HEAD")
    limit = ["^" + rev(repo, base)] if base else []
    matched = repo.lines(["rev-list", "--reverse", "--fixed-strings", "--grep=" + pattern,
                          head_sha] + limit)
    if not matched:
        raise ResolveError("커밋 메시지에 '%s' 가 포함된 커밋이 없습니다." % pattern)
    fp = repo.lines(["rev-list", "--first-parent", head_sha] + limit)
    pos = dict((sha, i) for i, sha in enumerate(fp))
    units = {}
    for c in matched:
        u = _mainline_unit(repo, c, fp, head_sha)
        if u is None:
            warnings.append("커밋 %s 의 mainline 반영 지점을 찾지 못해 제외했습니다." % short(c))
            continue
        units.setdefault(u, []).append(c)
    ordered = sorted(units, key=lambda u: -pos[u])       # 과거 -> 최신
    notes.append("grep '%s': 커밋 %d개 매칭 -> mainline 단위 %d개"
                 % (pattern, len(matched), len(ordered)))
    for u in ordered:
        if len(parents(repo, u)) > 1 and u not in units[u]:
            notes.append("머지 커밋 %s 는 메시지가 매칭되지 않았지만 매칭된 커밋을 포함하므로 "
                         "PR 전체(%s^1..%s)를 사용합니다." % (short(u), short(u), short(u)))
    segs = []
    for u in ordered:
        p = first_parent(repo, u)
        if segs and segs[-1]["head"] == p:               # 연속된 단위는 하나의 구간으로
            segs[-1]["head"] = u
        else:
            segs.append(_seg(p, u, ""))
    for s in segs:
        s["how"] = "grep '%s': %s..%s" % (pattern, short(s["base"]), short(s["head"]))
    return segs


def resolve_side(repo, head=None, base=None, commits=None, ranges=None, grep=None):
    """한쪽(원본 또는 횡전개 대상)의 diff 구간을 결정한다.

    우선순위: ranges/commits (명시) > grep > head/base (PR 모드)
    반환: {"mode", "segments", "notes", "warnings"}
    """
    notes, warnings = [], []
    if ranges or commits:
        if grep:
            raise ResolveError("range/commit 과 grep 은 함께 쓸 수 없습니다.")
        segs = [_resolve_range(repo, r, warnings) for r in (ranges or [])]
        segs += [_resolve_commit(repo, c, notes) for c in (commits or [])]
        mode = "explicit"
    elif grep:
        segs = _resolve_grep(repo, grep, head, base, notes, warnings)
        mode = "grep"
    else:
        mode, segs = _resolve_pr(repo, head or "HEAD", base, notes, warnings)

    for a, b in zip(segs, segs[1:]):
        if a["head"] != b["base"] and not is_ancestor(repo, a["head"], b["head"]):
            warnings.append("구간 순서를 확인하세요 (과거 -> 최신 순으로 지정): %s 다음에 %s"
                            % (a["how"], b["how"]))
    return {"mode": mode, "segments": segs, "notes": notes, "warnings": warnings}
