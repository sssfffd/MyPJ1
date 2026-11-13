"""결정된 구간(segment)에서 커밋 정보와 파일별 diff 를 수집한다."""

import os
import re
import shutil
import tempfile

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
REVERT_RE = re.compile(r"This reverts commit ([0-9a-f]{7,64})")
STATUS_NAMES = {"A": "added", "D": "deleted", "M": "modified", "R": "renamed",
                "C": "copied", "T": "type-changed"}


class DiffOptions(object):
    def __init__(self, context=3, function_context=False, ignore_space=False,
                 pathspecs=None, encodings=None, include_body=False):
        self.context = context
        self.function_context = function_context
        self.ignore_space = ignore_space
        self.pathspecs = list(pathspecs or [])
        self.encodings = list(encodings or ["utf-8", "cp949"])
        self.include_body = include_body

    def diff_flags(self):
        flags = ["-M", "--no-color", "--no-ext-diff", "--no-textconv",
                 "--src-prefix=a/", "--dst-prefix=b/"]
        if self.ignore_space:
            flags.append("-b")
        return flags

    def tail(self):
        return (["--"] + self.pathspecs) if self.pathspecs else []


def _decode(data, encodings):
    for enc in encodings:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            pass
    return data.decode("utf-8", "replace")


# ---------------------------------------------------------------- commits

def list_commits(repo, base, head, include_body=False):
    """base..head 의 커밋 목록 (과거 -> 최신)."""
    fmt = "%H%x1f%P%x1f%an%x1f%aI%x1f%s%x1f%b%x1e"
    out = repo.run(["log", "--reverse", "--format=" + fmt, head, "^" + base]
                   if repo.ok(["cat-file", "-e", base + "^{commit}"])
                   else ["log", "--reverse", "--format=" + fmt, head])
    commits = []
    for rec in out.decode("utf-8", "replace").split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, ps, author, date, subject, body = (rec.split("\x1f") + [""] * 6)[:6]
        c = {"sha": sha, "parents": ps.split(), "is_merge": len(ps.split()) > 1,
             "author": author, "date": date, "subject": subject,
             "reverts": REVERT_RE.findall(body), "patch_id": None}
        if include_body:
            c["body"] = body.strip()
        commits.append(c)
    return commits


def patch_ids(repo, base, head):
    """{commit sha: patch-id}. 내용이 같은 커밋(cherry-pick, 중복 머지)은 patch-id 가 같다."""
    rng = [head, "^" + base] if repo.ok(["cat-file", "-e", base + "^{commit}"]) else [head]
    log = repo.run(["log", "-p", "--no-merges", "--no-color", "--no-ext-diff",
                    "--no-textconv", "--format=commit %H"] + rng)
    code, out, _ = repo.run_raw(["patch-id", "--stable"], input=log)
    result = {}
    if code == 0:
        for line in out.decode().splitlines():
            parts = line.split()
            if len(parts) == 2:
                result[parts[1]] = parts[0]
    return result


def already_upstream(repo, base_ref, head):
    """head 쪽 커밋 중 동일 patch 가 이미 base_ref 에 존재하는 것 (git cherry 의 '-')."""
    code, out, _ = repo.run_raw(["cherry", base_ref, head])
    if code != 0:
        return []
    return [l[2:] for l in out.decode().splitlines() if l.startswith("- ")]


# ---------------------------------------------------------------- diff

def _parse_name_status(data):
    toks = data.decode("utf-8", "replace").split("\0")
    entries, i = [], 0
    while i < len(toks) and toks[i]:
        st = toks[i]
        if st[0] in "RC":
            entries.append((st[0], toks[i + 1], toks[i + 2], st[1:]))
            i += 3
        else:
            entries.append((st[0], None, toks[i + 1], ""))
            i += 2
    return entries


def _parse_numstat(data):
    toks = data.decode("utf-8", "replace").split("\0")
    stats, i = [], 0
    while i < len(toks) and toks[i]:
        add, dele, path = toks[i].split("\t", 2)
        i += 1
        if path == "":           # rename/copy: 뒤에 old, new 가 따라온다
            i += 2
        stats.append((None, None) if add == "-" else (int(add), int(dele)))
    return stats


