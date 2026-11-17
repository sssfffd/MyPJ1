# crossreview — 횡전개 코드 리뷰

동일한 기능을 다른 버전/레포/브랜치에 다시 개발(횡전개)했을 때,
원본 쪽 변경과 대상 쪽 변경을 웹 화면에서 나란히 비교하고 LLM 에 리뷰를 맡기는 도구입니다.

```
crossreview.py   내부 로직 전부 (git / diff / 비교 / 저장 / 웹 서버 / 빌드)
ui.html          웹 화면
```

표준 라이브러리 + git 만 사용합니다 (Python 3.7 이상).

## 실행

```bash
python crossreview.py
```

브라우저가 자동으로 열립니다. 콘솔에 접속 주소가 나오고, 다른 PC 에서는 `http://이PC의IP:8765/` 로 접속합니다.

```
crossreview 실행 중   (종료: Ctrl+C 또는 이 창 닫기)
  이 PC      : http://127.0.0.1:8765/
  다른 PC    : http://192.168.0.15:8765/
```

다른 PC 에서 접속이 안 되면 실행한 PC 의 방화벽에서 TCP 8765 인바운드를 허용하세요.

## 빌드 (exe)

```bash
python crossreview.py build
```

`dist/crossreview.exe` 하나가 만들어집니다 (ui.html 포함). 빌드하는 PC 에만 PyInstaller 가 필요합니다
(`pip install pyinstaller`). Windows 에서 빌드하면 Windows 용 exe 가 나옵니다.

## 화면 사용법

1. 원본 / 대상 **레포**를 고릅니다. 작업 브랜치와 기준 브랜치는 자동으로 선택됩니다.
2. **비교**를 누릅니다.
3. 왼쪽 파일 목록에서 파일을 고르면 원본·대상 변경이 나란히 보입니다.
   - 초록/빨강: 추가/삭제, **노란 줄: 반대쪽에 없는 변경**, **주황 글자: 실제로 달라진 부분**
   - "▼ 다음 차이" (단축키 n / p) 로 노란 줄만 따라가기, ↑↓ 로 파일 이동
4. 오른쪽 **LLM 리뷰** 패널에서 프롬프트 / 추가 요청을 적고 "이 파일 분석" 또는 "차이 있는 파일 모두".

⚙ 설정: 레포를 찾을 폴더, 결과 저장 폴더 (실행한 PC 에서만 변경 가능, `settings.json` 에 저장).

## 비교 방식

각 쪽에서 `git diff 기준브랜치...작업브랜치` — 기준 브랜치에서 갈라진 지점부터 작업 브랜치까지의 변경 전체입니다.
작업 브랜치에 커밋이 여러 개여도 PR 전체가 한 번에 잡힙니다.
원본과 대상 파일은 같은 경로, 없으면 같은 파일명(양쪽에 하나뿐일 때)으로 짝짓습니다.

브랜치 자동 선택 규칙
- 기준: `origin/HEAD` 가 가리키는 브랜치 → `origin/main` → `origin/master` → `main` → `master` → `develop`
- 작업: 현재 체크아웃된 브랜치. 이미 기준에 다 들어간 브랜치(main 등)가 체크아웃돼 있으면 기준에 아직 머지되지 않은 가장 최근 브랜치

## 결과 파일

비교할 때마다 자동 저장됩니다. 화면의 "이전 결과…" 로 다시 열 수 있습니다.

```
crossreview_out/20261117_083808_v1_vs_v2/
  result.json        전체 데이터 (화면이 다시 열 때 사용)
  source.diff        원본 전체 diff
  target.diff        대상 전체 diff
  llm/01_app.c.md    LLM 결과 + 보낸 프롬프트
```

## 이어서 개발할 곳 (crossreview.py)

| 위치 | 내용 |
|---|---|
| `call_llm(prompt)` | **사내 LLM 호출을 구현하는 곳.** 프롬프트 문자열을 받아 응답 문자열을 반환 |
| `DEFAULT_PROMPT` / `build_prompt()` | 기본 프롬프트와 diff 를 채워 넣는 방식 |
| `HOST` / `PORT` / `OPEN_BROWSER` | 접속 범위(다른 PC 허용 여부), 포트 |
| `collect_diff()` | diff 를 뽑는 방식 |
| `pair_files()` | 원본·대상 파일 짝짓기 |
| `API` | 화면이 호출하는 API 목록 (`POST /api/<이름>`, JSON) |

화면에서 쓰는 데이터 구조는 `compare()` 의 반환값(`result.json` 과 동일)입니다.
