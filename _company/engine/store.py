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


_errors: list[str] = []
_err_at: dict[str, float] = {}


def write_text(path: Path, text: str) -> None:
    """안전하게 쓰기: 임시 파일 → 바꿔치기.
    윈도우에서 백신·OneDrive·검색 색인이 파일을 잠깐 잡고 있으면 바꿔치기가 '액세스 거부'로 실패해서
    → 조금씩 기다리며 다시 시도 → 그래도 안 되면 직접 덮어쓰기. 다 안 되면 OSError"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident() % 100000}.tmp")   # 쓰는 곳마다 다른 임시 파일
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    last = None
    for wait in (0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.0):
        if wait:
            time.sleep(wait)
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:   # 다른 프로그램이 잠깐 잡고 있음
            last = e
        except OSError as e:
            last = e
            break
    try:   # 마지막 방법: 직접 덮어쓰기 (원래 내용은 .bak 에 있음)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return
    except OSError as e:
        last = e
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    raise last


def save(path: Path, data) -> None:
    """임시 파일에 쓰고 바꿔치기 + 가끔 백업. 디스크에 못 써도 예외를 안 냄(메모리엔 반영, 다음 저장 때 다시 시도)
    → 자동매매가 저장 실패로 멈추지 않게"""
    path = Path(path)
    key = str(path)
    with _lock:
        e = _cache.setdefault(key, {"data": data, "mt": None, "bak": 0})
        e["data"] = data
        try:
            write_text(path, json.dumps(data, ensure_ascii=False))
        except OSError as err:
            now = time.time()
            if now - _err_at.get(key, 0) > 300:   # 같은 파일 알림은 5분에 한 번
                _err_at[key] = now
                _errors.append(f"{path.name} 저장 실패 ({type(err).__name__}) — 다른 프로그램(백신·OneDrive 등)이 잡고 있는 것 같아요. 다음 저장 때 다시 시도해요")
            return
        e["mt"] = path.stat().st_mtime_ns
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


def errors() -> list[str]:
    """저장 실패 알림 꺼내 가기 (한 번만)"""
    with _lock:
        out, _errors[:] = list(_errors), []
    return out


def recovered() -> list[str]:
    """복구한 일이 있으면 꺼내 가기 (한 번만)"""
    with _lock:
        out, _recovered[:] = list(_recovered), []
    return out
