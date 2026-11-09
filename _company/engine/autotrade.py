"""🤖 자동매매 — 토스증권 오픈API로 규칙대로 사고팔기.

모드: off(꺼짐, 기본) · paper(가상: 주문은 장부에만) · live(실전: 토스로 지정가 주문)
시장: US(미장, 기본) · KR(국장)
전략: surge(급등주, 기본) · trend(꾸준한 상승 추세, 국장)
예산: all_cash(계좌의 그 통화 현금 전부, 기본) · fixed(총 투자 한도 금액)

💰 돈은 계좌 밖으로 못 나가요: 토스 API 에 출금·이체 기능이 없고, toss.py 는 '주문 생성·취소'만 허용해요.

급등주 규칙
- 사기: 장중 몇 분마다 picker.surge_scan 이 고른 '오늘 +N% 이상 + 거래량 폭증' 종목 (장 시작 10분 뒤 ~ 마감 30분 전)
- 팔기: 손절(-3%) · 고점 대비 하락(-4%, 수익 지키기) · 익절(+10%) · 장 마감 15분 전 전부 정리 (밤새 안 들고 감)

안전장치 (코드에서 강제)
- 자동매매가 산 주식만 사고팔고, 사장님이 직접 산 주식·직접 낸 주문은 안 건드림
- 예산(현금 전부 또는 고정 한도)을 고른 종목 수로 나눠 종목당 상한 · 미수/신용 안 씀
- 하루 손실 한도(자동매매 손익 기준)를 넘으면 그날 정지 · 하루 매수 횟수 한도 (매도는 언제나)
- 같은 종목 미체결이 있으면 새 주문 안 함 · 몇 분 안에 안 채워진 주문 자동 취소
- 긴급 정지: 꺼짐 + 자동매매가 낸 미체결 주문 전부 취소
- (선택) 주문 전 AI 검토

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

from . import picker, toss
from .agents import COMPANY_DIR, SHARED_DIR

CONFIG = SHARED_DIR / "autotrade.json"
STATE = COMPANY_DIR / "_cache" / "autotrade_state.json"
KST = timezone(timedelta(hours=9))
DEFAULTS = {
    "v": 2,                        # 설정 형식 버전 (예전 기본값 자동 갱신용)
    "mode": "off",
    "market": "US",
    "strategy": "surge",
    "auto_pick": True,             # 종목을 규칙이 알아서 고름
    "pick_count": 3,               # 동시에 들고 갈 최대 종목 수 (예산을 이 수로 나눔)
    "symbols": [],                 # 직접 고른 종목 (auto_pick 끄면 이것만)
    "budget_mode": "all_cash",     # all_cash = 계좌의 그 통화 현금 전부 / fixed = max_total_krw
    "max_total_krw": 100000,       # fixed 일 때 총 투자 한도
    "max_order_krw": 50000,        # fixed 일 때 한 번 주문 최대
    "max_position_krw": 50000,     # fixed 일 때 종목당 최대
    "max_orders_per_day": 6,       # 하루 매수 횟수 (매도는 제한 없음)
    "monthly_target_pct": 10.0,    # 한 달 목표 수익률
    "stop_at_target": True,        # 목표 달성하면 그달은 새로 안 삼 (번 돈 지키기)
    "max_daily_loss_pct": 5.0,     # all_cash: 예산 대비 하루 손실 한도 %
    "max_daily_loss_krw": 5000,    # fixed: 하루 손실 한도 금액
    "stop_loss_pct": 3.0,
    "take_profit_pct": 10.0,
    "trail_pct": 4.0,              # 산 뒤 최고가 대비 이만큼 떨어지면 팔기 (급등주)
    "surge_min_change_pct": 5.0,   # 오늘 이만큼 이상 오른 종목
    "surge_max_change_pct": 40.0,  # 너무 많이 오른 건 제외 (국장은 상한가 30%)
    "surge_volume_ratio": 3.0,     # 평소 거래량의 몇 배 이상
    "us_premarket": True,          # 미장 프리장(뉴욕 04:00~)부터 매매
    "pre_volume_ratio": 0.2,       # 프리장: 평소 하루 거래량의 이만큼(20%)이 벌써 거래된 종목
    "min_trading_value_krw": 3000000000,  # 하루 거래대금 최소 (원 환산)
    "us_min_price": 1.0,           # 1달러 미만 동전주 제외
    "limit_slip_pct": 0.5,         # 급등주: 사는 지정가는 현재가 +0.5%, 파는 건 -0.5% (체결 잘 되게)
    "rsi_max_buy": 68,             # 추세 전략: 과열 기준
    "interval_min": 1,             # 급등 종목 찾기 간격 (보유 종목 손절 확인은 따로 20초마다)
    "exit_check_sec": 10,
    "ai_check": False,             # 주문 전 AI 검토 (급등주는 속도가 중요해서 기본 꺼짐)
    "paper_cash_krw": 1000000,
    "usd_krw": 1400,               # 환율 (토스에서 자동 갱신, 실패하면 이 값)
}
NUMS = {"monthly_target_pct": (1, 100), "pick_count": (1, 10), "max_total_krw": (10000, 1e10), "max_order_krw": (10000, 1e10), "max_position_krw": (10000, 1e10),
        "max_orders_per_day": (1, 100), "max_daily_loss_pct": (0.5, 50), "max_daily_loss_krw": (1000, 1e10),
        "stop_loss_pct": (0.5, 50), "take_profit_pct": (0.5, 500), "trail_pct": (0.5, 50), "surge_min_change_pct": (1, 100),
        "surge_max_change_pct": (2, 500), "surge_volume_ratio": (1, 50), "pre_volume_ratio": (0.01, 5), "min_trading_value_krw": (0, 1e13), "us_min_price": (0.1, 100),
        "limit_slip_pct": (0, 3), "exit_check_sec": (10, 300), "rsi_max_buy": (30, 90), "interval_min": (1, 120), "paper_cash_krw": (10000, 1e10), "usd_krw": (500, 3000)}
CHOICES = {"mode": ("off", "paper", "live"), "market": ("US", "KR"), "strategy": ("surge", "trend"), "budget_mode": ("all_cash", "fixed")}
_lock = threading.RLock()
_thread: threading.Thread | None = None
_log_fn = print
_busy = threading.Event()       # 종목 준비/고르기 백그라운드 작업 중


# ── 설정·기록 ──
def config() -> dict:
    try:
        c = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        c = {}
    out = {**DEFAULTS, **{k: v for k, v in c.items() if k in DEFAULTS}}
    if int(c.get("v", 1)) < 2:   # 예전 기본값(2분)을 그대로 쓰던 경우 → 새 기본값 1분
        if c.get("interval_min") == 2:
            out["interval_min"] = 1
        out["v"] = 2
    return out


def save_config(new: dict) -> dict:
    c, errors = config(), []
    for k, opts in CHOICES.items():
        if k in new:
            if new[k] not in opts:
                errors.append(f"{k} 는 {'/'.join(opts)} 중 하나")
            elif k == "mode" and new[k] == "live" and not toss.enabled():
                errors.append("실전은 토스 키(🧰 도구)를 넣은 뒤에 켤 수 있어요")
            else:
                c[k] = new[k]
    if c["strategy"] == "trend" and c["market"] == "US":
        errors.append("추세 전략은 지금 국장만 돼요 (미장은 급등주)")
    if "symbols" in new:
        raw = new["symbols"] if isinstance(new["symbols"], list) else str(new["symbols"]).replace(",", " ").split()
        syms = [toss.symbol(s) for s in raw if s.strip()]
        bad = [s for s in syms if not re.fullmatch(r"\d{6}|[A-Z][A-Z.\-]{0,9}", s)]
        if bad:
            errors.append("종목 코드가 이상해요: " + ", ".join(bad))
        else:
            c["symbols"] = list(dict.fromkeys(syms))[:20]
    for k, (lo, hi) in NUMS.items():
        if k in new and new[k] not in (None, ""):
            try:
                v = float(new[k])
                if not lo <= v <= hi:
                    raise ValueError
                c[k] = int(v) if v.is_integer() and isinstance(DEFAULTS[k], int) else v
            except (TypeError, ValueError):
                errors.append(f"{k} 는 {lo:g}~{hi:g}")
    for k in ("ai_check", "auto_pick", "stop_at_target", "us_premarket"):
        if k in new:
            c[k] = bool(new[k])
    if errors:
        return {"ok": False, "message": " · ".join(errors)}
    CONFIG.write_text(json.dumps(c, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    event("설정", f"모드 {c['mode']} · {'미장' if c['market'] == 'US' else '국장'} · {'급등주' if c['strategy'] == 'surge' else '추세'} · "
                f"{'현금 전부' if c['budget_mode'] == 'all_cash' else '한도 ' + format(c['max_total_krw'], ',') + '원'}"
                + ("" if c["auto_pick"] else f" · 종목 {', '.join(c['symbols']) or '없음'}"))
    return {"ok": True, "config": c}


def _fresh_day(s: dict, market: str) -> dict:
    day = picker.session_date(market)
    if s.get("day") != day:  # 장(현지 날짜)이 바뀌면 하루 카운트·정지·손익 초기화
        s.update(day=day, orders_today=0, halted_today="", realized_today=0.0, highs={})
    return s


def state() -> dict:
    with _lock:
        try:
            s = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            s = {}
        for k, v in (("events", []), ("orders", []), ("bot_pos", {}), ("picks", {}), ("highs", {}), ("realized_today", 0.0), ("month", {})):
            s.setdefault(k, v)
        s.setdefault("paper", {"cash": config()["paper_cash_krw"], "positions": {}, "realized": 0})
        for k, v in list(s["bot_pos"].items()):  # 예전 형식(수량만) 호환
            if not isinstance(v, dict):
                s["bot_pos"][k] = {"qty": float(v), "avg": 0.0}
        return _fresh_day(s, config()["market"])


def _save(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    s["events"] = s["events"][-400:]
    s["orders"] = s["orders"][-400:]
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


def event(kind: str, text: str, **extra) -> None:
    with _lock:
        s = state()
        s["events"].append({"t": datetime.now(KST).strftime("%m-%d %H:%M:%S"), "kind": kind, "text": text, **extra})
        _save(s)
    _log_fn(f"🤖 [{kind}] {text}")


def _mkt(sym: str) -> str:
    return "KR" if re.fullmatch(r"\d{6}", sym) else "US"


def _ext(market: str, c: dict | None = None) -> bool:
    return market == "US" and bool((c or config()).get("us_premarket"))


def minutes_now(market: str, c: dict | None = None) -> int | None:
    return picker.session_minutes(market, extended=_ext(market, c))


def market_open(sym: str, side: str = "BUY", now: datetime | None = None) -> bool:
    """매수: (프리장 켜면 뉴욕 04:10~ / 아니면 정규장 시작 10분 뒤~) ~ 마감 30분 전
    매도: 장(프리장 포함) 시작 1분 뒤 ~ 마감 2분 전 (휴장일은 토스가 거절 → 건너뜀)"""
    mk = _mkt(sym)
    m = picker.session_minutes(mk, now, extended=_ext(mk))
    if m is None:
        return False
    first = -330 if _ext(mk) else 0
    return (first + 10 <= m <= 360) if side == "BUY" else (first + 1 <= m <= 388)


# ── 환율 ──
_fx_cache = [0.0, 0.0]


def usd_krw(c: dict) -> float:
    if time.time() - _fx_cache[0] < 600 and _fx_cache[1]:
        return _fx_cache[1]
    rate = float(c["usd_krw"])
    try:
        r = toss._get("/api/v1/exchange-rate", baseCurrency="USD", quoteCurrency="KRW") or {}
        for k in ("rate", "exchangeRate", "basePrice", "price", "value"):
            if r.get(k):
                rate = float(r[k]); break
    except (toss.TossError, ValueError, TypeError):
        pass
    if 500 <= rate <= 3000:
        _fx_cache[:] = [time.time(), rate]
    return rate


# ── 규칙 ──
def _ma(xs: list[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def _rsi(xs: list[float], n: int = 14) -> float | None:
    if len(xs) <= n:
        return None
    g = l = 0.0
    for a, b in zip(xs[-n - 1:-1], xs[-n:]):
        g += max(b - a, 0); l += max(a - b, 0)
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def signal_trend(closes: list[float], price: float, qty: float, avg: float, c: dict) -> tuple[str, str]:
    series = closes + [price]
    ma20, ma60, rsi = _ma(series, 20), _ma(series, 60), _rsi(series)
    if qty > 0 and avg > 0:
        pnl = (price / avg - 1) * 100
        if pnl <= -c["stop_loss_pct"]:
            return "SELL", f"손절: 평단 대비 {pnl:.1f}%"
        if pnl >= c["take_profit_pct"]:
            return "SELL", f"익절: 평단 대비 +{pnl:.1f}%"
        if ma20 and price < ma20 * 0.99:
            return "SELL", f"추세 이탈: 20일선({ma20:,.2f}) 아래"
        return "HOLD", f"보유 유지 (평단 대비 {pnl:+.1f}%)"
    if not (ma20 and ma60 and rsi is not None):
        return "HOLD", "차트 데이터 부족"
    if price > ma20 > ma60 and 45 <= rsi <= c["rsi_max_buy"]:
        return "BUY", f"상승 추세: 현재가 > 20일선 > 60일선, RSI {rsi:.0f}"
    return "HOLD", f"조건 안 맞음 (RSI {rsi:.0f})"


def signal_surge(price: float, qty: float, avg: float, high: float, minutes: int | None, picked: bool, c: dict) -> tuple[str, str]:
    if qty > 0 and avg > 0:
        pnl = (price / avg - 1) * 100
        drop = (price / high - 1) * 100 if high else 0
        if minutes is not None and minutes >= 375:
            return "SELL", f"장 마감 15분 전 정리 (평단 대비 {pnl:+.1f}%)"
        if pnl <= -c["stop_loss_pct"]:
            return "SELL", f"손절: 평단 대비 {pnl:.1f}% (기준 -{c['stop_loss_pct']}%)"
        if pnl >= c["take_profit_pct"]:
            return "SELL", f"익절: 평단 대비 +{pnl:.1f}%"
        if pnl > 0 and drop <= -c["trail_pct"]:
            return "SELL", f"고점 대비 {drop:.1f}% 하락 → 수익 지키기 (평단 대비 {pnl:+.1f}%)"
        return "HOLD", f"보유 유지 (평단 대비 {pnl:+.1f}% · 고점 대비 {drop:.1f}%)"
    if not picked:
        return "HOLD", "선택 종목 아님"
    if minutes is None or minutes > 360:
        return "HOLD", "마감 30분 전부터는 새로 안 삼"
    return "BUY", "급등 + 거래량 폭증"


def _ai_ok(sym: str, side: str, reason: str, price: float) -> tuple[bool, str]:
    from . import providers as pv
    from . import team as tm
    prompt = (f"자동매매 규칙이 {sym} {('매수' if side == 'BUY' else '매도')} 신호를 냈어요. 이유: {reason}. 현재가 {price}.\n"
              "지금 내도 되는지 한 줄 이유와 함께 마지막 줄에 '판정: 찬성' 또는 '판정: 반대'. 매도는 특별한 이유가 없으면 찬성.")
    try:
        reply = pv.chat(tm.fast_spec(), "너는 리스크 관리자다. 짧게 답한다.", [{"role": "user", "content": prompt}], effort="low", timeout=60)
    except Exception as e:
        return side == "SELL", f"AI 검토 실패({type(e).__name__})"
    last = [l for l in reply.strip().splitlines() if "판정" in l][-1:] or [""]
    return ("찬성" in last[0] and "반대" not in last[0]), reply.strip().splitlines()[0][:160]


# ── 종목 준비·고르기 (백그라운드) ──
def _bg(name: str, fn) -> dict:
    if _busy.is_set():
        return {"ok": True, "message": "이미 준비 중이에요"}

    def job():
        _busy.set()
        try:
            fn()
        except Exception as e:
            event("오류", f"{name} 실패: {type(e).__name__}: {e}")
        finally:
            _busy.clear()
    threading.Thread(target=job, daemon=True, name=name).start()
    return {"ok": True, "message": f"{name} 시작 (몇 분 걸려요)"}


def _prep_cfg(c: dict) -> dict:
    """준비용: 종목당 예산(원)을 대략 계산해서 '1주도 못 사는 비싼 종목'을 미리 빼게 함"""
    c = dict(c)
    if c["budget_mode"] == "all_cash" and toss.enabled():
        try:
            fx = 1.0 if c["market"] == "KR" else usd_krw(c)
            cur = "KRW" if c["market"] == "KR" else "USD"
            cash = float((toss.buying_power(cur) or {}).get("cashBuyingPower") or 0)
            with _lock:
                held = sum(v["qty"] * v["avg"] for k, v in state()["bot_pos"].items() if _mkt(k) == c["market"])
            c["max_order_krw"] = (cash + held) * fx / c["pick_count"]
            c["usd_krw"] = fx
        except toss.TossError:
            c["max_order_krw"] = 0          # 모르면 가격 상한 없이
    return c


def prepare_now() -> dict:
    """급등주 기준값 준비 / 추세 종목 고르기."""
    c = config()
    if c["strategy"] == "surge":
        def go():
            b = picker.prepare_day(_prep_cfg(c), log=_log_fn)
            event("준비", f"{'미장' if c['market'] == 'US' else '국장'} 급등주 기준값 {len(b.get('rows') or {})}개 준비 완료")
        return _bg("급등주 준비", go)

    def go2():
        r = picker.scan(c, log=_log_fn)
        with _lock:
            s = state(); s["picks"] = r; _save(s)
        event("종목 선택", r["note"] + (" — " + ", ".join(f"{p['name']}({p['symbol']})" for p in r["picks"]) if r["picks"] else ""))
    return _bg("추세 종목 고르기", go2)


scan_now = prepare_now   # 예전 이름 호환


# ── 체결 확인 ──
def _reconcile(fx: float) -> None:
    with _lock:
        todo = [o for o in state()["orders"] if o.get("mode") == "live" and o.get("order_id") and not o.get("final")]
    for o in todo:
        try:
            r = toss.get_order(o["order_id"])
        except toss.TossError:
            continue
        ex = r.get("execution") or {}
        filled = float(ex.get("filledQuantity") or 0)
        fill_px = float(ex.get("averageFilledPrice") or o["price"])
        with _lock:
            s = state()
            rec = next((x for x in s["orders"] if x.get("order_id") == o["order_id"]), None)
            if not rec:
                continue
            delta = filled - float(rec.get("counted", 0))
            if delta > 0:
                bp = s["bot_pos"].get(rec["symbol"], {"qty": 0.0, "avg": 0.0})
                f = 1 if _mkt(rec["symbol"]) == "KR" else fx
                if rec["side"] == "BUY":
                    tot = bp["qty"] + delta
                    s["bot_pos"][rec["symbol"]] = {"qty": tot, "avg": (bp["avg"] * bp["qty"] + fill_px * delta) / tot}
                    s["highs"][rec["symbol"]] = max(s["highs"].get(rec["symbol"], 0), fill_px)
                else:
                    gain = (fill_px - bp["avg"]) * delta * f
                    s["realized_today"] = float(s.get("realized_today", 0)) + gain
                    s["month"]["realized"] = float(s["month"].get("realized", 0)) + gain
                    left = bp["qty"] - delta
                    if left > 1e-9:
                        s["bot_pos"][rec["symbol"]] = {"qty": left, "avg": bp["avg"]}
                    else:
                        s["bot_pos"].pop(rec["symbol"], None); s["highs"].pop(rec["symbol"], None)
                rec["counted"] = filled
            st = r.get("status") or ""
            if st:
                rec["status"] = st.lower()
            if st in ("FILLED", "CANCELED", "REJECTED", "REPLACED"):
                rec["final"] = True
            _save(s)
        if delta > 0:
            event("체결", f"{o['symbol']} {'매수' if o['side'] == 'BUY' else '매도'} {delta:g}주 @ {fill_px:g}", symbol=o["symbol"])


def _bot_order_ids() -> set[str]:
    with _lock:
        return {o["order_id"] for o in state()["orders"] if o.get("order_id")}


def _cancel_stale(minutes: float) -> None:
    """자동매매가 낸 미체결 주문 중 오래된 것 취소 (매도는 더 빨리: 다음 바퀴에 더 낮은 가격으로 다시 냄)"""
    now = datetime.now(timezone.utc)
    mine = _bot_order_ids()
    for o in [o for o in toss.open_orders() if o.get("orderId") in mine]:
        limit = minutes if o.get("side") != "SELL" else min(minutes, 1.5)
        try:
            t = datetime.fromisoformat(str(o.get("orderedAt")).replace("Z", "+00:00"))
            t = t if t.tzinfo else t.replace(tzinfo=KST)
        except ValueError:
            continue
        if now - t > timedelta(minutes=limit) and o.get("status") in ("PENDING", "PARTIAL_FILLED"):
            try:
                toss.cancel_order(o["orderId"])
                event("자동 취소", f"{o.get('symbol')} {limit:g}분 넘게 미체결 → 취소", symbol=o.get("symbol"))
            except toss.TossError as e:
                event("오류", f"취소 실패 {o.get('symbol')}: {e}")


# ── 한 바퀴 ──
def run_once(force: bool = False, exits_only: bool = False) -> dict:
    """규칙 한 번. force=True(화면의 '한 번 돌려보기')면 꺼져 있어도 신호만 계산.
    exits_only=True 면 들고 있는 종목의 매도 조건만 빠르게 확인 (10초마다)."""
    c = config()
    mode, market = c["mode"], c["market"]
    with _lock:
        s = state()
    if mode == "off" and not force:
        return {"ok": True, "message": "꺼져 있어요"}
    if s.get("halted_today"):
        return {"ok": False, "message": "오늘은 정지됨: " + s["halted_today"]}
    if not toss.enabled():
        return {"ok": False, "message": "토스 키가 없어요 (🧰 도구)"}
    live, dry = mode == "live", mode == "off"
    cur = "KRW" if market == "KR" else "USD"
    results: list[str] = []
    try:
        fx = 1.0 if market == "KR" else usd_krw(c)
        if live:
            _reconcile(fx)
            _cancel_stale(1.5 if c["strategy"] == "surge" else 10)
            h = toss.holdings()
            raw = {it["symbol"]: {"qty": float(it.get("quantity") or 0), "avg": float(it.get("averagePurchasePrice") or 0)} for it in h.get("items") or []}
            with _lock:
                s = state()
            if c["auto_pick"]:   # 자동매매가 산 수량만 (사장님이 직접 산 건 그대로)
                pos = {k: {"qty": min(v["qty"], raw[k]["qty"]), "avg": v["avg"] or raw[k]["avg"]} for k, v in s["bot_pos"].items() if k in raw}
            else:
                pos = {k: v for k, v in raw.items() if k in c["symbols"]}
            pending = {o.get("symbol") for o in toss.open_orders()}
            cash_local = 0.0 if exits_only else float((toss.buying_power(cur) or {}).get("cashBuyingPower") or 0)   # 매도만 볼 땐 현금 조회 생략
        else:
            p = s["paper"]
            pos = {k: {"qty": v["qty"], "avg": v["avg"]} for k, v in p["positions"].items()}
            pending, cash_local = set(), p["cash"] / fx
        pos = {k: v for k, v in pos.items() if _mkt(k) == market and v["qty"] > 0}
        held_syms = list(pos)
        quotes = toss.prices(held_syms) if held_syms else {}
        invested = sum(v["qty"] * quotes.get(k, {}).get("price", v["avg"]) * fx for k, v in pos.items())
    except toss.TossError as e:
        event("오류", str(e))
        return {"ok": False, "message": str(e)}

    # 예산
    if c["budget_mode"] == "all_cash":
        budget = invested + cash_local * fx
        per_pos = budget / c["pick_count"]
        caps = {"total": budget, "position": per_pos, "order": per_pos}
        loss_limit = budget * c["max_daily_loss_pct"] / 100
    else:
        caps = {"total": c["max_total_krw"], "position": c["max_position_krw"], "order": c["max_order_krw"]}
        loss_limit = c["max_daily_loss_krw"]
    unreal = sum((quotes.get(k, {}).get("price", v["avg"]) - v["avg"]) * v["qty"] * fx for k, v in pos.items() if v["avg"])
    day_pl = float(s.get("realized_today", 0)) + unreal
    if live and not exits_only and day_pl <= -loss_limit and loss_limit > 0:
        return _halt_today(f"자동매매 오늘 손익 {day_pl:,.0f}원 — 한도 -{loss_limit:,.0f}원 초과")
    month_key = datetime.now(KST).strftime("%Y-%m")
    with _lock:
        sm = state()
        if sm["month"].get("key") != month_key or not sm["month"].get("start"):
            sm["month"] = {"key": month_key, "start": caps["total"] - unreal, "realized": 0.0}
            _save(sm)
        month = sm["month"]
    month_pl = float(month.get("realized", 0)) + unreal
    month_pct = month_pl / month["start"] * 100 if month["start"] else 0
    target_hit = month_pct >= c["monthly_target_pct"]
    results.append(f"💼 예산 {caps['total']:,.0f}원 (종목당 {caps['position']:,.0f}원) · 보유 {invested:,.0f}원 · 오늘 손익 {day_pl:+,.0f}원")
    results.append(f"🎯 이번 달 {month_pct:+.1f}% / 목표 {c['monthly_target_pct']:g}%" + (" — 달성! 이번 달은 새로 안 사요" if target_hit and c["stop_at_target"] else ""))

    # 종목
    minutes = minutes_now(market, c)
    first = -330 if _ext(market, c) else 0
    picked: list[str] = []
    if exits_only:
        pass
    elif not c["auto_pick"]:
        picked = list(c["symbols"])
    elif c["strategy"] == "surge":
        if not picker.base_ready(market):
            prepare_now()
            results.append("🔎 급등주 기준값 준비 중 (처음엔 몇 분~10여 분)")
        elif minutes is not None and first + 5 <= minutes <= 360 and len(pos) < c["pick_count"]:
            scan_cfg = {**c, "max_order_krw": caps["order"], "usd_krw": fx}
            r = picker.surge_scan(scan_cfg, log=_log_fn, exclude=set(pos))
            with _lock:
                s2 = state(); s2["picks"] = r; _save(s2)
            picked = [x["symbol"] for x in r["picks"]]
            results.append("🔎 " + r["note"])
        else:
            results.append("🔎 " + ("장 시간이 아니에요" if minutes is None else "종목 수가 꽉 찼거나 새로 살 시간이 아니에요"))
    else:
        pk = s.get("picks") or {}
        if pk.get("day") != picker.session_date(market):
            prepare_now(); results.append("🔎 오늘의 추세 종목 고르는 중")
        picked = [x["symbol"] for x in pk.get("picks", [])]

    symbols = list(dict.fromkeys(held_syms + picked))
    if not symbols:
        return {"ok": True, "mode": mode, "results": results + ["지금 살 종목이 없어요"]}
    try:
        quotes.update(toss.prices([x for x in symbols if x not in quotes]) if [x for x in symbols if x not in quotes] else {})
    except toss.TossError as e:
        return {"ok": False, "message": str(e), "results": results}

    for sym in symbols:
        q = quotes.get(sym)
        if not q:
            results.append(f"{sym}: 시세 없음"); continue
        price = q["price"]
        held = pos.get(sym, {"qty": 0.0, "avg": 0.0})
        with _lock:
            s3 = state()
            if held["qty"] > 0:
                s3["highs"][sym] = max(float(s3["highs"].get(sym, 0)), price); _save(s3)
            high = float(s3["highs"].get(sym, price))
        if c["strategy"] == "surge":
            side, reason = signal_surge(price, held["qty"], held["avg"], high, minutes, sym in picked, c)
            if side == "BUY":
                info = next((x for x in (state().get("picks") or {}).get("picks", []) if x["symbol"] == sym), None)
                reason = info["reason"] if info else reason
        else:
            try:
                closes, _, _ = toss.daily(sym, 120)
            except toss.TossError as e:
                results.append(f"{sym}: 차트 실패 {e}"); continue
            side, reason = signal_trend(closes, price, held["qty"], held["avg"], c)
            if side == "BUY" and sym not in picked:
                side, reason = "HOLD", "선택 종목 아님 (보유분은 매도 규칙만)"
        if exits_only and side == "BUY":
            side = "HOLD"
        if side == "HOLD":
            results.append(f"{sym}: {reason}"); continue
        if dry:
            results.append(f"{sym}: {('매수' if side == 'BUY' else '매도')} 신호 (꺼져 있어서 주문 안 함) — {reason}"); continue
        if not market_open(sym, side):
            results.append(f"{sym}: 신호는 있지만 매매 시간이 아님 — {reason}"); continue
        if sym in pending:
            results.append(f"{sym}: 미체결 주문이 있어서 대기"); continue
        f = 1 if _mkt(sym) == "KR" else fx
        if side == "BUY":
            if target_hit and c["stop_at_target"]:
                results.append(f"{sym}: 이번 달 목표 달성 → 새로 안 삼"); continue
            if state()["orders_today"] >= c["max_orders_per_day"]:
                results.append(f"{sym}: 오늘 매수 횟수 한도({c['max_orders_per_day']}) 도달"); continue
            if len(pos) >= c["pick_count"]:
                results.append(f"{sym}: 이미 {c['pick_count']}종목 보유 중"); continue
            buy_px = price * (1 + c["limit_slip_pct"] / 100) if c["strategy"] == "surge" else price
            budget = min(caps["order"], caps["position"] - held["qty"] * price * f, caps["total"] - invested, cash_local * fx)
            qty = int(budget // (buy_px * f))
            if qty < 1:
                results.append(f"{sym}: 살 돈이 부족해요 (가능 {budget:,.0f}원, 1주 {buy_px * f:,.0f}원)"); continue
            if c["ai_check"]:
                ok, why = _ai_ok(sym, side, reason, price)
                if not ok:
                    event("AI 반대", f"{sym} 매수 보류 — {why}", symbol=sym); results.append(f"{sym}: AI 반대 — {why}"); continue
            results.append(_execute(sym, "BUY", qty, buy_px, reason, live, fx))
            invested += qty * buy_px * f; cash_local -= qty * buy_px; pos[sym] = {"qty": qty, "avg": buy_px}
        else:
            qty = int(held["qty"])
            if qty < 1:
                results.append(f"{sym}: 팔 수량 없음"); continue
            sell_px = price * (1 - c["limit_slip_pct"] / 100) if c["strategy"] == "surge" else price
            results.append(_execute(sym, "SELL", qty, sell_px, reason, live, fx))
    return {"ok": True, "mode": mode, "results": results}


def _execute(sym: str, side: str, qty: int, price: float, reason: str, live: bool, fx: float) -> str:
    coid = "cai-" + uuid.uuid4().hex[:20]
    px = toss.order_price(sym, price, side)
    label = f"{sym} {'매수' if side == 'BUY' else '매도'} {qty}주 @ {px}"
    rec = {"t": datetime.now(KST).strftime("%m-%d %H:%M:%S"), "symbol": sym, "side": side, "qty": qty, "price": float(px),
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
    else:
        f = 1 if _mkt(sym) == "KR" else fx
        p_ = float(px)
        with _lock:
            s = state(); p = s["paper"]
            cur = p["positions"].get(sym, {"qty": 0, "avg": 0})
            if side == "BUY":
                p["cash"] -= qty * p_ * f
                tot = cur["qty"] + qty
                p["positions"][sym] = {"qty": tot, "avg": (cur["avg"] * cur["qty"] + p_ * qty) / tot}
                s["highs"][sym] = max(float(s["highs"].get(sym, 0)), p_)
            else:
                p["cash"] += qty * p_ * f
                gain = (p_ - cur["avg"]) * qty * f
                p["realized"] += gain; s["realized_today"] = float(s.get("realized_today", 0)) + gain
                s["month"]["realized"] = float(s["month"].get("realized", 0)) + gain
                p["positions"].pop(sym, None); s["highs"].pop(sym, None)
            _save(s)
        rec["status"] = "paper_filled"
    with _lock:
        s = state(); s["orders"].append(rec)
        if side == "BUY":
            s["orders_today"] += 1
        _save(s)
    event("실전 주문" if live else "가상 주문", f"{label} — {reason}", symbol=sym)
    return f"{sym}: {'✅ 주문 보냄' if live else '📝 가상 체결'} {label} — {reason}"


def _halt_today(why: str) -> dict:
    with _lock:
        s = state(); s["halted_today"] = why; _save(s)
    event("정지", why)
    return {"ok": False, "message": "오늘은 정지: " + why}


def emergency_stop() -> dict:
    """긴급 정지: 꺼짐 + 자동매매가 낸 미체결 주문 전부 취소 (보유 주식은 그대로 — 팔지 않음)."""
    c = config(); was = c["mode"]
    c["mode"] = "off"
    CONFIG.write_text(json.dumps(c, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    canceled, errors = 0, []
    if was == "live" and toss.enabled():
        try:
            mine = _bot_order_ids()
            for o in [o for o in toss.open_orders() if o.get("orderId") in mine]:
                try:
                    toss.cancel_order(o["orderId"]); canceled += 1
                except toss.TossError as e:
                    errors.append(str(e))
        except toss.TossError as e:
            errors.append(str(e))
    event("긴급 정지", f"꺼짐 (이전 {was}) · 미체결 취소 {canceled}건" + (f" · 실패: {errors[0]}" if errors else ""))
    return {"ok": not errors, "canceled": canceled,
            "message": f"자동매매를 껐어요. 미체결 {canceled}건 취소" + (" (일부 실패)" if errors else "") + ". 들고 있는 주식은 그대로예요 (토스 앱에서 직접 팔 수 있어요)"}


def reset_paper() -> dict:
    with _lock:
        s = state(); s["paper"] = {"cash": config()["paper_cash_krw"], "positions": {}, "realized": 0}; _save(s)
    event("가상 초기화", "가상 장부를 처음부터")
    return {"ok": True}


def overview() -> dict:
    c, s = config(), state()
    return {"config": c, "toss": toss.enabled(), "day": s["day"], "orders_today": s["orders_today"],
            "halted_today": s.get("halted_today", ""), "paper": s["paper"], "orders": s["orders"][-60:][::-1],
            "events": s["events"][-100:][::-1], "running": bool(_thread and _thread.is_alive()),
            "picks": s.get("picks") or {}, "scanning": _busy.is_set(), "bot_pos": s.get("bot_pos", {}),
            "realized_today": s.get("realized_today", 0), "base_ready": picker.base_ready(c["market"]), "month": s.get("month", {}),
            "session_minutes": minutes_now(c["market"], c),
            "local_time": picker.mnow(c["market"]).strftime("%H:%M"), "progress": dict(picker.PROGRESS), "next_open": _next_open(c)}


def _next_open(c: dict) -> str:
    """다음에 종목 찾기를 시작하는 한국 시각 (예: '오늘 18:05')"""
    market = c["market"]
    first = -330 if _ext(market, c) else 0
    now = datetime.now(KST)
    for step in range(0, 60 * 24 * 4, 5):   # 5분 단위로 최대 4일 앞까지
        t = now + timedelta(minutes=step)
        m = picker.session_minutes(market, t, extended=_ext(market, c))
        if m is not None and first + 5 <= m <= 360:
            day = "오늘" if t.date() == now.date() else ("내일" if t.date() == (now + timedelta(days=1)).date() else t.strftime("%m/%d"))
            return "지금" if step == 0 else f"{day} {t:%H:%M}"
    return ""


# ── 백그라운드 ──
def start(log_fn=print) -> None:
    global _thread, _log_fn
    _log_fn = log_fn
    if _thread and _thread.is_alive():
        return

    def loop():
        last = last_exit = 0.0
        while True:
            time.sleep(5)
            c = config()
            if c["mode"] == "off":
                continue
            t = picker.mnow(c["market"])
            # 장 전 준비: 미장은 뉴욕 07:00~ (프리장 켜면 00:30~), 국장은 07:30~ (평일)
            mins = minutes_now(c["market"], c)
            prep_at = ("00:30" if _ext(c["market"], c) else "07:00") if c["market"] == "US" else "07:30"
            if c["auto_pick"] and t.weekday() < 5 and t.strftime("%H:%M") >= prep_at:
                ready = picker.base_ready(c["market"]) if c["strategy"] == "surge" else \
                    (state().get("picks") or {}).get("day") == picker.session_date(c["market"])
                if not ready and not _busy.is_set():
                    prepare_now()
            if mins is None:
                continue
            # 들고 있는 종목은 자주(기본 10초) 매도 조건만 확인 — 급등주는 몇 분이면 크게 움직임
            gap = c["interval_min"] * 60
            if time.time() - last >= gap - 5 or time.time() - last_exit < c["exit_check_sec"]:
                pass
            else:
                with _lock:
                    s0 = state()
                holding = s0["bot_pos"] if c["mode"] == "live" else s0["paper"]["positions"]
                if holding:
                    last_exit = time.time()
                    try:
                        r = run_once(exits_only=True)
                        for line in r.get("results", []):
                            if "✅" in line or "📝" in line or "거절" in line or "정지" in line:
                                _log_fn("🤖 " + line)
                    except Exception as e:
                        event("오류", f"{type(e).__name__}: {e}")
            if time.time() - last < gap:
                continue
            last = time.time()
            try:
                r = run_once()
                for line in r.get("results", []):
                    if "✅" in line or "📝" in line or "거절" in line or "정지" in line:
                        _log_fn("🤖 " + line)
            except Exception as e:
                event("오류", f"{type(e).__name__}: {e}")

    _thread = threading.Thread(target=loop, daemon=True, name="autotrade")
    _thread.start()
