# crossreview — 횡전개 코드 리뷰용 diff 수집기

동일한 기능을 다른 버전/레포/브랜치에 다시 개발(횡전개)했을 때,
**"원본에서 바뀐 것"** 과 **"횡전개한 쪽에서 바뀐 것"** 을 각각 뽑아 짝지어 주는 도구입니다.
결과(JSON)를 사내 LLM 에 넘겨 "동일하게 개발되었는가" 를 분석하는 용도이며, LLM 호출부는 포함하지 않습니다.

- Python 표준 라이브러리 + `git` CLI 만 사용 (외부 패키지 0개)
- 레포를 수정하지 않음 (checkout / index / object DB 모두 건드리지 않음)
- 양쪽 모두 **로컬 clone** 이 필요 (같은 레포의 다른 브랜치면 레포 하나로 충분)

```
원본  (src): base ──●──●──●  head      → diff S
대상  (dst): base ──●──●     head      → diff T
                                          S 와 T 를 파일 단위로 짝지어 출력
```

## 빌드 / 실행

빌드는 한 줄이고 외부 패키지가 필요 없습니다 (표준 라이브러리 `zipapp`).

```bash
python build.py          # -> dist/crossreview.pyz (단일 파일), dist/crossreview.bat
```

`crossreview.pyz` 파일 하나만 복사하면 Python(3.6+)과 git 이 있는 PC 어디서든 실행됩니다.

```bash
python crossreview.pyz                 # 인자 없이 실행 -> UI (Windows 는 crossreview.bat 더블클릭)
python crossreview.pyz --src-... -o bundle.json     # CLI
```

빌드 없이 소스에서 바로 실행하려면 `python -m crossreview`.

## UI

```bash
python crossreview.pyz                         # UI 실행 (Edge/Chrome 이 있으면 주소창 없는 앱 창으로 열림)
python crossreview.pyz --src-... --dst-... --ui      # CLI 로 추출한 결과를 바로 화면으로
python crossreview.pyz --src-... --dst-... --html report.html   # 서버 없이 열리는 HTML 1개 (공유용)
```

**추출 설정**
- 레포: "폴더 선택…" 으로 폴더를 탐색해 고르거나(git 레포는 `git` 표시), 최근 사용한 레포 목록에서 선택
- base / head: 레포를 고르면 원격·로컬 브랜치와 태그가 선택 상자에 채워짐 (직접 입력도 가능)
- commit: head 기준 최근 커밋 100개에서 골라 추가 (머지 커밋은 `[merge]` 표시)
- "diff 추출" 을 누르면 화면에 표시되고 동시에 **파일로 저장**됨 (아래 "저장되는 파일")

**diff 비교**
- 왼쪽 목록: `동일`(초록) / 유사도 %(노랑) / `원본만`·`대상만`(빨강), 필터·검색, ↑/↓ 또는 j/k 로 이동
- 오른쪽: 원본 diff 와 대상 diff 를 나란히 표시
  - 초록 = 추가, 빨강 = 삭제 (양쪽에 똑같이 있는 변경)
  - **노란 줄 = 반대쪽에 없는 변경**, 그 안의 **주황 글자 = 비슷한 줄과 비교해 실제로 달라진 부분**
  - "▼ 다음 차이 / ▲ 이전 차이" (단축키 n / p) 로 노란 줄만 순서대로 이동
- "구간 정보": 어떤 구간을 어떻게 잡았는지(mode, 커밋 목록, note, warning) — warning 이 있으면 자동으로 펼쳐짐

**LLM 리뷰 패널** (화면 아래)
- "추가 프롬프트" 칸에 적은 내용이 기본 지시문 + diff 뒤에 붙어 전달됨
- "이 파일 분석" / "차이 있는 파일 전체 분석"(동일한 파일은 건너뜀, 중지 가능)
- 결과는 파일별로 표시되고 목록에 `LLM` 표시가 붙으며, 자동으로 `llm/*.md` 로 저장됨
- LLM 호출 자체는 **훅 파일**이 담당합니다 → 아래 "LLM 연결"

UI 는 표준 라이브러리 `http.server` 로 **127.0.0.1 에만** 뜨는 로컬 화면입니다 (외부 접속 불가, 외부 리소스 로드 없음, 요청 토큰/Host 검사).
팀이 함께 쓰려면 아래 "웹 서버 모드" 를 사용하세요. 옵션: `--port`(기본 8765, 사용 중이면 빈 포트), `--browser-tab`(앱 창 대신 브라우저 탭), `--no-browser`.

