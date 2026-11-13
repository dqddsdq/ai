"""🛟 자동 개선 지킴이 — 'AI 시작.bat' 이 앱을 켜기 직전에 매번 먼저 실행 (잠긴 파일).

자기 개선이 코드를 자동으로 고치고 앱을 다시 켤 때:
- 고친 뒤 앱이 제대로 못 켜지고 계속 꺼지면(3번째 켜기 시도) → 고치기 전 파일로 자동 되돌림
- 앱이 켜져서 자체 점검을 통과하고 3분 동안 잘 돌면 web.py 쪽에서 표시(_cache/improve_verify.json)를 지움

다른 엔진 파일을 하나도 불러오지 않음 → 고친 코드가 망가져 있어도 이건 항상 돌아감.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

COMPANY = Path(__file__).resolve().parent.parent
MARK = COMPANY / "_cache" / "improve_verify.json"
BACKUP = COMPANY / "_cache" / "backups"
HISTORY = COMPANY / "_shared" / "improvements.json"
MAX_BOOTS = 3


def _restore(job_id: str) -> int:
    man = json.loads((BACKUP / job_id / "_manifest.json").read_text(encoding="utf-8"))
    n = 0
    for m in man:
        dst = COMPANY / m["path"]
        if m["existed"]:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(BACKUP / job_id / m["path"], dst)
        else:
            dst.unlink(missing_ok=True)
        n += 1
    try:   # 기록에 표시
        rows = json.loads(HISTORY.read_text(encoding="utf-8"))
        for r in rows:
            if r.get("id") == job_id:
                r.update(status="rolled_back", rolled_back=datetime.now().isoformat(timespec="seconds"),
                         auto_rollback="고친 뒤 앱이 제대로 켜지지 않아서 자동으로 되돌림")
        HISTORY.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    except (OSError, ValueError):
        pass
    return n


def main() -> int:
    try:
        mark = json.loads(MARK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    mark["boots"] = int(mark.get("boots", 0)) + 1
    if mark["boots"] >= MAX_BOOTS:
        try:
            n = _restore(mark["job"])
            print(f"[되돌리기] 자동 개선 '{mark.get('request', '')[:40]}' 뒤로 앱이 제대로 안 켜져서 원래대로 되돌렸어요 ({n}개 파일)")
            (COMPANY / "_cache" / "improve_rolled_back.json").write_text(json.dumps(mark, ensure_ascii=False), encoding="utf-8")
        except (OSError, ValueError, KeyError) as e:
            print(f"[되돌리기 실패] {type(e).__name__}: {e} — _cache/backups/{mark.get('job')} 에 원래 파일이 있어요")
        MARK.unlink(missing_ok=True)
        return 0
    MARK.write_text(json.dumps(mark, ensure_ascii=False), encoding="utf-8")
    print(f"[확인 중] 자동 개선 적용 후 {mark['boots']}번째 켜기")
    return 0


if __name__ == "__main__":
    sys.exit(main())
