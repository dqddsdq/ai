"""🤖 자동매매 — 정해진 규칙으로 사고팔기 (토스증권 오픈API).

모드
- off   : 꺼짐 (기본)
- paper : 가상 매매. 실시간 시세로 규칙을 돌리되 주문은 장부에만 적음 (돈 안 씀)
- live  : 실전. 안전장치를 모두 통과한 지정가 주문만 토스로 보냄

💰 돈은 계좌 밖으로 못 나가요: 토스 API 에 출금·이체 기능이 없고, toss.py 는 '주문 생성·취소'만 허용해요.

안전장치 (live·paper 모두 적용, 코드에서 강제)
- 허용 종목만 매매 · 한 번 주문 최대 금액 · 종목당 최대 보유 금액 · 하루 최대 주문 수
- 하루 손실 한도를 넘으면 그날은 자동 정지 · 미수/신용 안 씀 (주문 가능 현금 안에서만)
- 장 시작 직후·마감 직전 매매 안 함 · 같은 종목 미체결 주문이 있으면 새 주문 안 함
- 10분 넘게 안 채워진 주문은 자동 취소 · 긴급 정지 버튼 (미체결 전부 취소 + 꺼짐)
- (선택) 주문 전에 AI 가 한 번 더 검토해서 반대하면 안 냄

설정: _shared/autotrade.json (🤖 자동매매 화면에서 바꿈, 자기 개선 기능은 못 고침)
기록: _cache/autotrade_state.json (git 에 안 올라감)
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import toss
from .agents import COMPANY_DIR, SHARED_DIR

CONFIG = SHARED_DIR / "autotrade.json"
STATE = COMPANY_DIR / "_cache" / "autotrade_state.json"
KST = timezone(timedelta(hours=9))
DEFAULTS = {
    "mode": "off",
    "symbols": [],                 # 허용 종목 (예: ["005930", "247540", "AAPL"])
    "interval_min": 5,
    "max_total_krw": 100000,       # 자동매매 전체 투자 한도 (허용 종목 보유 합계 + 새 주문이 이걸 못 넘음)
    "max_order_krw": 50000,        # 한 번 주문 최대 금액
    "max_position_krw": 50000,     # 종목당 최대 보유 금액
    "max_orders_per_day": 4,
    "max_daily_loss_krw": 5000,    # 오늘 손실이 이만큼 넘으면 그날 정지
    "stop_loss_pct": 5.0,          # 평단 대비 -5% 면 팔기
    "take_profit_pct": 10.0,       # 평단 대비 +10% 면 팔기
    "rsi_max_buy": 68,             # 과열이면 안 삼
    "ai_check": True,              # 주문 전 AI 검토
    "paper_cash_krw": 1000000,
    "usd_krw": 1400,               # 미국 주식 금액 환산용 대략 환율
}
_lock = threading.RLock()
_thread: threading.Thread | None = None
_log_fn = print


# ── 설정·기록 ──
def config() -> dict:
    try:
        c = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        c = {}
    return {**DEFAULTS, **{k: v for k, v in c.items() if k in DEFAULTS}}


def save_config(new: dict) -> dict:
    """화면에서 받은 값 검사 후 저장."""
    c = config()
    errors = []
    if "mode" in new:
        if new["mode"] not in ("off", "paper", "live"):
            errors.append("모드는 off/paper/live 중 하나")
        elif new["mode"] == "live" and not toss.enabled():
            errors.append("실전은 토스 키(🧰 도구)를 넣은 뒤에 켤 수 있어요")
        else:
            c["mode"] = new["mode"]
    if "symbols" in new:
        syms = [s.strip().upper() for s in (new["symbols"] if isinstance(new["symbols"], list) else str(new["symbols"]).replace(",", " ").split()) if s.strip()]
        bad = [s for s in syms if not re.fullmatch(r"\d{6}|[A-Z][A-Z.\-]{0,9}", toss.symbol(s))]
        if bad:
            errors.append("종목 코드가 이상해요: " + ", ".join(bad))
        else:
            c["symbols"] = list(dict.fromkeys(toss.symbol(s) for s in syms))[:20]
    nums = {"max_total_krw": (10000, 1000000000), "interval_min": (1, 120), "max_order_krw": (10000, 50000000), "max_position_krw": (10000, 100000000),
            "max_orders_per_day": (1, 50), "max_daily_loss_krw": (1000, 50000000), "stop_loss_pct": (0.5, 50),
            "take_profit_pct": (0.5, 200), "rsi_max_buy": (30, 90), "paper_cash_krw": (10000, 1000000000), "usd_krw": (500, 3000)}
    for k, (lo, hi) in nums.items():
        if k in new:
            try:
                v = float(new[k])
                if not lo <= v <= hi:
                    raise ValueError
                c[k] = int(v) if float(v).is_integer() and k != "stop_loss_pct" and k != "take_profit_pct" else v
            except (TypeError, ValueError):
                errors.append(f"{k} 는 {lo}~{hi}")
    if "ai_check" in new:
        c["ai_check"] = bool(new["ai_check"])
    if errors:
        return {"ok": False, "message": " · ".join(errors)}
    CONFIG.write_text(json.dumps(c, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    event("설정", f"모드 {c['mode']} · 종목 {', '.join(c['symbols']) or '없음'}")
    return {"ok": True, "config": c}


def state() -> dict:
    with _lock:
        try:
            s = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            s = {}
        today = datetime.now(KST).strftime("%Y-%m-%d")
        if s.get("day") != today:  # 날이 바뀌면 하루 카운트·정지 초기화 (가상 장부는 유지)
            s.update(day=today, orders_today=0, halted_today="")
        s.setdefault("events", [])
        s.setdefault("paper", {"cash": config()["paper_cash_krw"], "positions": {}, "realized": 0})
        s.setdefault("orders", [])
        return s


def _save(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    s["events"] = s["events"][-300:]
    s["orders"] = s["orders"][-300:]
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


def event(kind: str, text: str, **extra) -> None:
    with _lock:
        s = state()
        s["events"].append({"t": datetime.now(KST).strftime("%m-%d %H:%M:%S"), "kind": kind, "text": text, **extra})
        _save(s)
    _log_fn(f"🤖 [{kind}] {text}")


# ── 시장 시간 ──
def market_open(sym: str, now: datetime | None = None) -> bool:
    """대략적인 정규장 (휴장일은 토스가 order-hours-closed 로 거절 → 그냥 건너뜀).
    국내 09:10~15:10 (시작 직후·마감 직전 제외), 미국 23:40~05:50 KST (서머타임이면 22:40~04:50)."""
    now = now or datetime.now(KST)
    if re.fullmatch(r"\d{6}", sym):
        return now.weekday() < 5 and "09:10" <= now.strftime("%H:%M") <= "15:10"
    us = now.astimezone(timezone(timedelta(hours=-5)))
    dst = 3 <= us.month <= 10 or (us.month == 11 and us.day < 3)
    us_local = now.astimezone(timezone(timedelta(hours=-4 if dst else -5)))
    return us_local.weekday() < 5 and "09:40" <= us_local.strftime("%H:%M") <= "15:50"


# ── 규칙 (추세 따라가기 + 손절·익절) ──
def _ma(xs: list[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def _rsi(xs: list[float], n: int = 14) -> float | None:
    if len(xs) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(xs[-n - 1:-1], xs[-n:]):
        d = b - a
        gains += max(d, 0); losses += max(-d, 0)
    return 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)


def signal(closes: list[float], price: float, held_qty: float, avg_price: float, c: dict) -> tuple[str, str]:
    """('BUY'|'SELL'|'HOLD', 이유)"""
    series = closes + [price]
    ma20, ma60, rsi = _ma(series, 20), _ma(series, 60), _rsi(series)
    if held_qty > 0 and avg_price > 0:
        pnl = (price / avg_price - 1) * 100
        if pnl <= -c["stop_loss_pct"]:
            return "SELL", f"손절: 평단 대비 {pnl:.1f}% (기준 -{c['stop_loss_pct']}%)"
        if pnl >= c["take_profit_pct"]:
            return "SELL", f"익절: 평단 대비 +{pnl:.1f}% (기준 +{c['take_profit_pct']}%)"
        if ma20 and price < ma20 * 0.99:
            return "SELL", f"추세 이탈: 현재가가 20일선({ma20:,.0f}) 아래"
        return "HOLD", f"보유 유지 (평단 대비 {pnl:+.1f}%)"
    if not (ma20 and ma60 and rsi is not None):
        return "HOLD", "차트 데이터 부족 (60일 이상 필요)"
    if price > ma20 > ma60 and 45 <= rsi <= c["rsi_max_buy"]:
        return "BUY", f"상승 추세: 현재가 > 20일선({ma20:,.0f}) > 60일선({ma60:,.0f}), RSI {rsi:.0f}"
    return "HOLD", f"조건 안 맞음 (20일선 {ma20:,.0f} · 60일선 {ma60:,.0f} · RSI {rsi:.0f})"


def _ai_ok(sym: str, side: str, reason: str, price: float, closes: list[float]) -> tuple[bool, str]:
    """주문 전 AI 한 번 더 검토. 응답이 없거나 애매하면 안전하게 '반대'."""
    from . import providers as pv
    from . import team as tm
    prompt = (f"자동매매 규칙이 {sym} {('매수' if side == 'BUY' else '매도')} 신호를 냈어요. 이유: {reason}. 현재가 {price}. "
              f"최근 20일 종가: {[round(x, 2) for x in closes[-20:]]}.\n"
              "이 주문을 지금 내도 되는지 한 줄 이유와 함께 마지막 줄에 '판정: 찬성' 또는 '판정: 반대'로만 답하세요. "
              "매도(특히 손절)는 특별한 이유가 없으면 찬성하세요.")
    try:
        reply = pv.chat(tm.fast_spec(), "너는 신중한 리스크 관리자다. 짧게 답한다.", [{"role": "user", "content": prompt}],
                        effort="low", timeout=90)
    except Exception as e:
        return side == "SELL", f"AI 검토 실패({type(e).__name__}) → {'매도는 진행' if side == 'SELL' else '매수 보류'}"
    last = [l for l in reply.strip().splitlines() if "판정" in l][-1:] or [""]
    ok = "찬성" in last[0] and "반대" not in last[0]
    return ok, reply.strip().splitlines()[0][:160]


# ── 한 바퀴 ──
def _positions_live() -> dict[str, dict]:
    h = toss.holdings()
    out = {}
    for it in h.get("items") or []:
        out[it["symbol"]] = {"qty": float(it.get("quantity") or 0), "avg": float(it.get("averagePurchasePrice") or 0),
                             "currency": it.get("currency", "KRW")}
    dpl = ((h.get("dailyProfitLoss") or {}).get("amount") or {})
    out["_daily_pl_krw"] = float(dpl.get("krw") or 0) + float(dpl.get("usd") or 0) * config()["usd_krw"]
    return out


def run_once(force: bool = False) -> dict:
    """규칙을 한 번 돌림. force=True 면 장 시간이 아니어도 신호만 계산 (주문은 장 시간에만)."""
    with _lock:
        c, s = config(), state()
    mode = c["mode"]
    if mode == "off" and not force:
        return {"ok": True, "message": "꺼져 있어요"}
    if s.get("halted_today"):
        return {"ok": False, "message": "오늘은 정지됨: " + s["halted_today"]}
    if not c["symbols"]:
        return {"ok": False, "message": "허용 종목이 없어요"}
    if not toss.enabled():
        return {"ok": False, "message": "토스 키가 없어요 (시세가 필요해요)"}
    live = mode == "live"
    results = []
    try:
        if live:
            pos = _positions_live()
            _cancel_stale()
            if pos.get("_daily_pl_krw", 0) <= -c["max_daily_loss_krw"]:
                return _halt_today(f"오늘 손실 {pos['_daily_pl_krw']:,.0f}원 — 한도 {c['max_daily_loss_krw']:,}원 초과")
            pending = {o.get("symbol") for o in toss.open_orders()}
            cash = {cur: float((toss.buying_power(cur) or {}).get("cashBuyingPower") or 0) for cur in ("KRW", "USD")}
        else:
            p = s["paper"]
            pos = {k: {"qty": v["qty"], "avg": v["avg"]} for k, v in p["positions"].items()}
            pending, cash = set(), {"KRW": p["cash"], "USD": p["cash"] / c["usd_krw"]}
        quotes = toss.prices(c["symbols"] + [k for k in pos if not k.startswith("_") and k not in c["symbols"]])
        def _fx(sym): return 1 if re.fullmatch(r"\d{6}", sym) else c["usd_krw"]
        invested = sum(v["qty"] * quotes.get(k, {}).get("price", v["avg"]) * _fx(k)   # 허용 종목 보유 평가액 합계
                       for k, v in pos.items() if not k.startswith("_") and k in c["symbols"])
    except toss.TossError as e:
        event("오류", str(e))
        return {"ok": False, "message": str(e)}

    for sym in c["symbols"]:
        q = quotes.get(sym)
        if not q:
            results.append(f"{sym}: 시세 없음"); continue
        price, us = q["price"], not re.fullmatch(r"\d{6}", sym)
        fx = c["usd_krw"] if us else 1
        try:
            closes, _, _ = toss.daily(sym, 120)
        except toss.TossError as e:
            results.append(f"{sym}: 차트 실패 {e}"); continue
        held = pos.get(sym, {"qty": 0, "avg": 0})
        side, reason = signal(closes, price, held["qty"], held["avg"], c)
        if side == "HOLD":
            results.append(f"{sym}: {reason}"); continue
        if not market_open(sym):
            results.append(f"{sym}: {('매수' if side == 'BUY' else '매도')} 신호지만 장 시간이 아님 — {reason}"); continue
        if sym in pending:
            results.append(f"{sym}: 미체결 주문이 있어서 대기"); continue
        with _lock:
            if state()["orders_today"] >= c["max_orders_per_day"]:
                results.append(f"{sym}: 오늘 주문 수 한도({c['max_orders_per_day']}) 도달"); continue
        if side == "BUY":
            held_value = held["qty"] * price * fx
            budget = min(c["max_order_krw"], c["max_position_krw"] - held_value, c["max_total_krw"] - invested,
                         cash["USD" if us else "KRW"] * fx)
            qty = int(budget // (price * fx))
            if qty < 1:
                results.append(f"{sym}: 매수 신호지만 한도·현금 안에서 1주도 못 사요 (가능 {budget:,.0f}원)"); continue
        else:
            qty = int(held["qty"])
            if qty < 1:
                results.append(f"{sym}: 팔 수량 없음 (소수점 보유분은 앱에서 직접)"); continue
        if c["ai_check"]:
            ok, why = _ai_ok(sym, side, reason, price, closes)
            if not ok:
                event("AI 반대", f"{sym} {side} {qty}주 보류 — {why}", symbol=sym)
                results.append(f"{sym}: AI 가 반대해서 보류 — {why}"); continue
        results.append(_execute(sym, side, qty, price, reason, live, c))
        if side == "BUY":
            invested += qty * price * fx
    return {"ok": True, "mode": mode, "results": results}


def _execute(sym: str, side: str, qty: int, price: float, reason: str, live: bool, c: dict) -> str:
    coid = "cai-" + uuid.uuid4().hex[:20]
    label = f"{sym} {'매수' if side == 'BUY' else '매도'} {qty}주 @ {toss.order_price(sym, price, side)}"
    rec = {"t": datetime.now(KST).strftime("%m-%d %H:%M:%S"), "symbol": sym, "side": side, "qty": qty, "price": price,
           "reason": reason, "mode": "live" if live else "paper", "client_order_id": coid}
    if live:
        try:
            r = toss.place_limit_order(sym, side, qty, price, coid)
            rec.update(order_id=r.get("orderId"), status="sent")
        except toss.TossError as e:
            rec.update(status="rejected", error=str(e))
            with _lock:
                s = state(); s["orders"].append(rec); _save(s)
            event("주문 거절", f"{label} — {e}", symbol=sym)
            return f"{sym}: 주문 거절 — {e}"
    else:  # 가상 체결 (현재가에 바로 체결됐다고 가정)
        with _lock:
            s = state(); p = s["paper"]
            fx = 1 if re.fullmatch(r"\d{6}", sym) else c["usd_krw"]
            amt = qty * price * fx
            cur = p["positions"].get(sym, {"qty": 0, "avg": 0})
            if side == "BUY":
                p["cash"] -= amt
                tot = cur["qty"] + qty
                p["positions"][sym] = {"qty": tot, "avg": (cur["avg"] * cur["qty"] + price * qty) / tot}
            else:
                p["cash"] += amt
                p["realized"] += (price - cur["avg"]) * qty * fx
                p["positions"].pop(sym, None)
            _save(s)
        rec["status"] = "paper_filled"
    with _lock:
        s = state(); s["orders"].append(rec); s["orders_today"] += 1; _save(s)
    event("실전 주문" if live else "가상 주문", f"{label} — {reason}", symbol=sym)
    return f"{sym}: {'✅ 주문 보냄' if live else '📝 가상 체결'} {label}"


def _cancel_stale(minutes: int = 10) -> None:
    now = datetime.now(KST)
    for o in toss.open_orders():
        try:
            t = datetime.fromisoformat(str(o.get("orderedAt")).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=KST)
        except ValueError:
            continue
        if now - t > timedelta(minutes=minutes) and o.get("status") in ("PENDING", "PARTIAL_FILLED"):
            try:
                toss.cancel_order(o["orderId"])
                event("자동 취소", f"{o.get('symbol')} {minutes}분 넘게 미체결 → 취소", symbol=o.get("symbol"))
            except toss.TossError as e:
                event("오류", f"취소 실패 {o.get('symbol')}: {e}")


def _halt_today(why: str) -> dict:
    with _lock:
        s = state(); s["halted_today"] = why; _save(s)
    event("정지", why)
    return {"ok": False, "message": "오늘은 정지: " + why}


def emergency_stop() -> dict:
    """긴급 정지: 모드 끄기 + (실전이면) 미체결 주문 전부 취소."""
    c = config(); was = c["mode"]
    c["mode"] = "off"
    CONFIG.write_text(json.dumps(c, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    canceled, errors = 0, []
    if was == "live" and toss.enabled():
        try:
            for o in toss.open_orders():
                try:
                    toss.cancel_order(o["orderId"]); canceled += 1
                except toss.TossError as e:
                    errors.append(str(e))
        except toss.TossError as e:
            errors.append(str(e))
    event("긴급 정지", f"꺼짐 (이전 {was}) · 미체결 취소 {canceled}건" + (f" · 실패: {errors[0]}" if errors else ""))
    return {"ok": not errors, "canceled": canceled, "message": f"자동매매를 껐어요. 미체결 {canceled}건 취소" + (" (일부 실패)" if errors else "")}


def reset_paper() -> dict:
    with _lock:
        s = state(); s["paper"] = {"cash": config()["paper_cash_krw"], "positions": {}, "realized": 0}; _save(s)
    event("가상 초기화", "가상 장부를 처음부터")
    return {"ok": True}


def overview() -> dict:
    c, s = config(), state()
    p = s["paper"]
    return {"config": c, "toss": toss.enabled(), "day": s["day"], "orders_today": s["orders_today"],
            "halted_today": s.get("halted_today", ""), "paper": p, "orders": s["orders"][-50:][::-1],
            "events": s["events"][-80:][::-1], "running": bool(_thread and _thread.is_alive())}


# ── 백그라운드 ──
def start(log_fn=print) -> None:
    """웹 서버가 켜질 때 한 번. 모드가 off 면 쉬기만 함."""
    global _thread, _log_fn
    _log_fn = log_fn
    if _thread and _thread.is_alive():
        return

    def loop():
        last = 0.0
        while True:
            time.sleep(15)
            c = config()
            if c["mode"] == "off" or time.time() - last < c["interval_min"] * 60:
                continue
            last = time.time()
            try:
                r = run_once()
                for line in r.get("results", []):
                    if "신호지만 장 시간이 아님" not in line and "조건 안 맞음" not in line:
                        _log_fn("🤖 " + line)
            except Exception as e:  # 루프는 절대 죽지 않게
                event("오류", f"{type(e).__name__}: {e}")

    _thread = threading.Thread(target=loop, daemon=True, name="autotrade")
    _thread.start()