def _parse_file_patch(text):
    """파일 하나의 patch 텍스트 -> (header lines, hunks, binary 여부)."""
    header, hunks, binary, cur = [], [], False, None
    for line in text.split("\n"):
        line = line.rstrip("\r")
        m = HUNK_RE.match(line)
        if m:
            cur = {"old_start": int(m.group(1)),
                   "old_lines": int(m.group(2)) if m.group(2) is not None else 1,
                   "new_start": int(m.group(3)),
                   "new_lines": int(m.group(4)) if m.group(4) is not None else 1,
                   "section": m.group(5), "lines": []}
            hunks.append(cur)
        elif cur is None:
            if line.startswith("Binary files ") or line == "GIT binary patch":
                binary = True
            if line:
                header.append(line)
        elif line[:1] in (" ", "+", "-", "\\"):
            cur["lines"].append(line)
    return header, hunks, binary


def diff_files(repo, base, head, opts, env=None):
    """base 와 head(커밋 또는 tree) 사이의 파일별 diff 목록."""
    common = opts.diff_flags()
    names = _parse_name_status(repo.run(["diff", "--name-status", "-z"] + common
                                        + [base, head] + opts.tail(), env=env))
    stats = _parse_numstat(repo.run(["diff", "--numstat", "-z"] + common
                                    + [base, head] + opts.tail(), env=env))
    ctx = ["-W"] if opts.function_context else []
    raw = repo.run(["diff", "-U%d" % opts.context] + ctx + common
                   + [base, head] + opts.tail(), env=env)
    chunks = [c for c in re.split(br"(?m)^(?=diff --git )", raw) if c]
    if not (len(names) == len(stats) == len(chunks)):
        raise RuntimeError("diff 출력 파싱 불일치 (files=%d, numstat=%d, patches=%d)"
                           % (len(names), len(stats), len(chunks)))
    files = []
    for (st, old, new, score), (add, dele), chunk in zip(names, stats, chunks):
        header, hunks, binary = _parse_file_patch(_decode(chunk, opts.encodings))
        mode_change = [h for h in header if h.startswith(("old mode", "new mode"))]
        files.append({
            "path": new, "old_path": old,
            "status": STATUS_NAMES.get(st, st),
            "similarity": int(score) if score else None,     # rename/copy 유사도(%)
            "binary": binary or add is None,
            "mode_change": mode_change or None,
            "additions": add or 0, "deletions": dele or 0,
            "hunks": hunks,
        })
    return files


# ---------------------------------------------------------------- 여러 구간 합성

def _compose_net(repo, segments, opts):
    """떨어져 있는 여러 구간을 하나의 net diff 로 합성한다.

    임시 index / 임시 object 디렉터리에서만 작업하므로 레포(작업트리, index, object DB)를
    건드리지 않는다. 실패하면 None.
    """
    tmp = tempfile.mkdtemp(prefix="crossreview-")
    try:
        objdir = repo.text(["rev-parse", "--git-path", "objects"])
        objdir = os.path.normpath(os.path.join(repo.path, objdir))
        os.mkdir(os.path.join(tmp, "objects"))
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index"),
               "GIT_OBJECT_DIRECTORY": os.path.join(tmp, "objects"),
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": objdir}
        start = segments[0]["base"]
        repo.run(["read-tree", start], env=env)
        for seg in segments:
            patch = repo.run(["diff", "--binary", "--full-index", "-M", "--no-color",
                              "--no-ext-diff", "--no-textconv", seg["base"], seg["head"]]
                             + opts.tail())
            if not patch:
                continue
            for extra in ([], ["--3way"]):       # 그대로 적용 -> 안 되면 3-way
                if repo.run_raw(["apply", "--cached", "--whitespace=nowarn"] + extra,
                                input=patch, env=env)[0] == 0:
                    break
            else:
                return None
        code, out, _ = repo.run_raw(["write-tree"], env=env)   # 충돌(unmerged)이 남으면 실패
        if code != 0:
            return None
        return diff_files(repo, start, out.decode().strip(), opts, env=env)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _merge_sequential(per_segment):
    """합성 실패 시 대안: 같은 파일의 hunk 를 구간 순서대로 이어 붙인다."""
    merged, index = [], {}
    for i, files in enumerate(per_segment):
        for f in files:
            for h in f["hunks"]:
                h["segment"] = i
            key = f["old_path"] or f["path"]
            if key in index:
                g = index.pop(key)
                g["hunks"] += f["hunks"]
                g["additions"] += f["additions"]
                g["deletions"] += f["deletions"]
                g["binary"] = g["binary"] or f["binary"]
                if f["status"] != g["status"]:
                    g["status"] = f["status"] if f["status"] in ("deleted", "renamed") else g["status"]
                g["path"] = f["path"]
            else:
                g = f
                merged.append(g)
            index[g["path"]] = g
    return merged


