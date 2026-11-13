# -*- coding: utf-8 -*-
"""LLM 훅 예시. 이 파일을 llm_hook.py 로 복사해서 review() 안을 사내 LLM 호출로 바꾸세요.

crossreview 는 다음 순서로 훅을 찾습니다.
  --llm-hook 경로  >  환경변수 CROSSREVIEW_LLM_HOOK  >  실행한 폴더의 llm_hook.py  >  crossreview.pyz 옆의 llm_hook.py

request (dict)
  prompt        기본 지시문 + 구간 경고 + 원본 diff + 대상 diff + 추가 프롬프트를 합친 텍스트
  instruction   기본 지시문만
  extra_prompt  UI 의 "추가 프롬프트" 칸에 적은 내용
  source_patch / target_patch   파일 1개의 unified diff 텍스트 (없으면 "")
  source_file / target_file     hunk 단위 구조화 데이터 (없으면 None)
  pair          {"kind", "source_path", "target_path", "identical", "similarity", ...}
  source_info / target_info     구간 정보 (mode, notes, warnings, 커밋 제목)

반환값: UI 에 표시하고 파일로 저장할 문자열
"""


def review(request):
    # 예) 표준 라이브러리만으로 사내 LLM HTTP API 호출
    #
    # import json, urllib.request
    # body = json.dumps({"model": "...", "messages": [{"role": "user", "content": request["prompt"]}]})
    # req = urllib.request.Request("http://llm.internal/v1/chat/completions", body.encode("utf-8"),
    #                              {"Content-Type": "application/json"})
    # with urllib.request.urlopen(req, timeout=300) as r:
    #     return json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]

    pair = request["pair"]
    return ("(예시 훅) %s <-> %s\n프롬프트 %d자를 받았습니다. review() 를 실제 LLM 호출로 바꾸세요."
            % (pair["source_path"], pair["target_path"], len(request["prompt"])))
