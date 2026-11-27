"""⏱️ 2호 급등주 1초 감시 — 가상(paper) 기록 전용.

1분 스캔(picker.surge_scan · early_breakout_scan)이 고른 상위 후보 최대 5개만
- 1초마다 현재가 (여러 종목 한 번에 1건 조회)
- 1초에 한 종목씩 돌아가며 1분봉(체결량) 조회 → 종목마다 약 5초 간격
→ 체결량이 갑자기 늘고 가격이 오르는 순간을 '초반 진입 신호'로 기록하고, 1·5·15분 뒤 결과를 붙임.

⚠️ 주문 함수는 부르지 않음 (조회만). 실전·가상 장부·한도와도 연결 안 됨 — 검증 기록만.
API: 감시 중 초당 최대 2건 (toss.py 의 초당 10건 제한 안), 429 나면 30초 쉼.
기록: _cache/watch1s_<시장>.json
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from . import picker, store, toss

MAX_SYMS = 5            # 감시 종목 수 상한
VOL_SPIKE = 3.0         # 거래량 급증: 직전 1분봉 평균의 3배 이상
UP_10S = 0.2            # 최근 10초 +0.2% 이상 오름
COOLDOWN = 600          # 같은 종목 신호는 10분에 한 번
HORIZONS = (60, 300, 900)   # 결과 확인: 1·5·15분 뒤
PICKS_FRESH = 300       # 스캔 결과가 5분 넘게 오래되면 감시 안 함

_thread: threading.Thread | None = None
_mem: dict = {}         # 시장별 실시간 상태 (파일에 안 씀)


def _path(m: str):
    from .autotrade import STATE
    return STATE.with_name(f"watch1s_{m}.json")


def _load(m: str) -> dict:
    d = store.load(_path(m))
    d.setdefault("signals", [])
    return d


def _targets(m: str) -> list[dict]:
    """오늘 스캔 상위 후보 (급등 → 초반돌파 순, 최대 5개)"""
    from . import autotrade as at
    with at.in_market(m):
        s = at.state()
    out, seen = [], set()
    for key in ("picks", "breakout"):
        r = s.get(key) or {}
        if r.get("day") != picker.session_date(m):
            continue
        try:   # 스캔이 멈춰 오래된 후보면 감시 안 함
            t = picker.mnow(m)
            hh, mi = map(int, str(r.get("time", "")).split(":"))
            if (t.hour * 60 + t.minute) - (hh * 60 + mi) > PICKS_FRESH // 60:
                continue
        except ValueError:
            continue
        for x in r.get("picks") or []:
            sym = x.get("symbol")
            if sym and sym not in seen and x.get("price"):
                seen.add(sym)
                out.append(x)
    return out[:MAX_SYMS]


def _candle(m: str, sym: str, st: dict) -> None:
    """1분봉 최근 12개 → 진행 중인 분의 거래량 · 직전 완성봉 평균"""
    cs = sorted((toss._get("/api/v1/candles", symbol=sym, interval="1m", count=12) or {}).get("candles") or [],
                key=lambda c: c["timestamp"])
    if len(cs) < 4:
        return
    cur, prev = cs[-1], cs[-11:-1]
    avg = sum(float(c.get("volume") or 0) for c in prev) / len(prev)
    vol = float(cur.get("volume") or 0)
    now = time.time()
    v = st.setdefault(sym, {})
    rate = 0.0   # 직전 조회 뒤 늘어난 체결량을 1분 속도로 환산
    if v.get("ts") == cur["timestamp"] and now - v.get("vt", now) > 0:
        rate = max(0.0, vol - v.get("vol", 0)) / (now - v["vt"]) * 60
    v.update(ts=cur["timestamp"], vol=vol, vt=now, avg=avg,
             spike=max(vol, rate) / avg if avg > 0 else 0.0)


def _step(m: str) -> int:
    """1초 한 번: 조회 건수 반환"""
    mm = _mem.setdefault(m, {"vol": {}, "px": {}, "rr": 0, "open": [], "last_sig": {}, "dirty": False})
    mm["open"] = [sg for sg in mm["open"] if sg["day"] == picker.session_date(m)]   # 지난 장 신호는 버림 (기록엔 남음)
    tg = _targets(m)
    if not tg and not mm["open"]:
        return 0
    syms = [x["symbol"] for x in tg]
    info = {x["symbol"]: x for x in tg}
    now = time.time()
    calls = 1
    # 후보에서 빠져도 결과 확인 중인 종목은 같이 조회 (한 번에 1건)
    ask = list(dict.fromkeys(syms + [sg["symbol"] for sg in mm["open"]]))
    px = {k: v["price"] for k, v in toss.prices(ask).items()}
    if syms:   # 종목 하나씩 돌아가며 체결량 (후보 있을 때만)
        sym_v = syms[mm["rr"] % len(syms)]
        mm["rr"] += 1
        calls += 1
        try:
            _candle(m, sym_v, mm["vol"])
        except (toss.TossError, KeyError, ValueError, TypeError):
            pass
    for sym, p in px.items():
        hist = mm["px"].setdefault(sym, [])
        hist.append((now, p))
        del hist[:-90]
    # 열린 가상 신호 결과 갱신
    for sg in mm["open"]:
        p = px.get(sg["symbol"])
        if not p:
            continue
        r = (p / sg["entry"] - 1) * 100
        sg["max"] = round(max(sg["max"], r), 2)
        sg["min"] = round(min(sg["min"], r), 2)
        for h in HORIZONS:
            k = f"r{h // 60}m"
            if k not in sg and now - sg["at"] >= h:
                sg[k] = round(r, 2)
                mm["dirty"] = True
    done = [sg for sg in mm["open"] if f"r{HORIZONS[-1] // 60}m" in sg]
    mm["open"] = [sg for sg in mm["open"] if sg not in done]
    # 새 신호: 거래량 급증 + 최근 10초 상승
    for sym, p in px.items():
        if sym not in info:
            continue
        v = mm["vol"].get(sym) or {}
        if v.get("spike", 0) < VOL_SPIKE or now - v.get("vt", 0) > 15:
            continue
        if now - mm["last_sig"].get(sym, 0) < COOLDOWN or any(sg["symbol"] == sym for sg in mm["open"]):
            continue
        old = [q for t, q in mm["px"][sym] if 10 <= now - t <= 30]
        if not old or (p / old[-1] - 1) * 100 < UP_10S:
            continue
        x = info[sym]
        prev_close = x["price"] / (1 + (x.get("change") or 0) / 100)
        sg = {"t": datetime.now().strftime("%m-%d %H:%M:%S"), "at": now, "day": picker.session_date(m), "symbol": sym,
              "lane": "surge", "paper_only": True,
              "entry": p, "scan_price": x["price"], "chg_now": round((p / prev_close - 1) * 100, 2) if prev_close else None,
              "chg_scan": x.get("change"), "spike": round(v["spike"], 1), "max": 0.0, "min": 0.0,
              "why": f"체결량 직전 평균의 {v['spike']:.1f}배 · 10초 {(p / old[-1] - 1) * 100:+.2f}%"}
        mm["open"].append(sg)
        mm["last_sig"][sym] = now
        mm["dirty"] = True
    if mm["dirty"]:
        _flush(m, done)
    return calls


def _flush(m: str, done: list[dict]) -> None:
    mm = _mem[m]
    d = _load(m)
    by = {(s["symbol"], s["at"]): s for s in d["signals"]}
    for sg in mm["open"] + done:
        by[(sg["symbol"], sg["at"])] = dict(sg)
    d["signals"] = sorted(by.values(), key=lambda s: s["at"])[-500:]
    store.save(_path(m), d)
    mm["dirty"] = False


def summary(m: str, days: int = 10) -> str:
    """최근 결과 한 줄 (수수료·호가차 빼기 전)"""
    sigs = [s for s in _load(m).get("signals", []) if "r15m" in s][-200:]
    if not sigs:
        return "⏱️ 1초 감시 초반 신호(가상): 아직 결과 없음"
    n = len(sigs)
    avg = lambda k: sum(s.get(k, 0) for s in sigs) / n
    win = sum(1 for s in sigs if s["r5m"] > 0) / n * 100
    ch = [s["chg_now"] for s in sigs if s.get("chg_now") is not None]
    return (f"⏱️ 1초 감시 초반 신호(가상) {n}건: 진입 시 평균 {sum(ch) / max(len(ch), 1):+.1f}% 오른 상태 · "
            f"1분 {avg('r1m'):+.2f}% · 5분 {avg('r5m'):+.2f}% (승률 {win:.0f}%) · 15분 {avg('r15m'):+.2f}% · "
            f"최대상승 {avg('max'):+.2f}% / 최대하락 {avg('min'):+.2f}% (비용 전)")


def start(log=print) -> None:
    """자동매매 루프와 따로 도는 1초 감시 (가상 기록만)"""
    global _thread
    if _thread and _thread.is_alive():
        return

    def loop():
        from . import autotrade as at
        pause = 0.0
        while not at._stop_req.is_set():
            t0 = time.time()
            try:
                c = at.config()
                if c["mode"] != "off" and c.get("watch1s_on", True) and t0 >= pause \
                        and c["strategy"] in ("surge", "auto"):
                    for m in at.markets():
                        if at.minutes_now(m, c) is None:
                            continue
                        try:
                            _step(m)
                        except toss.TossError as e:
                            if "너무 잦아요" in str(e):
                                pause = time.time() + 30   # 한도 초과 → 30초 쉼
                                log("⏱️ 1초 감시: 조회 한도 → 30초 쉼")
            except Exception as e:   # 감시 오류로 앱이 멈추지 않게
                log(f"⏱️ 1초 감시 오류: {type(e).__name__}: {e}")
                time.sleep(5)
            time.sleep(max(0.2, 1.0 - (time.time() - t0)))

    _thread = threading.Thread(target=loop, daemon=True, name="watch1s")
    _thread.start()
