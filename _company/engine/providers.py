"""AI 제공자(Provider) 모음 — Claude, OpenAI, Gemini, 그리고 OpenAI 호환 API.

모델은 "제공자:모델명" 형식으로 지정합니다.
  claude:claude-opus-5-5
  openai:gpt-5
  gemini:gemini-2.5-pro
  ollama:gemma4:latest      (로컬, 무료)
  mistral:mistral-large-latest
  groq:llama-3.3-70b-versatile
  xai:grok-4
  echo:test                 (API 없이 테스트용)

접두사가 없으면(예: "gemma4:latest") 기존 설정과 호환되도록 ollama로 처리합니다.
중국계 모델은 BLOCKED_MODEL_KEYWORDS로 차단합니다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Iterable

# 사용자가 원치 않는 중국계 모델/제공자 키워드 (소문자 비교)
BLOCKED_MODEL_KEYWORDS = (
    "deepseek", "qwen", "qwq", "kimi", "moonshot", "glm", "chatglm", "zhipu",
    "baichuan", "minimax", "abab", "doubao", "ernie", "hunyuan", "yi-", "internlm",
    "spark", "step-", "sensenova",
)

# 기본 모델 — .env 에서 바꿀 수 있습니다 (예: OPENAI_MODEL=gpt-5.5)
DEFAULT_MODELS = {
    "claude": lambda: os.getenv("CLAUDE_MODEL", "claude-opus-5-5"),
    "openai": lambda: os.getenv("OPENAI_MODEL", "gpt-5"),
    "gemini": lambda: os.getenv("GEMINI_MODEL", "gemini-2.5-pro"),
    "ollama": lambda: os.getenv("OLLAMA_MODEL", "gemma4:latest"),
    "mistral": lambda: os.getenv("MISTRAL_MODEL", "mistral-large-latest"),
    "groq": lambda: os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
    "xai": lambda: os.getenv("XAI_MODEL", "grok-4"),
    "echo": lambda: "test",
}

# OpenAI 호환 엔드포인트: (base_url 환경변수/기본값, API 키 환경변수)
OPENAI_COMPATIBLE = {
    "ollama": (os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"), None),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY"),
}

KEY_ENV = {
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    **{name: env for name, (_, env) in OPENAI_COMPATIBLE.items()},
    "echo": None,
}

# 메시지 형식: [{"role": "user"|"assistant", "content": "..."}]
Message = dict
OnText = Callable[[str], None]


class ProviderError(RuntimeError):
    pass


@dataclass
class ModelSpec:
    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


def parse_model(spec: str | None) -> ModelSpec:
    """'claude:claude-opus-5-5' → ModelSpec. 접두사 없으면 ollama."""
    spec = (spec or os.getenv("DEFAULT_MODEL") or "ollama").strip()
    prefix, _, rest = spec.partition(":")
    if prefix.lower() in DEFAULT_MODELS:
        provider = prefix.lower()
        model = rest or DEFAULT_MODELS[provider]()
    else:
        provider, model = "ollama", spec
    lowered = model.lower()
    for word in BLOCKED_MODEL_KEYWORDS:
        if word in lowered:
            raise ProviderError(f"차단된 모델입니다 (중국계 모델 제외 정책): {model}")
    return ModelSpec(provider, model)


def _has_key(env: str) -> bool:
    """키가 있고, 'your_key_here' 같은 예시 값이 아니면 True."""
    value = os.getenv(env, "").strip()
    return bool(value) and not any(w in value.lower() for w in ("your", "xxx", "here", "<", "example"))


def provider_status() -> list[tuple[str, str, bool]]:
    """(제공자, 기본 모델, 키 설정 여부) 목록."""
    rows = []
    for name, default in DEFAULT_MODELS.items():
        env = KEY_ENV.get(name)
        ready = True if env is None else _has_key(env)
        if name == "gemini":
            ready = _has_key("GEMINI_API_KEY") or _has_key("GOOGLE_API_KEY")
        rows.append((name, default(), ready))
    return rows


def chat(
    spec: ModelSpec,
    system: str,
    messages: list[Message],
    on_text: OnText | None = None,
    effort: str = "medium",
) -> str:
    """모델에 대화를 보내고 전체 답변 텍스트를 돌려줍니다.

    on_text 가 있으면 답변이 생성되는 대로 조각을 넘겨줍니다(스트리밍).
    """
    handler = {
        "claude": _chat_claude,
        "openai": _chat_openai,
        "gemini": _chat_gemini,
        "echo": _chat_echo,
    }.get(spec.provider, _chat_openai_compatible)
    try:
        return handler(spec, system, messages, on_text, effort)
    except ProviderError:
        raise
    except ImportError as e:
        raise ProviderError(f"패키지가 없어요: {e}. `pip install -r requirements.txt` 실행하세요.") from e
    except Exception as e:  # SDK마다 예외 타입이 달라서 한 곳에서 정리
        raise ProviderError(f"[{spec}] 호출 실패: {type(e).__name__}: {e}") from e


def _emit(on_text: OnText | None, chunks: Iterable[str]) -> str:
    out = []
    for piece in chunks:
        if piece:
            out.append(piece)
            if on_text:
                on_text(piece)
    return "".join(out)


# ── Claude (Anthropic) ────────────────────────────────────────────────────
def _chat_claude(spec, system, messages, on_text, effort):
    import anthropic

    client = anthropic.Anthropic()
    kwargs = dict(
        model=spec.model,
        max_tokens=64000,
        system=system,
        messages=messages,
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
    )
    # 안전 분류기가 거절하면 서버가 다른 Claude 모델로 자동 재시도 (Claude API 전용)
    if spec.model in ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"):
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        stream_ctx = client.beta.messages.stream(**kwargs)
    else:
        stream_ctx = client.messages.stream(**kwargs)

    with stream_ctx as stream:
        text = _emit(on_text, stream.text_stream)
        final = stream.get_final_message()
    if final.stop_reason == "refusal":
        raise ProviderError("Claude가 이 요청을 거절했어요. 질문을 바꿔 보세요.")
    if final.stop_reason == "max_tokens":
        text += "\n\n[답변이 길어서 잘렸어요]"
    return text


# ── OpenAI ────────────────────────────────────────────────────────────────
def _chat_openai(spec, system, messages, on_text, effort):
    from openai import OpenAI

    client = OpenAI()
    stream = client.responses.create(
        model=spec.model,
        instructions=system,
        input=messages,
        stream=True,
    )

    def pieces():
        for event in stream:
            if event.type == "response.output_text.delta":
                yield event.delta
            elif event.type in ("response.failed", "error"):
                raise ProviderError(f"OpenAI 오류: {event}")

    return _emit(on_text, pieces())


# ── OpenAI 호환 (Ollama, Mistral, Groq, xAI) ─────────────────────────────
def _chat_openai_compatible(spec, system, messages, on_text, effort):
    from openai import OpenAI

    if spec.provider not in OPENAI_COMPATIBLE:
        raise ProviderError(f"알 수 없는 제공자: {spec.provider}")
    base_url, key_env = OPENAI_COMPATIBLE[spec.provider]
    api_key = os.getenv(key_env) if key_env else "ollama"
    if not api_key:
        raise ProviderError(f"{key_env} 가 .env 에 없어요.")
    client = OpenAI(base_url=base_url, api_key=api_key)
    stream = client.chat.completions.create(
        model=spec.model,
        messages=[{"role": "system", "content": system}, *messages],
        stream=True,
    )

    def pieces():
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    return _emit(on_text, pieces())


# ── Google Gemini ─────────────────────────────────────────────────────────
def _chat_gemini(spec, system, messages, on_text, effort):
    from google import genai
    from google.genai import types

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ProviderError("GEMINI_API_KEY 가 .env 에 없어요.")
    client = genai.Client(api_key=api_key)
    contents = [
        types.Content(
            role="model" if m["role"] == "assistant" else "user",
            parts=[types.Part(text=m["content"])],
        )
        for m in messages
    ]
    stream = client.models.generate_content_stream(
        model=spec.model,
        contents=contents,
        config=types.GenerateContentConfig(system_instruction=system),
    )
    return _emit(on_text, (chunk.text for chunk in stream))


# ── 테스트용 (API 키 없이 동작 확인) ──────────────────────────────────────
def _chat_echo(spec, system, messages, on_text, effort):
    last = messages[-1]["content"] if messages else ""
    if "JSON" in system and "agent" in system:
        reply = '{"agent": "developer", "reason": "테스트 라우팅"}'
    else:
        reply = f"[echo] {last}"
    return _emit(on_text, [reply])
