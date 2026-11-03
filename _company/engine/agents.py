"""에이전트 폴더(_agents/<id>/)를 읽어서 시스템 프롬프트를 만듭니다."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

COMPANY_DIR = Path(__file__).resolve().parent.parent
AGENTS_DIR = COMPANY_DIR / "_agents"
SHARED_DIR = COMPANY_DIR / "_shared"
SESSIONS_DIR = COMPANY_DIR / "sessions"
MODELS_FILE = SHARED_DIR / "agent_models.json"

# 공동 메모리: 우선순위 순서 (_system.md 의 메모리 위계)
SHARED_FILES = ("decisions.md", "identity.md", "goals.md")
AGENT_FILES = ("prompt.md", "goal.md", "tools.md")
MEMORY_TAIL_CHARS = 6000  # memory.md 는 계속 쌓이므로 최근 부분만


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def list_agents() -> list[str]:
    if not AGENTS_DIR.exists():
        return []
    return sorted(p.name for p in AGENTS_DIR.iterdir() if p.is_dir() and not p.name.startswith("."))


def team_of(agent_id: str) -> str:
    """'stock-3' → 'stock'"""
    return re.sub(r"-\d+$", "", agent_id)


def list_teams() -> list[str]:
    return sorted({team_of(a) for a in list_agents()})


def team_members(team: str) -> list[str]:
    """팀장(번호 없는 폴더)이 먼저, 그다음 -2, -3 ..."""
    members = [a for a in list_agents() if team_of(a) == team]
    return sorted(members, key=lambda a: (a != team, int(a.rsplit("-", 1)[1]) if a != team else 0))


def member_view(agent_id: str) -> str:
    """prompt.md 의 '팀 내 관점' 첫 줄 (예: '안정 수익 중심')."""
    m = re.search(r"## 팀 내 관점.*?\n- ([^:：\n]+)", _read(AGENTS_DIR / agent_id / "prompt.md"), re.S)
    return m.group(1).strip() if m else agent_id


def agent_summary(agent_id: str) -> str:
    """라우터가 고를 수 있도록 에이전트 한 줄 설명을 만듭니다."""
    system_md = _read(SHARED_DIR / "_system.md")
    lines = [l for l in system_md.splitlines() if l.startswith("- ") and "**" in l]
    for line in lines:
        if f"`{team_of(agent_id)}`" in line:
            return line[2:].replace("**", "").strip()
    for line in lines:
        if agent_id.lower() in line.lower():
            return line[2:].replace("**", "").strip()
    goal = _read(AGENTS_DIR / agent_id / "goal.md")
    bullets = [l[2:] for l in goal.splitlines() if l.startswith("- ")]
    return f"{agent_id}: " + (" / ".join(bullets[:2]) if bullets else "일반 업무")


def load_models() -> dict:
    try:
        return json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_model(agent_id: str, spec: str) -> None:
    data = load_models()
    data[agent_id] = spec
    MODELS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@dataclass
class Agent:
    id: str

    @property
    def dir(self) -> Path:
        return AGENTS_DIR / self.id

    def exists(self) -> bool:
        return self.dir.is_dir()

    def model_spec(self) -> str | None:
        return load_models().get(self.id)

    def system_prompt(self) -> str:
        parts = [
            f"당신은 1인 기업의 AI 에이전트 '{self.id}' 입니다. 한국어로 답하세요.",
            "아래는 회사 공동 메모리와 당신의 개인 설정입니다. 충돌하면 먼저 나온 것이 우선합니다.",
        ]
        for name in SHARED_FILES:
            text = _read(SHARED_DIR / name)
            if text:
                parts.append(f"<shared file=\"{name}\">\n{text}\n</shared>")
        for name in AGENT_FILES:
            text = _read(self.dir / name)
            if text:
                parts.append(f"<agent file=\"{name}\">\n{text}\n</agent>")
        memory = _read(self.dir / "memory.md")
        if memory:
            if len(memory) > MEMORY_TAIL_CHARS:
                memory = "…(앞부분 생략)…\n" + memory[-MEMORY_TAIL_CHARS:]
            parts.append(f"<agent file=\"memory.md\">\n{memory}\n</agent>")
        parts.append(
            "도구 매니페스트의 AUTONOMY_LEVEL 을 지키세요. 외부로 보내거나 삭제하는 행동은 "
            "직접 실행하지 말고 초안과 실행 방법만 제시하세요."
        )
        parts.append(STYLE_RULES)
        return "\n\n".join(parts)

    def remember(self, note: str) -> None:
        """memory.md 에 한 줄 추가 (append-only)."""
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with (self.dir / "memory.md").open("a", encoding="utf-8") as f:
            f.write(f"\n- [{stamp}] {note.strip()}\n")


# 위 회사 메모리(decisions.md 등)의 형식 규칙보다 우선하는 답변 스타일
STYLE_RULES = """[답변 스타일 — 위의 어떤 형식 규칙보다 우선]
- 질문 크기에 맞춰 답하세요. 인사·잡담은 1~2문장, 간단한 질문은 3~5줄, 분석·계획 요청일 때만 길게.
- '다음 액션'이나 되묻기는 사용자가 계획·실행 방법을 물었을 때만 붙이세요. 인사에는 붙이지 마세요.
- 묻지 않은 주제(회사의 다른 사업 등)를 먼저 꺼내지 마세요.
- 결론을 먼저, 군더더기 없이."""

ROUTER_PROMPT = """당신은 CEO 에이전트의 업무 분배기입니다.
사용자 요청을 가장 잘 처리할 팀 하나를 고르세요.

팀 목록:
{agents}

예외: 인사·잡담·감사·짧은 확인("ㅎㅇ", "고마워", "ㅇㅋ", "뭐해?")처럼 가벼운 말이면 팀을 고르지 말고
직접 짧게(1~2문장, 존댓말, 제안이나 질문 덧붙이지 않기) 답하세요: {{"agent": "none", "reply": "안녕하세요! 무엇을 도와드릴까요?"}}

반드시 JSON 한 줄로만 답하세요. 예: {{"agent": "stock", "reason": "종목 분석 요청"}}"""


def route_prompt() -> str:
    lines = "\n".join(f"- {t}: {agent_summary(t)}" for t in list_teams() if t != "ceo")
    return ROUTER_PROMPT.format(agents=lines)


def parse_quick(reply: str) -> str | None:
    """라우터가 가벼운 말에 바로 답했으면 그 답을 돌려줌."""
    match = re.search(r"\{.*\}", reply, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if str(data.get("agent", "")).strip().lower() == "none" and str(data.get("reply", "")).strip():
        return str(data["reply"]).strip()
    return None


def parse_route(reply: str) -> tuple[str, str]:
    """라우터 답변에서 (agent, reason) 추출. 실패하면 ceo."""
    match = re.search(r"\{.*?\}", reply, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            agent = team_of(str(data.get("agent", "")).strip())
            if agent in list_teams():
                return agent, str(data.get("reason", ""))
        except json.JSONDecodeError:
            pass
    return "ceo", "분배 실패 — CEO가 직접 처리"


class SessionLog:
    """sessions/<시각>/chat.md 에 대화 기록."""

    def __init__(self) -> None:
        self.dir = SESSIONS_DIR / datetime.now().strftime("%Y-%m-%dT%H-%M")
        self.file = self.dir / "chat.md"

    def write(self, who: str, text: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.file.open("a", encoding="utf-8") as f:
            f.write(f"\n### {who}\n\n{text.strip()}\n")