## 웹 서버 모드 (팀이 주소로 접속)

같은 프로그램을 서버 한 대에 띄워 두고 여러 명이 브라우저로 접속해 쓸 수 있습니다. 설치는 여전히 `crossreview.pyz` 하나입니다.

```bash
python crossreview.pyz --host 0.0.0.0 --port 8765 \
    --repo-root /srv/repos \          # 이 폴더 아래의 레포만 선택/추출 가능 (필수, 반복 가능)
    --auth team:비밀번호 \             # 접속 시 로그인 요구 (권장)
    --cert server.pem --key server.key \   # HTTPS (권장, 없으면 평문 HTTP)
    --out-dir /srv/crossreview_out      # 결과 저장 폴더
```

- 레포는 **서버에 있는 clone** 을 씁니다. `--repo-root` 아래에 대상 레포들을 clone 해 두고 주기적으로 `git fetch` 하세요 (예: cron / 작업 스케줄러). 화면의 폴더 선택도 이 폴더 밖으로는 나갈 수 없습니다.
- 추출할 때마다 실행 ID 가 붙은 폴더에 저장되고 주소가 `…/?run=실행ID` 로 바뀝니다. **"링크 복사"** 로 동료에게 그대로 공유할 수 있고, "이전 결과 열기…" 목록에서 다시 열 수 있습니다. 결과는 실행별로 분리되어 여러 명이 동시에 써도 섞이지 않습니다.
- LLM 훅(`llm_hook.py`)과 결과 파일은 서버 쪽에 있습니다.
- `--host` 없이 실행하면 기존처럼 내 PC 전용(127.0.0.1)입니다.

보안상 알아둘 점: `--auth` 는 공용 계정 1개짜리 HTTP Basic 인증이고 사용자별 권한 구분은 없습니다.
접속한 사람은 `--repo-root` 아래 모든 레포의 diff 를 볼 수 있으니, 그래도 되는 레포만 두세요. `--auth` 나 HTTPS 없이 띄우면 시작할 때 경고가 출력됩니다.

## LLM 연결 (llm_hook.py)

`llm_hook_example.py` 를 `llm_hook.py` 로 복사해 `review()` 안에 사내 LLM 호출을 넣으면 됩니다.

```python
def review(request):
    # request["prompt"]        기본 지시문 + 구간 경고 + 원본 diff + 대상 diff + 추가 프롬프트
    # request["extra_prompt"]  UI 에 적은 추가 프롬프트만
    # request["source_patch"], request["target_patch"], request["pair"], ... (예시 파일 참고)
    return "LLM 응답 텍스트"
```

- 찾는 순서: `--llm-hook 경로` → 환경변수 `CROSSREVIEW_LLM_HOOK` → 실행한 폴더의 `llm_hook.py` → `crossreview.pyz` 옆의 `llm_hook.py`
- 호출할 때마다 파일을 새로 읽으므로 훅을 고친 뒤 프로그램을 다시 띄울 필요가 없습니다.
- 훅이 없으면 "이 파일 분석" 을 눌렀을 때 **전송될 프롬프트 미리보기**가 표시됩니다.

## 저장되는 파일

UI 에서 추출하면 `저장 폴더/실행ID/` 아래에 (저장 폴더는 `--out-dir`, 기본 `./crossreview_out`), CLI 는 `--out-dir DIR` 을 주면 DIR 에 바로 저장합니다.

```
crossreview_out/20250101_093000_a1b2/
  meta.json                      목록 표시용 요약
  bundle.json                    전체 데이터 (LLM 결과 포함, "JSON 열기" 로 다시 볼 수 있음)
  report.html                    브라우저로 바로 열리는 리포트
  diffs/0001_app.c.source.diff   파일별 원본 diff
  diffs/0001_app.c.target.diff   파일별 대상 diff
  llm/0001_app.c.md              LLM 결과 (추가 프롬프트 + 응답)
```

## 사용법 (CLI)

```bash
# 서로 다른 레포(버전)의 PR 끼리 비교
python -m crossreview \
  --src-repo ../product-v1 --src-base origin/main --src-head feature/login \
  --dst-repo ../product-v2 --dst-base origin/main --dst-head feature/login \
  -o bundle.json --split-dir units/

# 같은 레포: 원본은 이미 머지된 머지 커밋, 대상은 지금 작업 중인 브랜치
python -m crossreview --src-commit 1a2b3c4 --dst-base origin/release-2.0 --dst-head HEAD

# 원본 기능이 여러 PR 에 걸쳐 들어간 경우: 이슈 키로 모으기
python -m crossreview --src-grep PROJ-123 --src-head origin/main \
                      --dst-base origin/release-2.0 --dst-head HEAD
```

