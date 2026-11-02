# 🤖 1인 기업 AI 엔진

`_agents/` 폴더의 에이전트들(CEO, 유튜브, 비서, 개발자 등)을 실제로 대화하고 일하게 만드는 엔진입니다.
여러 AI 회사 모델을 자유롭게 바꿔 쓸 수 있습니다 (중국계 모델은 차단).

## 🏠 내 컴퓨터에서 무료로 (로컬 AI, 기본값)

API 키 없이 내 PC에서 돌아갑니다. 기본값이 로컬(Ollama)이에요.

1. https://ollama.com 에서 Ollama 설치
2. 모델 받기: `ollama pull gemma4` (PC가 약하면 더 작은 모델, 예: `ollama pull gemma3:4b`)
3. `cd _company && pip install -r requirements.txt && python -m engine chat`

에이전트마다 다른 로컬 모델을 쓰려면: `python -m engine set-model youtube ollama:gemma3:4b`

## 시작하기 (클라우드 AI도 쓸 때)

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

## 📚 내 자료로 똑똑하게 만들기 (학습 없이, 바로 적용)

`00_Raw/` 나 `10_Wiki/` 에 `.md` 파일을 넣으면, 질문과 관련된 부분을 찾아서 에이전트가 참고합니다.
모델을 다시 학습시킬 필요가 없고, 파일을 넣는 즉시 반영돼요.

```bash
python -m engine search 후킹 전략    # 어떤 자료가 찾아지는지 확인
python -m engine chat --no-knowledge  # 자료 참고 끄기
```

## 🧠 내 컴퓨터에서 학습시키기 (파인튜닝)

말투나 일하는 방식을 모델 자체에 새기고 싶을 때 씁니다. 지식은 위의 자료 폴더가 더 정확하고 쉬워요.

1. 평소처럼 `chat` 으로 일하면 `sessions/*/chat.md` 에 기록이 쌓여요. 마음에 안 드는 답변 세션은 지우세요.
2. 학습 데이터 만들기: `python -m engine export-training` → `training/dataset.jsonl`
3. 최소 100~200개 대화는 모아야 효과가 있어요.
4. LoRA 파인튜닝 (PC 사양에 따라):
   - **NVIDIA 그래픽카드 (VRAM 8GB 이상)**: Unsloth
   - **Apple Silicon Mac (M1~M4)**: MLX-LM
5. 학습된 모델을 GGUF로 변환 → `ollama create 내모델 -f Modelfile` → `python -m engine set-model ceo ollama:내모델`
