# 🤖 1인 기업 AI 엔진

`_agents/` 폴더의 에이전트들(CEO, 유튜브, 비서, 개발자 등)을 실제로 대화하고 일하게 만드는 엔진입니다.
여러 AI 회사 모델을 자유롭게 바꿔 쓸 수 있습니다 (중국계 모델은 차단).

## ⚡ 기본: Claude Code 로그인 그대로 사용 (API 키 불필요)

PC에 Claude Code가 설치돼 있고 로그인돼 있으면, 엔진이 자동으로 그걸 씁니다.

```bash
cd _company
pip install -r requirements.txt
python -m engine chat
```

- `learn` 은 Claude가 **직접 웹 검색 + 페이지 읽기**를 해서 노트를 씁니다 (DuckDuckGo 불필요)
- 모델 바꾸기: `--model claudecode:sonnet`, `claudecode:haiku`(빠름), `claudecode:opus`
- 사용량은 Claude 구독 한도에서 차감됩니다

## 🏠 무료 로컬 AI (선택)

LM Studio, llama.cpp, vLLM, Ollama 중 아무거나 켜 두면 `local` 로 자동 감지합니다.
`python -m engine models` 로 켜져 있는 실행기와 로드된 모델을 확인하고, `set-model ceo local` 로 지정하세요.

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
4. 학습 (NVIDIA 16GB 기준, Windows 는 WSL2 Ubuntu 안에서):
   ```bash
   pip install unsloth
   python -m engine.finetune                         # Gemma 3 12B, QLoRA
   python -m engine.finetune --base unsloth/gemma-3-4b-it   # 작게·빠르게
   ```
5. 끝나면 출력되는 `ollama create my-company -f ...` 실행 → `python -m engine set-model ceo ollama:my-company`

> RTX 4070 Ti SUPER 16GB 추천 실행 모델: 대화 `ollama pull gemma3:12b`, 분석·시뮬레이션 `ollama pull gpt-oss:20b`

## 🌐 스스로 인터넷 공부하기

AI가 직접 검색하고, 페이지를 읽고, 정리한 노트를 `10_Wiki/날짜/주제.md` 에 저장합니다.
저장된 노트는 위의 자료 검색이 자동으로 찾아 쓰기 때문에, 공부할수록 답변이 좋아져요.

```bash
python -m engine learn "유튜브 쇼츠 수익화"       # 주제 하나 공부
python -m engine learn --auto --rounds 3           # goals.md 보고 주제를 스스로 3개 골라 공부
```

- 검색: 기본은 무료 DuckDuckGo, `.env` 에 `BRAVE_API_KEY` 가 있으면 Brave Search (더 안정적)
- 모델: `researcher` 에이전트의 모델 (`set-model researcher ...` 로 변경)

**매일 자동으로 공부시키기**
- 맥/리눅스: `crontab -e` 에 `0 9 * * * cd /경로/ai/_company && python -m engine learn --auto --rounds 2`
- 윈도우: 작업 스케줄러 → 매일 → 프로그램 `python`, 인수 `-m engine learn --auto --rounds 2`, 시작 위치 `_company`

**안전장치**
- 하는 일은 검색 → 읽기 → `10_Wiki` 에 노트 쓰기 뿐. 코드 실행·게시·전송·삭제 안 함
- 웹 페이지 안의 "지시문"은 따르지 않도록 막아둠 (프롬프트 인젝션 방지)
- 모든 노트에 출처 URL 과 `status: 미검토` 표시 → 틀린 정보가 섞일 수 있으니 가끔 확인하고 `검토완료` 로 바꾸세요

## 🎲 목표 시뮬레이션 ("이걸 원해" → 달성 확률)

원하는 것을 말하면 AI가 시나리오(변수·범위·계산식)를 설계하고, 그걸 1만 번 가상으로 돌려서 달성 확률을 알려줍니다.

```bash
python -m engine simulate "애드센스 블로그로 2년 안에 월 1000만원"
python -m engine simulate --stock 005930.KS --years 1 --target 20    # 삼성전자 1년 +20%?
python -m engine simulate --stock AAPL --years 3 --target 50
python -m engine simulate --stock X --csv 내가격데이터.csv --target 10
```

결과: 달성 확률, 보통/나쁜/좋은 경우 범위, **결과를 가장 크게 좌우하는 변수**, AI 해석 → `simulations/` 에 보고서 저장.

- **가정 바꿔보기**: 함께 저장되는 `.json` 의 숫자를 고치고 `--scenario 파일.json` 으로 다시 실행
- **주식**: 실제 과거 수익률을 20일 묶음으로 섞어 미래 경로를 만듭니다(블록 부트스트랩). 손실 확률·최대 낙폭도 계산
- **안전**: AI는 JSON 설계만 하고, 계산은 사칙연산과 허용 함수만 되는 계산기가 합니다(AI가 쓴 코드 실행 없음)

> ⚠️ 시뮬레이션은 "가정이 맞다면"의 결과이지 예측이 아닙니다. 주식 결과는 투자 조언이 아니며,
> 과거 수익률이 미래에도 반복된다는 가정에 기댑니다.
