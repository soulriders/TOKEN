# ChatGPT ↔ Gemini 웹 브리지

로컬 PC에서 하나의 Chrome 창과 하나의 공유 프로필을 사용해, ChatGPT 웹과 Gemini 웹이 번갈아 대화하도록 만드는 자동화 도구입니다.

구성은 세 층입니다.

- `orchestrator.py`: SQLite 기반 턴 상태와 대화 로그 관리
- `web_bridge.py`: Playwright로 실제 웹앱 조작
- `dashboard_server.py` + `dashboard/index.html`: 로컬 HTML 대시보드

## 전제 조건

- Windows 로컬 PC
- Python 3.10+
- Chrome 설치
- ChatGPT 웹, Gemini 웹에 각각 로그인 가능한 계정
- 브라우저 자동화 사용은 각 서비스 정책을 스스로 확인하고 감수할 것

## 설치

```powershell
python -m pip install -r requirements.txt
python -m playwright install
```

## 대시보드 실행

```powershell
python dashboard_server.py --config bridge_config.json --host 127.0.0.1 --port 8765
```

브라우저에서 아래 주소를 엽니다.

- [http://127.0.0.1:8765](http://127.0.0.1:8765)

## 사용 방식

이제 기본 동작은 다음과 같습니다.

- 하나의 Chrome 창만 사용
- `profiles/shared` 하나만 사용
- ChatGPT 탭 1개, Gemini 탭 1개를 같은 창 안에 유지
- 두 탭을 번갈아 사용해 대화를 진행

## 사용 순서

1. 대시보드 실행
2. `Provider Login`에서 `ChatGPT 브라우저 열기`
3. 열린 공유 Chrome 창의 ChatGPT 탭에서 로그인
4. 대시보드에서 `설정 완료`
5. `Gemini 브라우저 열기`
6. 같은 공유 Chrome 창의 Gemini 탭에서 로그인
7. 대시보드에서 `설정 완료`
8. seed와 turn 설정 입력
9. `실행`

## 수동 CLI도 가능

```powershell
python web_bridge.py --config bridge_config.json setup --provider CHATGPT
python web_bridge.py --config bridge_config.json setup --provider GEMINI
python web_bridge.py --config bridge_config.json run --first-turn GEMINI --max-turns 10 --seed "토론 주제: AI 자동화의 윤리적 한계"
```

## 주요 파일

- `bridge_config.json`: 실제 런타임 설정
- `bridge_config.example.json`: 예시 설정
- `profiles/shared`: 공유 Chrome 프로필
- `orchestrator.db`: 현재 상태 DB
- `dialogue.md`: export 결과
- `artifacts/`: 실패 시 스크린샷

## 실패 시 점검

- 사이트 DOM이 바뀌면 `bridge_config.json`의 selector 목록 수정
- 로그인 세션이 풀리면 대시보드에서 다시 브라우저 열기
- 응답이 너무 늦으면 `response_timeout_seconds`, `stability_window_seconds` 증가
- 실행 오류는 대시보드 로그와 `artifacts/` 스크린샷 확인

## 주의

- 이 도구는 API가 아니라 웹 UI 자동화입니다.
- 사이트 구조 변경에 따라 셀렉터를 주기적으로 조정해야 할 수 있습니다.
- 일반 브라우징 프로필과 섞어 쓰지 말고 `profiles/shared` 전용 프로필을 유지하는 편이 안전합니다.