진행 요약/경고는 stderr, JSON 은 `-o` 파일(또는 stdout)로 나옵니다. `--strict` 를 주면 경고가 있을 때 exit code 2.

### 구간 지정 (src / dst 각각)

| 옵션 | 의미 | 쓰는 상황 |
|---|---|---|
| `--X-base B --X-head H` | `merge-base(B, H)..H` | 일반적인 PR (기본) |
| `--X-commit C` (반복) | `C^1..C` | squash 머지 커밋, 머지 커밋(=PR 전체), 단일 커밋 |
| `--X-range A..B` (반복) | 구간 직접 지정 | rebase/fast-forward 머지 등 자동 판단이 안 될 때 |
| `--X-grep KEY` | 메시지에 KEY 가 있는 커밋 전부 | 기능이 여러 PR/커밋에 흩어져 있을 때 |

기타: `--path-map SRC=DST`(경로 prefix 매핑), `-U N`/`-W`(컨텍스트), `-b`(공백 무시),
`--X-pathspec`(경로 제한), `--encoding`(기본 utf-8 → cp949 순 시도).

## "직전 커밋과 비교하면 안 되는" 경우들과 처리 방식

핵심 원칙: **커밋 단위가 아니라 "기능이 mainline 에 만든 최종 변화(net diff)" 단위로 본다.**

| # | 상황 | 직전 커밋 비교 시 문제 | 이 도구의 처리 |
|---|---|---|---|
| 1 | PR 에 커밋이 여러 개 | 마지막 커밋만 보임 | `merge-base(base, head)..head` 한 구간으로 PR 전체 net diff. 중간의 WIP/오타수정은 상쇄되어 안 보임 |
| 2 | PR 도중 base 가 전진 / base 를 PR 브랜치로 머지 | 남의 변경이 섞임 | merge-base 기준이라 base 쪽 변경은 제외 (note 로 알림) |
| 3 | PR 에 추가 push / force-push | 이전 push 기준 diff 가 됨 | 항상 현재 head 와 merge-base 를 비교하므로 push 횟수와 무관 |
| 4 | 이미 머지된 PR | `base..head` 가 빈 diff | 머지 커밋 M 을 찾아 `M^1..M` 사용 (warning). ff/rebase 머지는 자동 판단 불가 → 에러로 `--X-range` 안내 |
| 5 | PR 이 2번 이상 머지됨 (본 PR + 수정 PR) | 한쪽만 보거나 사이의 무관한 변경이 섞임 | `--X-commit M1 --X-commit M2` 또는 `--X-grep KEY` → 떨어진 구간들을 **하나의 net diff 로 합성** (`composed: "net"`) |
| 6 | revert 후 재머지 | 원본+revert+재적용이 뒤섞임 | 5번과 동일하게 합성하면 revert 는 상쇄, 최종 내용만 남음 (note) |
| 7 | 같은 내용이 중복 커밋/중복 머지 | 변경이 두 번 보임 | `patch-id` 로 구간 내 중복 감지, `git cherry` 로 base 에 이미 있는 patch 감지 → warning |
| 8 | 로컬 base 가 오래됨 | 엉뚱한 merge-base | upstream 보다 뒤처져 있으면 note (`origin/<branch>` 지정 권장, 실행 전 `git fetch`) |
| 9 | criss-cross merge (merge-base 여러 개) | 기준 모호 | 첫 번째 사용 + warning |

5·6번의 합성은 임시 index 에 구간별 patch 를 차례로 적용하는 방식입니다.
사이에 낀 다른 사람의 변경과 같은 줄에서 충돌하면 합성할 수 없으므로
`composed: "sequential"` 로 떨어지고(warning), 같은 파일의 hunk 를 구간 순서대로 나열합니다
(각 hunk 에 `segment` 번호가 붙음).

**운영 권장**
- 커밋 메시지에 이슈 키를 넣는 규칙이 있으면 `--src-grep` 이 가장 견고합니다 (PR 이 몇 번 나뉘어도 수집됨).
- CI 에 넣을 때는 `--strict` 로 warning 을 사람이 확인하게 하세요.
- `mode`, `composed`, `segments[].how`, `notes`, `warnings` 를 LLM 프롬프트에도 같이 넣으면 오탐 설명에 도움이 됩니다.

