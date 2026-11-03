"""AI 제공자(Provider) 모음 — Claude, OpenAI, Gemini, 그리고 OpenAI 호환 API.

모델은 "제공자:모델명" 형식으로 지정합니다.
  claudecode                (설치된 Claude Code 로그인 사용, API 키 불필요 — 기본값)
  claude:claude-opus-5-5    (Anthropic API 키 사용)
  openai:gpt-5
  gemini:gemini-2.5-pro
  local                     (켜져 있는 로컬 실행기 + 로드된 모델 자동 감지, 기본값)
  lmstudio:gemma-3-12b-it   (LM Studio, 무료)
  llamacpp:any              (llama.cpp server)
  vllm:모델명               (vLLM)
  ollama:gemma4:latest      (Ollama)
  mistral:mistral-large-latest
  groq:llama-3.3-70b-versatile
  xai:grok-4
  echo:test                 (API 없이 테스트용)

접두사가 없으면(예: "gemma4:latest") 켜져 있는 로컬 실행기에서 그 모델을 찾습니다.
중국계 모델은 BLOCKED_MODEL_KEYWORDS로 차단합니다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
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
    "claudecode": lambda: os.getenv("CLAUDECODE_MODEL", "default"),
    "claude": lambda: os.getenv("CLAUDE_MODEL", "claude-opus-5-5"),
    "openai": lambda: os.getenv("OPENAI_MODEL", "gpt-5"),
    "gemini": lambda: os.getenv("GEMINI_MODEL", "gemini-2.5-pro"),
    "local": lambda: os.getenv("LOCAL_MODEL", "auto"),
    "lmstudio": lambda: os.getenv("LMSTUDIO_MODEL", "auto"),
    "llamacpp": lambda: os.getenv("LLAMACPP_MODEL", "auto"),
    "vllm": lambda: os.getenv("VLLM_MODEL", "auto"),
    "ollama": lambda: os.getenv("OLLAMA_MODEL", "auto"),
    "mistral": lambda: os.getenv("MISTRAL_MODEL", "mistral-large-latest"),
    "groq": lambda: os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
    "xai": lambda: os.getenv("XAI_MODEL", "grok-4"),
    "echo": lambda: "test",
}

# 내 PC에서 돌아가는 실행기들 (모두 OpenAI 호환 API 제공): (주소 환경변수, 기본 주소)
LOCAL_SERVERS = {
    "lmstudio": ("LMSTUDIO_BASE_URL", "http://localhost:1234/v1"),
    "llamacpp": ("LLAMACPP_BASE_URL", "http://localhost:8080/v1"),
    "vllm": ("VLLM_BASE_URL", "http://localhost:8000/v1"),
    "ollama": ("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
}

# 클라우드 OpenAI 호환 엔드포인트: (base_url, API 키 환경변수)
OPENAI_COMPATIBLE = {
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY"),
}

KEY_ENV = {
    "claudecode": None,
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    **{name: env for name, (_, env) in OPENAI_COMPATIBLE.items()},
    **{name: None for name in (*LOCAL_SERVERS, "local")},
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


def claude_cli() -> str | None:
    """claude 명령 위치. PATH 에 없어도 흔한 설치 위치를 직접 찾아봅니다 (윈도우 설치 직후 PATH 미등록 대비)."""
    found = os.getenv("CLAUDE_CLI") or shutil.which("claude")
    if found:
        return found
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".local", "bin", "claude.exe"),           # 윈도우 공식 설치 (irm ... | iex)
        os.path.join(home, ".local", "bin", "claude"),               # 맥/리눅스 공식 설치
        os.path.join(home, ".claude", "local", "claude.exe"),
        os.path.join(home, ".claude", "local", "claude"),
        os.path.join(os.getenv("APPDATA", ""), "npm", "claude.cmd"),  # npm 으로 설치
        os.path.join(os.getenv("LOCALAPPDATA", ""), "Programs", "claude", "claude.exe"),
        "/usr/local/bin/claude", "/opt/homebrew/bin/claude",
    ]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def _auto_default() -> str:
    """키/설치 상태를 보고 기본 제공자 고르기: Claude Code → Claude API → 로컬."""
    if claude_cli():
        return "claudecode"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "claude"
    return "local"


def parse_model(spec: str | None) -> ModelSpec:
    """'claude:claude-opus-5-5' → ModelSpec. 접두사 없으면 로컬 자동 감지."""
    spec = (spec or os.getenv("DEFAULT_MODEL") or _auto_default()).strip()
    prefix, _, rest = spec.partition(":")
    if prefix.lower() in DEFAULT_MODELS:
        provider = prefix.lower()
        model = rest or DEFAULT_MODELS[provider]()
    else:
        provider, model = "local", spec
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
        if name == "claudecode":
            ready = bool(claude_cli())
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
    web: bool = False,
    images: list[str] | None = None,
    timeout: float | None = None,
) -> str:
    """모델에 대화를 보내고 전체 답변 텍스트를 돌려줍니다.

    on_text 가 있으면 답변이 생성되는 대로 조각을 넘겨줍니다(스트리밍).
    """
    if images:
        if spec.provider == "claude":
            messages = _with_images_claude(messages, images)
        elif spec.provider != "claudecode":
            note = f"\n\n[사용자가 이미지 {len(images)}장을 첨부했지만 이 모델({spec})로는 볼 수 없어요. 이미지가 필요하면 Claude 모델로 바꾸라고 안내하세요.]"
            messages = messages[:-1] + [{"role": "user", "content": messages[-1]["content"] + note}]
    if spec.provider == "claudecode":
        try:
            return _chat_claudecode(spec, system, messages, on_text, effort, web, images or [], timeout)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"[{spec}] 호출 실패: {type(e).__name__}: {e}") from e
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


# ── Claude Code CLI (구독 로그인 그대로 사용) ──────────────────────────────
CLAUDECODE_TIMEOUT = 900


def _with_images_claude(messages: list[Message], images: list[str]) -> list[Message]:
    import base64
    import mimetypes

    blocks = []
    for path in images:
        mt = mimetypes.guess_type(path)[0] or "image/png"
        with open(path, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode()
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}})
    blocks.append({"type": "text", "text": messages[-1]["content"]})
    return messages[:-1] + [{"role": "user", "content": blocks}]


def _chat_claudecode(spec, system, messages, on_text, effort, web, images=(), timeout=None):
    exe = claude_cli()
    if not exe:
        raise ProviderError("Claude Code(claude 명령)를 못 찾았어요. 설치 후 `claude` 로 한 번 로그인하세요.")
    # Windows 의 claude.cmd 는 명령줄 길이 제한(8191자)이 있어서 긴 내용은 전부 stdin 으로 보냅니다.
    history = messages[:-1]
    parts = ["<instructions>", system, "</instructions>"]
    if history:
        parts.append("<conversation_so_far>")
        for m in history:
            who = "사용자" if m["role"] == "user" else "나(에이전트)"
            parts.append(f"[{who}]\n{m['content']}")
        parts.append("</conversation_so_far>")
    parts += ["<user_message>", messages[-1]["content"] if messages else "", "</user_message>"]
    if images:
        parts.append("<attached_images>\n사용자가 화면 캡처 이미지를 첨부했어요. 답하기 전에 Read 도구로 아래 파일을 전부 열어서 보세요:\n"
                     + "\n".join(os.path.abspath(p) for p in images) + "\n</attached_images>")
    prompt = "\n\n".join(parts)

    cmd = [
        exe, "-p",
        "--system-prompt", "You are an AI agent inside the user's one-person company. Follow <instructions> exactly "
                           "and reply only to <user_message>, in Korean unless asked otherwise.",
        "--output-format", "text",
        "--tools", ",".join((["WebSearch", "WebFetch"] if web else []) + (["Read"] if images else [])),
        "--no-session-persistence",
        "--strict-mcp-config",
    ]
    allowed = (["WebSearch", "WebFetch"] if web else []) + (["Read"] if images else [])
    if allowed:
        cmd += ["--allowedTools", ",".join(allowed)]
    if images:
        cmd += ["--add-dir", os.path.dirname(os.path.abspath(images[0]))]
    if spec.model and spec.model != "default":
        cmd += ["--model", spec.model]

    import tempfile
    import threading

    # stderr 는 파일로 받음: 파이프로 받으면 윈도우에서 버퍼가 차서 멈출 수 있음
    err_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err_file,
                            text=True, encoding="utf-8", errors="replace", cwd=os.getcwd(),
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    limit = timeout or CLAUDECODE_TIMEOUT
    timed_out = threading.Event()

    def _kill():
        timed_out.set()
        proc.kill()

    watchdog = threading.Timer(limit, _kill)
    watchdog.daemon = True
    watchdog.start()
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        text = _emit(on_text, iter(proc.stdout.readline, ""))
        proc.wait(timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        pass
    finally:
        watchdog.cancel()
        if proc.poll() is None:  # 중지 버튼 등으로 중간에 끊기면 claude 프로세스도 정리
            proc.kill()
    err_file.seek(0)
    err = err_file.read().strip()[-600:]
    err_file.close()
    if timed_out.is_set():
        raise ProviderError(f"Claude Code 가 {int(limit)}초 안에 답하지 않았어요. {err}".strip())
    if proc.returncode != 0:
        raise ProviderError(f"Claude Code 오류 (코드 {proc.returncode}): {err or locals().get('text', '')[-300:] or '메시지 없음'}")
    return text.strip()


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


# ── 로컬 실행기 감지 ──────────────────────────────────────────────────────
def local_url(name: str) -> str | None:
    if name == "local":
        return os.getenv("LOCAL_BASE_URL") or None
    env, default = LOCAL_SERVERS[name]
    return os.getenv(env) or default


def probe_local(name: str, timeout: float = 1.5) -> list[str] | None:
    """실행기가 켜져 있으면 로드된/설치된 모델 목록, 꺼져 있으면 None."""
    url = local_url(name)
    if not url:
        return None
    try:
        import requests

        r = requests.get(url.rstrip("/") + "/models", timeout=timeout)
        r.raise_for_status()
        return [m.get("id", "") for m in r.json().get("data", []) if m.get("id")]
    except Exception:
        return None


def resolve_local(spec: ModelSpec) -> tuple[str, str]:
    """(base_url, 모델 이름). 'local' 이면 켜져 있는 실행기를 찾습니다."""
    names = [spec.provider] if spec.provider != "local" else (
        ["local"] if os.getenv("LOCAL_BASE_URL") else list(LOCAL_SERVERS))
    tried = []
    for name in names:
        models = probe_local(name)
        tried.append(f"{name}({local_url(name)})")
        if models is None:
            continue
        if spec.model in ("auto", ""):
            if not models:
                raise ProviderError(f"{name} 는 켜져 있지만 로드된 모델이 없어요. 모델을 먼저 로드하세요.")
            return local_url(name), models[0]
        if spec.provider != "local" or spec.model in models or not models:
            return local_url(name), spec.model
        # 이름이 정확히 안 맞으면 비슷한 것 찾기 (예: gemma3:12b ↔ gemma-3-12b-it)
        key = "".join(ch for ch in spec.model.lower() if ch.isalnum())
        close = [m for m in models if key[:6] and key[:6] in "".join(ch for ch in m.lower() if ch.isalnum())]
        if close:
            return local_url(name), close[0]
        if spec.provider == "local" and len(names) > 1:
            continue
        raise ProviderError(f"{name} 에 '{spec.model}' 모델이 없어요. 있는 모델: {', '.join(models)}")
    raise ProviderError("켜져 있는 로컬 AI 실행기를 못 찾았어요. LM Studio 서버를 켜거나 "
                        f"(Developer 탭 → Start Server) 클라우드 모델을 쓰세요. 확인한 곳: {', '.join(tried)}")


# ── OpenAI 호환 (로컬 실행기, Mistral, Groq, xAI) ─────────────────────────
def _chat_openai_compatible(spec, system, messages, on_text, effort):
    from openai import OpenAI

    model = spec.model
    if spec.provider in LOCAL_SERVERS or spec.provider == "local":
        base_url, model = resolve_local(spec)
        api_key = "local"
    elif spec.provider in OPENAI_COMPATIBLE:
        base_url, key_env = OPENAI_COMPATIBLE[spec.provider]
        api_key = os.getenv(key_env)
        if not api_key:
            raise ProviderError(f"{key_env} 가 .env 에 없어요.")
    else:
        raise ProviderError(f"알 수 없는 제공자: {spec.provider}")
    client = OpenAI(base_url=base_url, api_key=api_key)
    stream = client.chat.completions.create(
        model=model,
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
