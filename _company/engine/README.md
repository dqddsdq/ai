# 🤖 1인 기업 AI 엔진

`_agents/` 폴더의 에이전트들(CEO, 유튜브, 비서, 개발자 등)을 실제로 대화하고 일하게 만드는 엔진입니다.
여러 AI 회사 모델을 자유롭게 바꿔 쓸 수 있습니다 (중국계 모델은 차단).

## 시작하기

```bash
cd _company
pip install -r requirements.txt
cp .env.example .env      # 쓰고 싶은 AI 키 채우기
python -m engine models   # 어떤 AI가 준비됐는지 확인
python -m engine chat     # 대화 시작!
```

## 사용법

| 명령 | 설명 |
|---|---|
| `python -m engine chat` | 대화. 첫 질문을 보고 CEO가 담당 에이전트를 자동 배정 |
| `python -m engine chat --agent youtube` | 특정 에이전트와 대화 |
| `python -m engine ask "질문"` | 한 번만 묻고 끝 |
| `python -m engine set-model youtube gemini` | 에이전트별 기본 AI 지정 |
| `--model openai:gpt-5` | 이번 실행만 AI 바꾸기 |
| `--effort high` | 더 깊게 생각 (Claude) |

대화 중 명령: `/agent 이름`, `/model 제공자:모델`, `/remember 기억할 내용`, `/new`, `/help`, `/quit`

## 지원 AI

| 제공자 | 예시 | 필요한 키 |
|---|---|---|
| `claude` | `claude:claude-opus-5-5`, `claude:claude-haiku-5-5` (저렴) | `ANTHROPIC_API_KEY` |
| `openai` | `openai:gpt-5` | `OPENAI_API_KEY` |
| `gemini` | `gemini:gemini-2.5-pro` | `GEMINI_API_KEY` |
| `ollama` | `ollama:gemma4:latest` (내 PC, 무료) | 없음 (Ollama 실행) |
| `mistral` | `mistral:mistral-large-latest` | `MISTRAL_API_KEY` |
| `groq` | `groq:llama-3.3-70b-versatile` | `GROQ_API_KEY` |
| `xai` | `xai:grok-4` | `XAI_API_KEY` |

접두사 없이 `gemma4:latest`처럼 쓰면 Ollama로 처리합니다 (기존 `agent_models.json` 호환).
새 모델이 나오면 `.env`의 `OPENAI_MODEL` 같은 값만 바꾸면 됩니다.

## 에이전트가 읽는 것

매 호출마다 시스템 프롬프트로 자동 주입:
1. `_shared/decisions.md` → `identity.md` → `goals.md` (회사 공동 메모리)
2. `_agents/<id>/prompt.md`, `goal.md`, `tools.md`
3. `_agents/<id>/memory.md` (최근 6000자)

대화 기록은 `sessions/<시각>/chat.md`에 저장됩니다.