## 출력 형식 (bundle.json)

```jsonc
{
  "schema_version": 1,
  "summary": {"paired": 2, "identical": 1, "source_only": 1, "target_only": 1, "warnings": 0},
  "source": {                       // "target" 도 같은 구조
    "label": "source", "repo": "/abs/path",
    "mode": "pr",                   // pr | merged-pr | merge-commit | explicit | grep
    "composed": "direct",           // direct | net(여러 구간 합성) | sequential(합성 실패)
    "segments": [{
      "base": "<sha>", "head": "<sha>", "how": "merge-base(main, feat)..feat",
      "commits": [{"sha", "parents", "is_merge", "author", "date", "subject", "reverts", "patch_id"}]
    }],
    "notes": [], "warnings": [],
    "stats": {"files": 3, "additions": 3, "deletions": 2},
    "files": [{
      "path": "src/app.c", "old_path": null,
      "status": "modified",         // added | deleted | modified | renamed | copied | type-changed
      "binary": false, "additions": 1, "deletions": 1,
      "hunks": [{"old_start": 2, "old_lines": 7, "new_start": 2, "new_lines": 7,
                 "section": "함수 시그니처 등", "lines": [" ctx", "-old", "+new"]}]
    }]
  },
  "pairs": [{
    "kind": "paired",               // paired | source_only(이식 누락 의심) | target_only(추가 변경)
    "match_by": "path",             // path | path_map | basename
    "source_path": "app.c", "target_path": "src/app.c",
    "identical": true,              // 추가/삭제 라인이 공백 제외 동일 → LLM 생략 후보
    "similarity": 1.0,              // 0~1, 낮을수록 우선 리뷰
    "status_mismatch": false
  }]
}
```

`--split-dir DIR` 를 주면 파일 짝마다 `0001.json` … 을 따로 씁니다
(`pair`, `source_file`, `target_file`, 그리고 프롬프트에 바로 넣을 수 있는 `source_patch` / `target_patch` 텍스트).
`--skip-identical` 로 동일한 짝은 제외할 수 있습니다.

## Python 에서 직접 쓰기

```python
from crossreview.bundle import build_side, build_bundle, iter_review_units, file_patch_text

src = build_side("../v1", "source", head="feature/login", base="origin/main")
dst = build_side("../v2", "target", head="feature/login", base="origin/main")
bundle = build_bundle(src, dst, path_maps=[("src/v1/", "src/v2/")])

for pair, s, d in iter_review_units(bundle, skip_identical=True):
    prompt = "원본:\n%s\n횡전개:\n%s" % (file_patch_text(s), file_patch_text(d))
    # -> 사내 LLM 호출
```

## 구조 / 테스트

```
crossreview/
  gitcmd.py    git subprocess 래퍼
  resolve.py   어떤 구간을 diff 할지 결정 (위 표의 로직)
  collect.py   커밋 목록, 파일별 diff 파싱, 여러 구간 합성
  pairing.py   src/dst 파일 짝짓기, 동일 여부/유사도
  bundle.py    bundle 조립, LLM 용 헬퍼
  cli.py       CLI
  server.py    로컬 UI 서버 (추출 / 레포·브랜치 목록 / LLM 호출 API)
  export.py    파일 저장 (bundle.json, report.html, diffs/, llm/)
  llm.py       프롬프트 조립, llm_hook.py 호출
  ui.html      UI 화면 (단일 파일, 외부 리소스 없음)
llm_hook_example.py   LLM 훅 예시
build.py       단일 파일(dist/crossreview.pyz) 빌드
tests/         임시 git 레포로 위 시나리오를 실제 검증
```

```bash
python -m unittest discover -s tests -v
```

## 알려진 한계

- 파일 짝짓기는 경로 → `--path-map` → 유일한 파일명 순입니다. 파일이 분할/병합된 이식은 `source_only` / `target_only` 로 나옵니다.
- fast-forward / rebase 로 머지된 PR 은 히스토리에 PR 경계가 남지 않아 `--X-range` 또는 `--X-grep` 이 필요합니다.
- `--X-grep` 이 PR 내부 커밋에 매칭되면 그 PR 의 머지 커밋 전체를 단위로 잡습니다 (note 로 표시).
