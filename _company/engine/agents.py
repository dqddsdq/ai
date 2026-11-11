"""자동매매 팀(_agents/trader/)의 지시문·기억을 읽어 시스템 프롬프트를 만듭니다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

COMPANY_DIR = Path(__file__).resolve().parent.parent
AGENTS_DIR = COMPANY_DIR / "_agents"
SHARED_DIR = COMPANY_DIR / "_shared"
SESSIONS_DIR = COMPANY_DIR / "sessions"

SHARED_FILES = ("decisions.md", "goals.md")       # 사장님이 정한 원칙·목표 (있으면 읽음)
AGENT_FILES = ("prompt.md", "goal.md")
MEMORY_TAIL_CHARS = 6000  # memory.md 는 계속 쌓이므로 최근 부분만
TRADER = "trader"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def list_agents() -> list[str]:
    if not AGENTS_DIR.exists():
        return []
    return sorted(p.name for p in AGENTS_DIR.iterdir() if p.is_dir() and not p.name.startswith("."))


def list_teams() -> list[str]:
    return list_agents()


def team_of(agent_id: str) -> str:
    return agent_id


def team_members(team: str) -> list[str]:
    return [team] if (AGENTS_DIR / team).is_dir() else []


def agent_summary(agent_id: str) -> str:
    goal = _read(AGENTS_DIR / agent_id / "goal.md")
    return goal.splitlines()[0] if goal else "자동매매 엔진과 대화"


@dataclass
class Agent:
    id: str

    @property
    def dir(self) -> Path:
        return AGENTS_DIR / self.id

    def exists(self) -> bool:
        return self.dir.is_dir()

    def model_spec(self) -> str | None:
        from . import team as tm
        return tm.team_config(self.id).get("member_model")

    def system_prompt(self) -> str:
        parts = [f"당신은 자동매매 앱의 AI '{self.id}' 입니다. 한국어로 답하세요.",
                 "아래는 사장님이 정한 원칙·목표와 당신의 지시문입니다. 충돌하면 먼저 나온 것이 우선합니다."]
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
        parts.append(STYLE_RULES)
        return "\n\n".join(parts)

    def remember(self, note: str) -> None:
        """memory.md 에 한 줄 추가 (append-only)."""
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with (self.dir / "memory.md").open("a", encoding="utf-8") as f:
            f.write(f"\n- [{stamp}] {note.strip()}\n")


STYLE_RULES = """[답변 스타일]
- 질문 크기에 맞춰 답하세요. 짧은 확인은 1~2문장, 간단한 질문은 3~5줄, 분석·복기 요청일 때만 길게.
- 묻지 않은 주제를 먼저 꺼내지 마세요. 결론을 먼저, 군더더기 없이.
- 숫자는 <engine> 자료에 있는 것만. 없으면 "기록에 없어요"."""
