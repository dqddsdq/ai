"""💾 안전한 기록 저장 — 자동매매·학습·그림자 매매 기록을 메모리에 들고 쓰고, 디스크에는 안전하게.

- 읽기: 한 번 읽으면 메모리에 보관. 파일이 바뀌었을 때만(수정 시각) 다시 읽음 → 10초마다 여러 번 읽던 부담 제거
- 쓰기: 임시 파일에 다 쓴 뒤 바꿔치기(os.replace) → 쓰는 도중 PC 가 꺼져도 원래 파일은 안 깨짐
- 백업: 10분마다 .bak 한 벌 + 하루 한 번 _cache/backup/ 에 날짜별(14일 보관)
- 파일이 깨져 있으면 .bak → 날짜별 백업 순서로 자동 복구 (recovered() 로 알림)
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

_lock = threading.RLock()
_cache: dict[str, dict] = {}
_recovered: list[str] = []
BAK_EVERY = 600


def _backup_dir(path: Path) -> Path:
    return path.parent / "backup"


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _from_backups(path: Path):
    """깨진 파일 복구: .bak → 날짜별 백업 (최신부터)"""
    cands = [path.with_suffix(path.suffix + ".bak")]
    bdir = _backup_dir(path)
    if bdir.exists():
        cands += sorted(bdir.glob(f"{path.stem}_*{path.suffix}"), reverse=True)
    for c in cands:
        try:
            data = _read(c)
            _recovered.append(f"{path.name} 이 깨져 있어서 {c.name} 에서 복구했어요")
            return data
        except Exception:
            continue
    return None


def load(path: Path, default=dict) -> dict:
    """메모리에 있으면 그대로, 파일이 바뀌었으면 다시 읽기. 없거나 깨졌으면 백업 → 기본값"""
    path = Path(path)
    key = str(path)
    with _lock:
        try:
            mt = path.stat().st_mtime_ns
        except FileNotFoundError:
            mt = None
        e = _cache.get(key)
        if e and e["mt"] == mt and e["data"] is not None:
            return e["data"]
        data, fixed = None, False
        if mt is not None:
            try:
                data = _read(path)
            except Exception:
                data = _from_backups(path)
                fixed = data is not None
        if data is None:
            data = default()
        _cache[key] = {"data": data, "mt": mt, "bak": e["bak"] if e else 0}
        if fixed:   # 복구한 내용으로 파일도 바로 고쳐 둠
            save(path, data)
        return data


def save(path: Path, data) -> None:
    """임시 파일에 쓰고 바꿔치기 + 가끔 백업"""
    path = Path(path)
    key = str(path)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
        e = _cache.setdefault(key, {"data": data, "mt": None, "bak": 0})
        e["data"], e["mt"] = data, path.stat().st_mtime_ns
        now = time.time()
        if now - e["bak"] >= BAK_EVERY:
            e["bak"] = now
            try:
                shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
                bdir = _backup_dir(path)
                bdir.mkdir(exist_ok=True)
                daily = bdir / f"{path.stem}_{datetime.now():%Y%m%d}{path.suffix}"
                if not daily.exists():
                    shutil.copyfile(path, daily)
                    olds = sorted(bdir.glob(f"{path.stem}_*{path.suffix}"))
                    for old in olds[:-14]:
                        old.unlink(missing_ok=True)
            except OSError:
                pass


def recovered() -> list[str]:
    """복구한 일이 있으면 꺼내 가기 (한 번만)"""
    with _lock:
        out, _recovered[:] = list(_recovered), []
    return out