# ---------------------------------------------------------------- side

def collect_side(repo, resolution, opts=None, label=None, base_ref=None):
    """resolve_side() 결과를 받아 한쪽의 전체 데이터를 만든다."""
    opts = opts or DiffOptions()
    segs = resolution["segments"]
    notes, warnings = list(resolution["notes"]), list(resolution["warnings"])

    out_segs, all_commits = [], []
    for seg in segs:
        commits = list_commits(repo, seg["base"], seg["head"], opts.include_body)
        ids = patch_ids(repo, seg["base"], seg["head"])
        for c in commits:
            c["patch_id"] = ids.get(c["sha"])
        all_commits += commits
        out_segs.append(dict(seg, commits=commits))

    # --- 구간 상태 진단
    plain = [c for c in all_commits if not c["is_merge"]]
    if len(plain) > 1:
        notes.append("커밋 %d개 -> 커밋별 diff 가 아니라 구간 전체의 net diff 를 사용합니다." % len(plain))
    if any(c["is_merge"] for c in all_commits) and resolution["mode"] == "pr":
        notes.append("구간 안에 머지 커밋이 있습니다 (base 를 PR 브랜치로 머지). "
                     "merge-base 기준 diff 라 base 쪽 변경은 포함되지 않습니다.")
    shas = set(c["sha"] for c in all_commits)
    for c in all_commits:
        for r in c["reverts"]:
            hit = [s for s in shas if s.startswith(r)]
            notes.append("revert 커밋 %s (%s 를 되돌림%s)" % (
                c["sha"][:10], r[:10],
                ", 대상도 구간 안에 있어 net diff 에서 상쇄됨" if hit else ", 대상은 구간 밖"))
    seen = {}
    for c in plain:
        if c["patch_id"]:
            seen.setdefault(c["patch_id"], []).append(c["sha"][:10])
    for pid, group in seen.items():
        if len(group) > 1:
            warnings.append("동일한 내용의 커밋이 중복되어 있습니다 (중복 push/머지 의심): %s"
                            % ", ".join(group))
    if base_ref and resolution["mode"] == "pr":
        dup = already_upstream(repo, base_ref, segs[-1]["head"])
        if dup:
            warnings.append("동일 patch 가 이미 '%s' 에 존재하는 커밋: %s (중복 PR/머지 의심)"
                            % (base_ref, ", ".join(s[:10] for s in dup)))

    # --- diff 수집
    chained = all(a["head"] == b["base"] for a, b in zip(segs, segs[1:]))
    if chained:
        composed = "direct"
        files = diff_files(repo, segs[0]["base"], segs[-1]["head"], opts)
    else:
        files = _compose_net(repo, segs, opts)
        if files is not None:
            composed = "net"
            notes.append("떨어져 있는 구간 %d개를 하나의 net diff 로 합성했습니다." % len(segs))
        else:
            composed = "sequential"
            warnings.append("구간 합성 실패(사이에 낀 다른 변경과 충돌). 구간별 hunk 를 순서대로 "
                            "나열했습니다. 같은 위치를 여러 번 고친 경우 뒤 구간이 최종입니다.")
            files = _merge_sequential([diff_files(repo, s["base"], s["head"], opts) for s in segs])

    return {
        "label": label, "repo": repo.path, "mode": resolution["mode"],
        "composed": composed, "segments": out_segs,
        "notes": notes, "warnings": warnings,
        "stats": {"files": len(files),
                  "additions": sum(f["additions"] for f in files),
                  "deletions": sum(f["deletions"] for f in files)},
        "files": files,
    }
