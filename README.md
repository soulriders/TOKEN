# ChatGPT ↔ Gemini 웹 구독 중계기 (DB + Turn Token)

API 없이 웹 구독(브라우저) 기반으로 두 모델의 응답을 번갈아 중계할 때 사용할 수 있는 로컬 오케스트레이터 골격입니다.

> 주의: 각 서비스의 이용약관/자동화 정책을 반드시 확인하세요. 이 저장소는 상태관리(토큰/로그) 중심의 예시이며, 실제 브라우저 자동화는 정책 준수 범위에서 구현해야 합니다.

## 핵심 아이디어

- `SQLite`에 대화 로그와 현재 차례(`current_turn`)를 저장
- 워커 A/B(예: `gemini_worker.py`, `chatgpt_worker.py`)는 DB를 폴링
- 자신의 차례일 때만 마지막 상대 메시지를 읽고 응답 생성
- 응답 저장 후 차례를 상대에게 넘김

## 빠른 시작

```bash
python3 orchestrator.py init --first-turn GEMINI --seed "토론 주제: AI 자동화의 윤리적 한계"
python3 orchestrator.py status
python3 orchestrator.py pull --worker GEMINI
python3 orchestrator.py push --worker GEMINI --message "Gemini의 첫 응답"
python3 orchestrator.py pull --worker CHATGPT
python3 orchestrator.py push --worker CHATGPT --message "ChatGPT의 반론"
python3 orchestrator.py export --format markdown --output dialogue.md
```

## 워커 동작 규약

1. `pull --worker <GEMINI|CHATGPT>`
   - 자신의 차례면 입력 메시지를 반환
   - 아니면 `WAIT` 반환
2. 응답을 만들었으면 `push --worker <...> --message "..."`
   - 메시지 로그 저장
   - 차례를 상대에게 토글

## 스키마

- `state`
  - `id=1` 고정
  - `current_turn`: `GEMINI | CHATGPT`
  - `turn_count`: 누적 turn 수
  - `max_turns`: 종료 제한
  - `status`: `running | finished`
- `messages`
  - `sender`: `SYSTEM | GEMINI | CHATGPT`
  - `content`: 본문
  - `created_at`: ISO timestamp

## 확장 포인트

- Playwright/Selenium 워커에서 `pull/push` 호출
- Redis로 교체 시 `state`를 키-값으로 매핑
- `push` 시 금칙어/종료키워드 감지 훅 추가

## GitHub에 올릴 수 있나요?

네, 가능합니다. 이 저장소는 일반 Python/SQLite 스크립트 기반이라 GitHub에 그대로 업로드해도 됩니다.

권장 절차:

```bash
git init
git add README.md orchestrator.py .gitignore
git commit -m "Add local orchestrator"
git branch -M main
git remote add origin <YOUR_GITHUB_REPO_URL>
git push -u origin main
```

업로드 시 주의:

- `orchestrator.db`, `dialogue.md` 같은 실행 산출물은 `.gitignore`로 제외
- 쿠키/세션 파일, 계정 정보, 자동화 도중 저장된 민감 데이터는 커밋 금지
- 서비스 자동화는 각 플랫폼 이용약관을 먼저 확인
