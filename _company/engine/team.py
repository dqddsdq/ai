"""팀 설정 (자동매매 전용 — 팀은 '자동매매(trader)' 하나뿐)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import agents as ag
from . import providers as pv


def team_config(team: str = "trader") -> dict:
    """_agents/<팀>/team.json — 예: {"member_model": "claudecode:sonnet"}"""
    try:
        return json.loads((ag.AGENTS_DIR / team / "team.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def settings() -> dict:
    """_shared/settings.json"""
    try:
        return json.loads((ag.SHARED_DIR / "settings.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def fast_spec(override: str | None = None):
    """짧은 판단용 모델 (settings.json 의 fast_model, 기본 Haiku). 사용자가 모델을 고르면 그걸 씀."""
    return pv.parse_model(override or settings().get("fast_model") or "claudecode:haiku")


@dataclass
class Step:
    """작업 단계 카드 (화면 표시용) — 자기 개선 실행 화면이 씀"""
    id: str
    title: str
    member: str
    view: str = ""
    text: str = ""
    verdict: str = ""
    error: str = ""
    model: str = ""
    reads: list = None
    kind: str = ""

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "member": self.member, "view": self.view,
                "text": self.text, "verdict": self.verdict, "error": self.error, "model": self.model,
                "reads": self.reads or [], "kind": self.kind}
