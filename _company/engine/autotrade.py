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

import contextlib
import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import backtest as bt
from . import news, picker, shadow, store, toss
from . import tradelearn as tl
from .agents import COMPANY_DIR, SHARED_DIR

CONFIG = SHARED_DIR / "autotrade.json"
STATE = COMPANY_DIR / "_cache" / "autotrade_state.json"
KST = timezone(timedelta(hours=9))
DEFAULTS = {
    "v": 4,                        # 설정 형식 버전 (예전 기본값 자동 갱신용)
    "mode": "off",
    "markets": ["US", "KR"],       # 동시에 돌릴 시장 (국장 09:00~15:30 · 미장 프리장 포함 저녁~새벽, 시장마다 기록·한도·정지 따로)
    "market": "US",                # (내부용) 지금 처리 중인 시장 — 설정 화면에서는 markets 를 씀
    "strategy": "auto",            # auto = 단타(급등)·스윙(추세) 후보를 '기대 수익률' 순으로 섞어서 / surge / trend
    "auto_pick": True,             # 종목을 규칙이 알아서 고름
    "pick_count": 3,               # 동시에 들고 갈 최대 종목 수 (예산을 이 수로 나눔)
    "symbols": [],                 # 직접 고른 종목 (auto_pick 끄면 이것만)
    "exclude": [],                 # 절대 사지 않을 종목
    "budget_mode": "all_cash",     # all_cash = 계좌의 그 통화 현금 전부 / fixed = max_total_krw
    "max_total_krw": 100000,       # fixed 일 때 총 투자 한도
    "max_order_krw": 50000,        # 한 번 주문 최대 (두 방식 모두 상한, 0 = 제한 없음)
    "max_position_krw": 50000,     # 종목당 최대 (두 방식 모두 상한, 0 = 제한 없음)
    "max_orders_per_day": 6,       # 하루 매수 횟수 (매도는 제한 없음)
    "loss_streak_pause": 3,        # 장중 학습: 연속 손실 이만큼이면 잠시 매수 쉬기
    "pause_min": 30,               # 그때 쉬는 시간(분)
    "monthly_target_pct": 10.0,    # 한 달 목표 수익률
    "stop_at_target": True,        # 목표 달성하면 그달은 새로 안 삼 (번 돈 지키기)
    "max_daily_loss_pct": 5.0,     # all_cash: 예산 대비 하루 손실 한도 %
    "max_daily_loss_krw": 5000,    # fixed: 하루 손실 한도 금액
    "stop_loss_pct": 3.0,
    "take_profit_pct": 10.0,       # 남은 물량 전부 익절
    "partial_tp_pct": 4.0,         # 나눠 팔기: 평단 대비 이만큼 오르면 일부 먼저 팔기 (0 = 안 씀)
    "partial_tp_ratio": 0.5,       # 그때 파는 비율 (0.5 = 절반) — 나머지는 손절선을 산 가격으로 올림
    "trail_pct": 4.0,              # 산 뒤 최고가 대비 이만큼 떨어지면 팔기 (최고 수익이 이만큼 이상 났을 때부터 작동)
    "trail_wide_after_pct": 10.0,  # 최고 수익이 이 % 이상이면 고점 대비 기준을 넓힘 (큰 상승은 더 길게)
    "trail_wide_pct": 6.0,         # 그때 쓰는 고점 대비 하락 기준(%)
    "surge_min_change_pct": 5.0,   # 오늘 이만큼 이상 오른 종목
    "surge_max_change_pct": 40.0,  # 너무 많이 오른 건 제외 (국장은 상한가 30%)
    "surge_volume_ratio": 3.0,     # 평소 거래량의 몇 배 이상
    "us_premarket": True,          # 미장 프리장(뉴욕 04:00~)부터 매매
    "pre_volume_ratio": 0.2,       # 프리장: 평소 하루 거래량의 이만큼(20%)이 벌써 거래된 종목
    "min_trading_value_krw": 3000000000,  # 하루 거래대금 최소 (원 환산)
    "us_min_price": 1.0,           # 1달러 미만 동전주 제외
    "limit_slip_pct": 0.5,         # 급등주: 사는 지정가는 현재가 +0.5%, 파는 건 -0.5% (체결 잘 되게)
    "rsi_max_buy": 68,             # 추세(스윙): 과열 기준
    "swing_stop_pct": 6.0,         # 스윙 손절 (며칠 들고 가니 단타보다 넓게)
    "swing_trail_pct": 8.0,        # 스윙: 최고 수익이 이만큼 난 뒤 고점 대비 이만큼 빠지면 팔기
    "swing_max_days": 15,          # 스윙 최대 보유 일수
    "interval_min": 1,             # 급등 종목 찾기 간격 (보유 종목 손절 확인은 따로 20초마다)
    "exit_check_sec": 10,
    "ai_check": False,             # 주문 전 AI 검토 (급등주는 속도가 중요해서 기본 꺼짐)
    "auto_tune": True,             # 🔁 매일 장 끝나고 자동 조절 (아래 항목별로 켜고 끔, 모두 범위 안에서만)
    "auto_rules": True,            #   학습 제안(거래량 기준·나눠 팔기 % 등) 자동 적용
    "auto_stop": True,             #   손절 % 자동 (stop_min ~ stop_max)
    "auto_limits": True,           #   종목당·주문 한도 자동 (처음 값의 limit_min_mult ~ limit_max_mult 배)
    "auto_loss": True,             #   하루 손실 한도 자동 (loss_pct_min ~ loss_pct_max %)
    "auto_mode": True,             #   실전↔가상 자동 (끄기·긴급 정지는 절대 자동으로 안 풀림)
    "stop_min": 2.0, "stop_max": 6.0,
    "limit_min_mult": 0.5, "limit_max_mult": 3.0,
    "loss_pct_min": 2.0, "loss_pct_max": 8.0,
    "mode_down_days": 3,           # 실전에서 이 날수 연속 손실이면 가상으로
    "mode_down_month_pct": 8.0,    # 또는 이번 달 이 % 이상 손실이면 가상으로
    "mode_up_trades": 10,          # 가상에서 이 건수 이상·승률 50%↑·평균 + 면 실전 복귀
    "shadow_on": True,             # 🧪 그림자 가상 매매: 규칙 조합 여러 개를 실전과 동시에 가상으로
    "auto_promote": True,          #   확실히 나은 조합을 실전 규칙으로 자동 교체 (매일 복기 뒤)
    "promote_min_trades": 15,      #   교체하려면 그 조합 거래가 최소 이만큼
    "promote_margin_pct": 1.0,     #   '현재'보다 총수익률이 이만큼(%p) 이상 높아야
    "promote_window_days": 14,     #   비교 기간(일)
    "backtest_on": True,           # 📼 매일 장 끝나고 백테스트 → 상위 조합을 그림자 매매로
    "ai_take_profit": "test",      # 🤖 익절을 AI 에게: off=규칙대로 · test=규칙대로 팔되 'AI 였으면?'을 따로 비교 · on=익절 규칙 대신 AI 가 판단
    "ai_tp_guard_peak": 3,         #    on 일 때 안전장치: 최고 수익이 +이 % 이상이었다가
    "ai_tp_guard_floor": 0.3,      #    +이 % 까지 밀리면 무조건 팔기 (손절·마감 정리·하루 손실 한도는 그대로 먼저)
    "ai_tp_min_trades": 10,        #    test 비교가 이 건수 이상 쌓이고
    "ai_tp_margin_pct": 0.3,       #    AI 쪽이 평균 이 %p 이상 나으면 자동으로 on
    "ai_decide": True,             # 🤖 거래마다 AI 판단: 사기 전 살지·얼마나, 보유 중 일찍 팔지 (손절·마감 정리·한도는 못 바꿈)
    "ai_max_per_hour": 30,         #   AI 판단 시간당 최대 횟수 (넘으면 규칙대로)
    "ai_hold_check_min": 5,        #   보유 종목을 AI 가 다시 보는 간격(분)
    "prob_sizing": True,           # 학습 기록으로 승률을 추정해 살 금액 조절 (25%~100%, 한도는 절대 안 넘음)
    "min_size_ratio": 0.25,        # 승률이 낮을 때 최소로 사는 비율
    "min_win_prob": 0,             # 추정 승률이 이보다 낮으면 안 삼 (0 = 안 씀)
    "news_on": True,               # 📰 사기 전에 공시 확인 (국장 DART · 미장 SEC) — 악재(증자·CB·감자·상장폐지 등)면 안 삼
    "news_unknown_ratio": 1.0,     #    공시가 하나도 없는 '이유 모르는 급등'이면 금액 × 이 값 (1 = 안 줄임 · 그림자 매매로 0.5·0.25 비교 후 자동 교체)
    "focus_max_pct": 50,           # 🎯 확신할 때만 몰아넣기: 조건 맞으면 한 종목에 예산의 최대 이 %까지 (0 = 안 씀, 늘 똑같이 나눔)
    "focus_min_samples": 20,       #    조건 ① 비슷한 지난 거래가 이 건수 이상
    "focus_min_ev_pct": 2,         #    조건 ② 기대 수익(수수료 뺀 값)이 +이 % 이상
    "ai_chart_check": False,       # 사기 직전 AI 가 1분봉 차트 그림을 보고 찬성/반대 (10~20초 늦어짐, 기본 꺼짐)
    "paper_cash_krw": 1000000,
    "usd_krw": 1400,
    "sim_slip_pct": 0.1,           # 가상 체결 미끄러짐(%) — 가상 매매·그림자·백테스트가 실전보다 좋게 나오지 않게
    "fee_us_pct": 0.1,             # 미장 수수료(%) 사고팔 때 각각
    "fee_kr_pct": 0.015,           # 국장 수수료(%) 사고팔 때 각각
    "tax_kr_pct": 0.18,            # 국장 증권거래세(%) 팔 때만 (세법 바뀌면 고치기)               # 환율 (토스에서 자동 갱신, 실패하면 이 값)
}
NUMS = {"promote_min_trades": (3, 200), "promote_margin_pct": (0, 50), "promote_window_days": (1, 90), "ai_max_per_hour": (0, 600),
        "ai_hold_check_min": (1, 120), "stop_min": (0.5, 20), "stop_max": (1, 30), "limit_min_mult": (0.1, 1), "limit_max_mult": (1, 20), "loss_pct_min": (0.5, 20),
        "loss_pct_max": (1, 50), "mode_down_days": (1, 20), "mode_down_month_pct": (1, 50), "mode_up_trades": (3, 100),
        "loss_streak_pause": (1, 20), "pause_min": (5, 600), "monthly_target_pct": (1, 100), "pick_count": (1, 10), "max_total_krw": (10000, 1e10), "max_order_krw": (0, 1e10), "max_position_krw": (0, 1e10),
        "max_orders_per_day": (1, 100), "max_daily_loss_pct": (0.5, 50), "max_daily_loss_krw": (1000, 1e10),
        "stop_loss_pct": (0.5, 50), "take_profit_pct": (0.5, 500), "trail_pct": (0.5, 50), "trail_wide_after_pct": (1, 200), "trail_wide_pct": (0.5, 50), "surge_min_change_pct": (1, 100),
        "surge_max_change_pct": (2, 500), "surge_volume_ratio": (1, 50), "pre_volume_ratio": (0.01, 5), "min_trading_value_krw": (0, 1e13), "us_min_price": (0.1, 100),
        "limit_slip_pct": (0, 3), "min_size_ratio": (0.05, 1), "min_win_prob": (0, 90), "partial_tp_pct": (0, 100), "partial_tp_ratio": (0.1, 0.9), "exit_check_sec": (10, 300), "rsi_max_buy": (30, 90), "swing_stop_pct": (1, 30), "swing_trail_pct": (1, 50), "swing_max_days": (1, 120), "interval_min": (1, 120), "paper_cash_krw": (10000, 1e10), "usd_krw": (500, 3000), "fee_us_pct": (0, 2), "fee_kr_pct": (0, 2), "tax_kr_pct": (0, 2), "sim_slip_pct": (0, 2), "focus_max_pct": (0, 100), "focus_min_samples": (5, 500), "focus_min_ev_pct": (0, 20), "news_unknown_ratio": (0.1, 1), "ai_tp_guard_peak": (1, 50), "ai_tp_guard_floor": (0, 10), "ai_tp_min_trades": (3, 200), "ai_tp_margin_pct": (0, 20)}
CHOICES = {"ai_take_profit": ("off", "test", "on"), "mode": ("off", "paper", "live"), "strategy": ("auto", "surge", "trend"), "budget_mode": ("all_cash", "fixed")}
_lock = threading.RLock()
_thread: threading.Thread | None = None
_log_fn = print
_busy_m = {"US": threading.Event(), "KR": threading.Event()}   # 시장별 종목 준비/고르기 백그라운드 작업 중
_ctx = threading.local()        # 지금 처리 중인 시장 (스레드마다)


def _raw_cfg() -> dict:
    return dict(store.load(CONFIG))   # 메모리 보관 (파일이 바뀌면 다시 읽음)


def markets(raw: dict | None = None) -> list[str]:
    raw = _raw_cfg() if raw is None else raw
    if int(raw.get("v", 1)) < 4:     # 예전 설정(시장 하나) → 이번 버전부터 국장·미장 둘 다
        return ["US", "KR"]
    ms = [m for m in (raw.get("markets") or DEFAULTS["markets"]) if m in ("US", "KR")]
    return list(dict.fromkeys(ms)) or ["US"]


def cur_market() -> str:
    m = getattr(_ctx, "m", None)
    return m if m in ("US", "KR") else markets()[0]


@contextlib.contextmanager
def in_market(m: str | None):
    """이 안에서는 config()["market"]·state() 가 그 시장 기준"""
    prev = getattr(_ctx, "m", None)
    _ctx.m = m if m in ("US", "KR") else prev
    try:
        yield
    finally:
        _ctx.m = prev


def _busy() -> threading.Event:
    return _busy_m[cur_market()]


# ── 설정·기록 ──
def config() -> dict:
    c = _raw_cfg()
    out = {**DEFAULTS, **{k: v for k, v in c.items() if k in DEFAULTS}}
    out["markets"] = markets(c)
    out["market"] = cur_market() if cur_market() in out["markets"] else out["markets"][0]
    if int(c.get("v", 1)) < 2:   # 예전 기본값(2분)을 그대로 쓰던 경우 → 새 기본값 1분
        if c.get("interval_min") == 2:
            out["interval_min"] = 1
    if int(c.get("v", 1)) < 3:   # 나눠 팔기 예전 기본값(+6%) → 새 기본값(+4%)
        if c.get("partial_tp_pct") in (6, 6.0):
            out["partial_tp_pct"] = 4.0
    if int(c.get("v", 1)) < 4:   # 이번 버전: 수익률 기준 자동 전략 · 국장+미장
        out["strategy"] = "auto"
    out["v"] = 4
    return out


def note_user_mode(mode: str) -> None:
    """사장님이 직접 모드를 바꾼 기록 (실전을 직접 켠 적이 있어야 자동 복귀 허용, 직접 바꾸면 자동 전환 표시 해제)"""
    with _lock:
        s = state()
        if mode == "live":
            s["user_live_ok"] = True
        s["auto_paper"] = False
        _save(s)


def save_config(new: dict, _by_auto: bool = False) -> dict:
    c, errors = config(), []
    if "mode" in new and not _by_auto and new.get("mode") in CHOICES["mode"]:
        note_user_mode(new["mode"])
    for k, opts in CHOICES.items():
        if k in new:
            if new[k] not in opts:
                errors.append(f"{k} 는 {'/'.join(opts)} 중 하나")
            elif k == "mode" and new[k] == "live" and not toss.enabled():
                errors.append("실전은 토스 키(🧰 도구)를 넣은 뒤에 켤 수 있어요")
            else:
                c[k] = new[k]
    if "markets" in new:
        ms = new["markets"] if isinstance(new["markets"], list) else str(new["markets"]).replace(",", " ").split()
        ms = [m for m in dict.fromkeys(str(x).upper() for x in ms) if m in ("US", "KR")]
        if not ms:
            errors.append("시장을 하나 이상 골라 주세요")
        else:
            c["markets"] = ms
    if "exclude" in new:
        raw = new["exclude"] if isinstance(new["exclude"], list) else str(new["exclude"]).replace(",", " ").split()
        ex = [toss.symbol(s) for s in raw if str(s).strip()]
        if any(not re.fullmatch(r"\d{6}|[A-Z][A-Z.\-]{0,9}", x) for x in ex):
            errors.append("제외 종목 코드가 이상해요")
        else:
            c["exclude"] = list(dict.fromkeys(ex))[:100]
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
    for k in ("ai_check", "auto_pick", "stop_at_target", "us_premarket", "ai_chart_check", "prob_sizing",
              "auto_tune", "auto_rules", "auto_stop", "auto_limits", "auto_loss", "auto_mode", "shadow_on", "auto_promote", "backtest_on", "ai_decide"):
        if k in new:
            c[k] = bool(new[k])
    if errors:
        return {"ok": False, "message": " · ".join(errors)}
    c.pop("market", None)
    store.save(CONFIG, c)
    event("설정", f"모드 {c['mode']} · {'+'.join({'US': '미장', 'KR': '국장'}[m] for m in c['markets'])} · {STRAT_NAME[c['strategy']]} · "
                f"{'현금 전부' if c['budget_mode'] == 'all_cash' else '한도 ' + format(c['max_total_krw'], ',') + '원'}"
                + ("" if c["auto_pick"] else f" · 종목 {', '.join(c['symbols']) or '없음'}"))
    return {"ok": True, "config": c}


STRAT_NAME = {"auto": "수익률 기준(단타+스윙)", "surge": "급등주", "trend": "추세"}


def _state_path(m: str | None = None):
    """시장마다 기록 파일 따로 (예전 하나짜리 파일은 미장 것으로 옮김)"""
    m = m or cur_market()
    path = STATE.with_name(f"autotrade_state_{m}.json")
    if m == "US" and not path.exists() and STATE.exists():
        try:
            STATE.rename(path)
        except OSError:
            pass
    return path


def _fresh_day(s: dict, market: str) -> dict:
    day = picker.session_date(market)
    if s.get("day") != day:  # 장(현지 날짜)이 바뀌면 하루 카운트·정지·손익 초기화 (스윙 종목의 최고가는 유지)
        keep = {k: v for k, v in (s.get("highs") or {}).items() if (s.get("style") or {}).get(k) == "swing"}
        s.update(day=day, orders_today=0, halted_today="", realized_today=0.0, highs=keep, day_budget=0.0)
    return s


def state() -> dict:
    with _lock:
        s = store.load(_state_path())   # 메모리 보관 · 깨지면 백업에서 복구
        for k, v in (("events", []), ("orders", []), ("bot_pos", {}), ("picks", {}), ("swing", {}), ("style", {}), ("buy_day", {}), ("highs", {}), ("realized_today", 0.0), ("month", {}), ("partial", {}), ("trail_mode", {}), ("hold_log", {})):
            s.setdefault(k, v)
        if "paper" not in s:
            s["paper"] = {"cash": config()["paper_cash_krw"], "positions": {}, "realized": 0}
        for k, v in list(s["bot_pos"].items()):  # 예전 형식(수량만) 호환
            if not isinstance(v, dict):
                s["bot_pos"][k] = {"qty": float(v), "avg": 0.0}
        return _fresh_day(s, cur_market())


def _save(s: dict) -> None:
    s["events"] = s["events"][-400:]
    s["orders"] = s["orders"][-400:]
    store.save(_state_path(), s)   # 임시 파일에 쓰고 바꿔치기 + 백업


def event(kind: str, text: str, **extra) -> None:
    with _lock:
        s = state()
        s["events"].append({"t": datetime.now(KST).strftime("%m-%d %H:%M:%S"), "kind": kind, "text": text, **extra})
        _save(s)
    flag = '🇺🇸' if cur_market() == 'US' else '🇰🇷'
    _log_fn(f"🤖 {flag} [{kind}] {text}")
    if kind in NOTIFY_KINDS and not (kind == "자동 조정" and not text.startswith(("오늘 자동으로", "모드"))) \
            and not (kind == "실데이터" and text.startswith("점검 통과")):
        from . import notify
        notify.send(f"{flag} [{kind}] {text}")


NOTIFY_KINDS = {"익절 비교", "공시 악재", "실전 주문", "체결", "주문 거절", "정지", "긴급 정지", "정지 해제", "규칙 교체", "자동 조정", "복기", "실데이터",
                "휴장", "오류", "복구", "AI 판단", "백테스트"}   # 🔔 텔레그램으로 보낼 기록 종류


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


def signal_swing(closes: list[float], price: float, qty: float, avg: float, high: float, days: int, c: dict) -> tuple[str, str]:
    """스윙(며칠 보유) 매도: 손절 · 고점 대비 하락(최고 수익이 기준 이상 난 뒤) · 20일선 이탈 · 최대 보유 일수. 밤새 들고 감"""
    pnl = (price / avg - 1) * 100
    high = max(high or price, price)
    peak, drop = (high / avg - 1) * 100, (price / high - 1) * 100
    ma20 = _ma(closes + [price], 20)
    if pnl <= -c["swing_stop_pct"]:
        return "SELL", f"[스윙] 손절: 평단 대비 {pnl:.1f}% (기준 -{c['swing_stop_pct']:g}%)"
    if peak >= c["swing_trail_pct"] and drop <= -c["swing_trail_pct"]:
        return "SELL", f"[스윙] 고점 대비 {drop:.1f}% 하락 (최고 +{peak:.1f}%) → 수익 지키기 (평단 대비 {pnl:+.1f}%)"
    if ma20 and price < ma20 * 0.98:
        return "SELL", f"[스윙] 추세 이탈: 20일선({ma20:,.2f}) 아래 (평단 대비 {pnl:+.1f}%)"
    if days >= c["swing_max_days"]:
        return "SELL", f"[스윙] {days}일 보유 → 정리 (평단 대비 {pnl:+.1f}%)"
    return "HOLD", f"[스윙] 보유 유지 {days}일째 (평단 대비 {pnl:+.1f}% · 최고 +{peak:.1f}%)"


_news_seen: dict[str, float] = {}


def _seen_news_block(sym: str) -> bool:
    """같은 종목 악재 알림은 1시간에 한 번만"""
    if time.time() - _news_seen.get(sym, 0) < 3600:
        return True
    _news_seen[sym] = time.time()
    return False


def _no_tp(c: dict) -> dict:
    """익절 규칙(나눠 팔기·전부 익절·고점 대비 하락)만 끈 설정 — 손절·마감 정리·본전 정리·20일선·최대 일수는 그대로"""
    return {**c, "partial_tp_pct": 0, "take_profit_pct": 0, "trail_pct": 1e9, "trail_wide_pct": 1e9, "trail_wide_after_pct": 0,
            "swing_trail_pct": 1e9}


def tp_guard(avg: float, high: float, price: float, c: dict) -> str:
    """AI 익절 안전장치: 최고 +3% 이상 → +0.3% 까지 밀리면 무조건 팔기"""
    if not avg:
        return ""
    peak, pnl = (high / avg - 1) * 100, (price / avg - 1) * 100
    if peak >= c["ai_tp_guard_peak"] and pnl <= c["ai_tp_guard_floor"]:
        return f"🛡️ AI 익절 안전장치: 최고 +{peak:.1f}% → 지금 {pnl:+.1f}% (기준 +{c['ai_tp_guard_floor']:g}%) → 팔기"
    return ""


def _ai_room(c: dict) -> bool:
    """이번 시간 AI 판단 여유가 있는지 (횟수는 안 씀)"""
    with _lock:
        n = len([t for t in state().get("ai_calls", []) if time.time() - t < 3600])
    return n < c["ai_max_per_hour"]


def _ghost_open(sym: str, held: dict, high: float, price: float, style: str, bought: str | None) -> None:
    """test 모드: 규칙이 익절했을 때 'AI 였으면?'을 이어서 따라가는 유령 보유 (돈 안 씀)"""
    with _lock:
        s = state()
        g = s.setdefault("tp_ghost", {})
        if sym not in g:
            g[sym] = {"avg": held["avg"], "high": max(high, price), "rule_px": price, "style": style, "buy_day": bought,
                      "t": datetime.now(KST).strftime("%m-%d %H:%M"), "ai_at": 0}
            _save(s)


def _ghost_step(c: dict, market: str, minutes) -> list[str]:
    """유령 보유 따라가기: AI 익절 모드였으면 언제 팔았을지 → 규칙 익절과 차이 기록 → 충분히 나으면 자동으로 on"""
    out = []
    with _lock:
        ghosts = dict(state().get("tp_ghost", {}))
    if not ghosts:
        return out
    try:
        qs = toss.prices(list(ghosts))
    except toss.TossError:
        return out
    nt = _no_tp(c)
    for sym, g in ghosts.items():
        q = qs.get(sym)
        if not q:
            continue
        price = q["price"]
        g["high"] = max(g["high"], price)
        why = tp_guard(g["avg"], g["high"], price, c)
        if not why:
            if g["style"] == "swing":
                try:
                    closes, _, _ = toss.daily(sym, 60)
                except toss.TossError:
                    continue
                days = (datetime.now(KST).date() - datetime.strptime(g["buy_day"], "%Y-%m-%d").date()).days if g.get("buy_day") else 0
                side, r = signal_swing(closes, price, 1, g["avg"], g["high"], days, nt)
            elif minutes is None:
                side, r = "SELL", "장 끝 (단타)"
            else:
                side, r = signal_surge(price, 1, g["avg"], g["high"], minutes, False, nt)
            if side == "SELL":
                why = "규칙(손절·마감 등): " + r
        if not why:
            dec = _ai_peek(("ghost", market, sym))
            if dec and dec != "PENDING" and dec[0]:
                why = "🤖 AI: " + dec[1]
                _ai_jobs.pop(("ghost", market, sym), None)
            elif dec is None and time.time() - g.get("ai_at", 0) >= c["ai_hold_check_min"] * 60 and _ai_room(c):
                g["ai_at"] = time.time()
                _ai_async(("ghost", market, sym), _ai_decide_hold, sym, {"avg": g["avg"], "qty": 1}, price, g["high"], g["style"],
                          "익절 비교용 (규칙은 이미 팔았음)", c)
        with _lock:
            s = state()
            gs = s.setdefault("tp_ghost", {})
            if why:
                gs.pop(sym, None)
                diff = (price - g["rule_px"]) / g["avg"] * 100
                st_ = s.setdefault("ai_tp_stats", {"n": 0, "sum": 0.0, "log": []})
                st_["n"] += 1; st_["sum"] += diff
                st_["log"] = (st_["log"] + [{"t": g["t"], "sym": sym, "diff": round(diff, 2), "why": why[:60]}])[-30:]
                out.append(f"🤖 익절 비교 {sym}: AI 였으면 {diff:+.2f}%p ({why[:50]})")
                event("익절 비교", f"{sym} 규칙 익절 {g['rule_px']:g} vs AI {price:g} → AI {diff:+.2f}%p · 누적 {st_['n']}건 평균 {st_['sum'] / st_['n']:+.2f}%p", symbol=sym)
            elif sym in gs:
                gs[sym] = g
            _save(s)
    # 자동 전환: 두 시장 합쳐서 충분히 쌓이고 AI 가 확실히 나으면 on
    n = tot = 0
    for m in ("US", "KR"):
        with in_market(m):
            st_ = state().get("ai_tp_stats") or {}
        n += st_.get("n", 0); tot += st_.get("sum", 0.0)
    if c["ai_take_profit"] == "test" and n >= c["ai_tp_min_trades"] and tot / n >= c["ai_tp_margin_pct"]:
        if save_config({"ai_take_profit": "on"}, _by_auto=True).get("ok"):
            event("규칙 교체", f"🤖 익절을 AI 판단으로 바꿨어요 — 비교 {n}건 평균 AI 쪽이 {tot / n:+.2f}%p 나음 (안전장치 +{c['ai_tp_guard_peak']:g}%→+{c['ai_tp_guard_floor']:g}%)")
    return out


def focus_ok(est: dict | None, c: dict) -> bool:
    """🎯 확신할 때만 몰아넣기 조건: 비슷한 지난 거래가 충분히 많고(N건) 기대 수익이 높을 때(+X%)만"""
    return bool(est and c.get("focus_max_pct") and est.get("n", 0) >= c["focus_min_samples"]
                and est.get("ev", 0) >= c["focus_min_ev_pct"])


def trail_for(avg: float, high: float, c: dict) -> float:
    """이 종목에 지금 적용할 고점 대비 하락 기준(%) — 최고 수익이 trail_wide_after_pct 이상이면 넓은 기준"""
    peak = (high / avg - 1) * 100 if avg and high else 0
    return c["trail_wide_pct"] if c.get("trail_wide_after_pct") and peak >= c["trail_wide_after_pct"] else c["trail_pct"]


def signal_surge(price: float, qty: float, avg: float, high: float, minutes: int | None, picked: bool, c: dict,
                 partial_done: bool = False) -> tuple[str, str]:
    """('BUY'|'SELL'|'SELL_PART'|'HOLD', 이유)
    매도 순서: 마감 정리 → 손절 → (나눠 판 뒤) 본전 정리 → 나눠 팔기 → 고점 대비 하락 → (나눠 팔기 안 쓸 때만) 전부 익절"""
    if qty > 0 and avg > 0:
        pnl = (price / avg - 1) * 100
        high = max(high or price, price)
        peak = (high / avg - 1) * 100
        drop = (price / high - 1) * 100
        trail = trail_for(avg, high, c)
        if minutes is not None and minutes >= 375:
            return "SELL", f"장 마감 15분 전 정리 (평단 대비 {pnl:+.1f}%)"
        if pnl <= -c["stop_loss_pct"]:
            return "SELL", f"손절: 평단 대비 {pnl:.1f}% (기준 -{c['stop_loss_pct']}%)"
        if partial_done and pnl <= c["limit_slip_pct"] + 0.2:   # 일부 익절한 뒤엔 남은 물량을 손해 보고 팔지 않게 (파는 지정가 -0.5% 감안해 본전에서 정리)
            return "SELL", f"나눠 팔기 후 본전 도달 → 남은 물량 정리 (평단 대비 {pnl:+.1f}%)"
        if not partial_done and c.get("partial_tp_pct") and pnl >= c["partial_tp_pct"]:
            return "SELL_PART", f"나눠 팔기: 평단 대비 +{pnl:.1f}% → {int(c['partial_tp_ratio'] * 100)}% 먼저 익절"
        # 고점 대비 하락: 최고 수익이 기준(trail) 이상 났으면 '무장' → 그 뒤엔 평단 아래로 살짝 내려가도 팔기
        # (예전엔 '지금 수익 중'일 때만 팔아서, +4.4% → 평단 아래로 빠르게 밀리면 손절(-3%)까지 기다리던 문제 수정)
        if peak >= trail and drop <= -trail:
            return "SELL", f"고점 대비 {drop:.1f}% 하락 (기준 -{trail:g}%, 최고 +{peak:.1f}%) → 수익 지키기 (평단 대비 {pnl:+.1f}%)"
        if not c.get("partial_tp_pct") and c.get("take_profit_pct") and pnl >= c["take_profit_pct"]:
            return "SELL", f"익절: 평단 대비 +{pnl:.1f}%"
        armed = "작동 중" if peak >= trail else f"최고 +{peak:.1f}% < {trail:g}% 라 아직 대기"
        return "HOLD", f"보유 유지 (평단 대비 {pnl:+.1f}% · 최고 +{peak:.1f}% · 고점 대비 {drop:.1f}% / 기준 -{trail:g}% {armed})"
    if not picked:
        return "HOLD", "선택 종목 아님"
    if minutes is None or minutes > 360:
        return "HOLD", "마감 30분 전부터는 새로 안 삼"
    return "BUY", "급등 + 거래량 폭증"


def _note_hold(sym: str, avg: float, high: float, price: float, side: str, reason: str, c: dict) -> None:
    """보유 종목 기록: ① 고점 대비 기준이 바뀌면 (종목·이전→이후·이유) 기록 ② 5분마다 '지금 판단' 기록 (나중에 원인 찾기용)"""
    with _lock:
        s = state()
        new = trail_for(avg, high, c)
        old = s["trail_mode"].get(sym)
        changed = old is not None and abs(float(old) - new) > 1e-9
        if old is None or changed:
            s["trail_mode"][sym] = new
        now = time.time()
        log_due = side == "HOLD" and now - float(s["hold_log"].get(sym, 0)) >= 300
        if log_due:
            s["hold_log"][sym] = now
        _save(s)
    if changed:
        peak = (high / avg - 1) * 100
        event("기준 변경", f"{sym} 고점 대비 매도 기준 -{old:g}% → -{new:g}% (이유: 최고 수익 +{peak:.1f}% "
              f"{'≥' if new == c['trail_wide_pct'] else '<'} {c['trail_wide_after_pct']:g}%)", symbol=sym)
    if log_due:
        event("보유 확인", f"{sym} {reason}", symbol=sym)


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
    m = cur_market()
    if _busy_m[m].is_set():
        return {"ok": True, "message": "이미 준비 중이에요"}

    def job():
        with in_market(m):
            _busy_m[m].set()
            try:
                fn()
            except Exception as e:
                event("오류", f"{name} 실패: {type(e).__name__}: {e}")
            finally:
                _busy_m[m].clear()
    threading.Thread(target=job, daemon=True, name=name).start()
    return {"ok": True, "message": f"{name} 시작 (몇 분 걸려요)"}


def _cap(v: float, *limits: float) -> float:
    """상한 적용 (0 이하 한도는 '제한 없음')"""
    for lim in limits:
        if lim and lim > 0:
            v = min(v, lim)
    return v


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
            c["max_order_krw"] = _cap((cash + held) * fx / c["pick_count"], c["max_order_krw"], c["max_position_krw"])
            c["usd_krw"] = fx
        except toss.TossError:
            lim = _cap(1e12, c["max_order_krw"], c["max_position_krw"])
            c["max_order_krw"] = 0 if lim >= 1e12 else lim   # 현금을 모르면 한도만으로 (한도도 없으면 가격 상한 없이)
    return c


def prepare_now() -> dict:
    """장 전 준비: 급등주 기준값(단타) + 스윙(추세) 후보 — 전략에 맞게"""
    c = config()
    pc = _prep_cfg(c)
    mk = "미장" if c["market"] == "US" else "국장"

    def go():
        if c["strategy"] in ("surge", "auto"):
            b = picker.prepare_day(pc, log=_log_fn)
            event("준비", f"{mk} 급등주 기준값 {len(b.get('rows') or {})}개 준비 완료")
        if c["strategy"] in ("trend", "auto"):
            r = picker.scan({**c, "max_order_krw": pc["max_order_krw"], "usd_krw": pc.get("usd_krw", c["usd_krw"])}, log=_log_fn,
                            exclude=set(c["exclude"]))
            with _lock:
                s = state(); s["swing"] = r
                s.setdefault("swing_hist", []).append({"day": r["day"], "picks": [{"symbol": x["symbol"], "price": x["price"]} for x in r["picks"]]})
                s["swing_hist"] = [h for i, h in enumerate(s["swing_hist"]) if h["day"] not in [z["day"] for z in s["swing_hist"][i + 1:]]][-30:]
                _save(s)
            event("종목 선택", f"{mk} 스윙 " + r["note"] + (" — " + ", ".join(f"{p['name']}({p['symbol']})" for p in r["picks"][:6]) if r["picks"] else ""))
    return _bg("장 전 준비", go)


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
                    s["style"][rec["symbol"]] = (rec.get("features") or {}).get("style", "surge")
                    s["buy_day"].setdefault(rec["symbol"], datetime.now(KST).strftime("%Y-%m-%d"))
                    tl.open_trade(rec["symbol"], delta, fill_px, rec.get("features"), "live", s["day"])
                else:
                    mk_ = _mkt(rec["symbol"]); c_ = config()   # 수수료·세금 포함 실제 손익
                    gain = (fill_px * (1 - tl.side_cost(c_, mk_, "SELL") / 100) - bp["avg"] * (1 + tl.side_cost(c_, mk_, "BUY") / 100)) * delta * f
                    s["realized_today"] = float(s.get("realized_today", 0)) + gain
                    s["month"]["realized"] = float(s["month"].get("realized", 0)) + gain
                    left = bp["qty"] - delta
                    tl.on_sell(rec["symbol"], delta, fill_px, rec.get("reason", ""), "live", left <= 1e-9, config())
                    if left > 1e-9:
                        s["bot_pos"][rec["symbol"]] = {"qty": left, "avg": bp["avg"]}
                        if rec.get("partial"):
                            s["partial"][rec["symbol"]] = True
                    else:
                        s["bot_pos"].pop(rec["symbol"], None); s["highs"].pop(rec["symbol"], None); s["partial"].pop(rec["symbol"], None)
                        s["trail_mode"].pop(rec["symbol"], None); s["hold_log"].pop(rec["symbol"], None)
                        s["style"].pop(rec["symbol"], None); s["buy_day"].pop(rec["symbol"], None)
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
    halted = s.get("halted_today", "")   # 정지돼도 '새로 사기'만 막고, 들고 있는 종목의 손절·정리는 계속
    if not toss.enabled():
        return {"ok": False, "message": "토스 키가 없어요 (🧰 도구)"}
    live, dry = mode == "live", mode == "off"
    cur = "KRW" if market == "KR" else "USD"
    results: list[str] = []
    try:
        fx = 1.0 if market == "KR" else usd_krw(c)
        if live:
            _reconcile(fx)
            _cancel_stale(1.5)
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
        per_pos = _cap(budget / c["pick_count"], c["max_position_krw"])   # 종목당 = 현금÷종목 수, 단 '종목당 최대'를 넘지 않게
        caps = {"total": budget, "position": per_pos, "order": _cap(per_pos, c["max_order_krw"])}
        with _lock:   # 하루 손실 한도는 '그날 처음 예산' 기준으로 고정 (돈이 줄어도 한도가 같이 줄지 않게)
            s_b = state()
            if not s_b.get("day_budget") and budget > 0 and not exits_only:
                s_b["day_budget"] = budget; _save(s_b)
            day_budget = float(s_b.get("day_budget") or budget)
        loss_limit = day_budget * c["max_daily_loss_pct"] / 100
    else:
        caps = {"total": c["max_total_krw"], "position": _cap(c["max_total_krw"], c["max_position_krw"]),
                "order": _cap(c["max_total_krw"], c["max_order_krw"], c["max_position_krw"])}
        loss_limit = c["max_daily_loss_krw"]
    unreal = sum((quotes.get(k, {}).get("price", v["avg"]) - v["avg"]) * v["qty"] * fx for k, v in pos.items() if v["avg"])
    day_pl = float(s.get("realized_today", 0)) + unreal
    if live and not exits_only and not halted and day_pl <= -loss_limit and loss_limit > 0:
        halted = f"자동매매 오늘 손익 {day_pl:,.0f}원 — 한도 -{loss_limit:,.0f}원 초과"
        _halt_today(halted)   # 새로 사기만 멈추고, 이번 바퀴에도 들고 있는 종목 손절·정리는 계속
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

    # 종목 후보: 단타(급등) + 스윙(추세)을 기대 수익률 순으로
    minutes = minutes_now(market, c)
    first = -330 if _ext(market, c) else 0
    strat = c["strategy"]
    cands: dict[str, dict] = {}   # 종목 → {style, features, reason, score}
    if exits_only:
        pass
    elif not c["auto_pick"]:
        for x in c["symbols"]:
            if x not in c["exclude"] and _mkt(x) == market:
                cands[x] = {"style": "swing" if strat == "trend" else "surge", "features": {}, "reason": "직접 고른 종목", "score": 0}
    elif halted:
        results.append("⛔ 오늘 정지 중 — 새로 사지 않고 들고 있는 종목만 관리 (" + halted + ")")
    else:
        slots = len(pos) < c["pick_count"]
        if strat in ("surge", "auto"):
            if not picker.base_ready(market):
                prepare_now()
                results.append("🔎 급등주 기준값 준비 중 (처음엔 몇 분~10여 분)")
            elif minutes is not None and first + 5 <= minutes <= 360 and slots:
                scan_cfg = {**c, "max_order_krw": caps["order"], "usd_krw": fx}
                r = picker.surge_scan(scan_cfg, log=_log_fn, exclude=set(pos) | set(c["exclude"]))
                with _lock:
                    s2 = state(); s2["picks"] = r; _save(s2)
                for x in r["picks"]:
                    cands[x["symbol"]] = {"style": "surge", "features": {k: x.get(k) for k in ("change", "ratio", "pre", "minute", "score")},
                                          "reason": x["reason"], "score": x.get("score", 0), "info": x}
                results.append("🔎 단타 " + r["note"])
            elif minutes is None:
                results.append("🔎 장 시간이 아니에요")
        if strat in ("trend", "auto"):
            sw = s.get("swing") or {}
            if sw.get("day") != picker.session_date(market):
                if strat == "trend" or picker.base_ready(market):
                    prepare_now(); results.append("🔎 오늘의 스윙(추세) 종목 고르는 중")
            elif minutes is not None and minutes >= 10 and slots:   # 스윙은 정규장에서만 삼
                for x in sw.get("picks", []):
                    if x["symbol"] not in c["exclude"] and x["symbol"] not in pos and x["symbol"] not in cands:
                        cands[x["symbol"]] = {"style": "swing", "features": {k: x.get(k) for k in ("r20", "r60", "rsi", "score")},
                                              "reason": "[스윙] " + x["reason"], "score": x.get("score", 0), "info": x}
                if sw.get("picks"):
                    results.append(f"🔎 스윙 후보 {len(sw['picks'])}개 ({sw.get('note', '')})")
    # 🎲 후보마다 기대 수익률 → 높은 순 (단타·스윙 상관없이)
    for sym, cd in cands.items():
        cd["features"].update(style=cd["style"], mkt=market)
        cd["est"] = tl.estimate(cd["features"], c)
        cd["reason"] += f" · 추정 승률 {cd['est']['p'] * 100:.0f}%({cd['est']['n']}건) · 기대 {cd['est']['ev']:+.2f}%"
    picked = sorted(cands, key=lambda k: (cands[k]["est"]["ev"], cands[k]["score"]), reverse=True)
    if c["news_on"] and not exits_only and news.enabled(market):   # 📰 상위 후보 공시 확인 (20분 보관) → 실전·그림자·AI 판단 공통
        for k in [k for k in picked if k not in pos][:8]:
            cands[k]["news"] = news.peek(k, market)   # 기다리지 않음 (처음엔 '확인 중', 다음 바퀴에 결과)
            if cands[k]["news"].get("ok"):
                cands[k]["features"]["news"] = "악재" if cands[k]["news"]["bad"] else "있음" if cands[k]["news"]["items"] else "없음"
    if cands:
        with _lock:
            s2 = state(); s2["cands"] = {k: {"style": v["style"], "reason": v["reason"], "ev": v["est"]["ev"], "p": v["est"]["p"]} for k, v in cands.items()}; _save(s2)

    symbols = list(dict.fromkeys(held_syms + picked))
    if not symbols:
        if c["shadow_on"] and not dry:   # 실전에 살 게 없어도 그림자 조합들의 매도는 계속
            try:
                shadow.step(c, market, cands, quotes, minutes, fx, exits_only, signal_surge, signal_swing, market_open,
                            lambda x: toss.daily(x, 60)[0], s["day"])
            except Exception as e:
                results.append(f"🧪 그림자 매매 오류: {type(e).__name__}: {e}")
        return {"ok": True, "mode": mode, "results": results + ["지금 살 종목이 없어요"]}
    try:
        quotes.update(toss.prices([x for x in symbols if x not in quotes]) if [x for x in symbols if x not in quotes] else {})
    except toss.TossError as e:
        return {"ok": False, "message": str(e), "results": results}

    # 📚 장중 학습: 보유 종목 최대 상승·하락 갱신, 고른 종목(산 것·못 산 것)의 이후 움직임 기록
    mode_key = "live" if live else "paper"
    wday = f"{market}:{s['day']}"
    try:
        tl.mark({k: quotes[k]["price"] for k in pos if k in quotes}, mode_key)
        if not exits_only:
            wsyms = [x for x in tl.watch_symbols(wday) if x not in quotes]
            if wsyms:
                quotes.update(toss.prices(wsyms))
            tl.watch(wday, [cd["info"] for cd in cands.values() if cd.get("info") and cd["style"] == "surge"],
                     {k: v["price"] for k, v in quotes.items()})
    except (toss.TossError, OSError) as e:
        results.append(f"📚 학습 기록 일부 실패: {e}")
    stop_why = tl.guard(wday, bool(minutes is not None and minutes < 0))

    for sym in symbols:
        tp_by_rule = None   # test 모드: 익절 규칙이 실제로 팔면 'AI 였으면?' 비교 시작
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
            style = s3["style"].get(sym) if held["qty"] > 0 else (cands.get(sym) or {}).get("style")
            style = style or ("swing" if strat == "trend" else "surge")
            bought = s3["buy_day"].get(sym)
        if held["qty"] > 0 and style == "swing":
            try:
                closes, _, _ = toss.daily(sym, 60)
            except toss.TossError as e:
                results.append(f"{sym}: 차트 실패 {e}"); continue
            days = (datetime.now(KST).date() - datetime.strptime(bought, "%Y-%m-%d").date()).days if bought else 0
            side, reason = signal_swing(closes, price, held["qty"], held["avg"], high, days, c)
        elif held["qty"] > 0:
            side, reason = signal_surge(price, held["qty"], held["avg"], high, minutes, False, c,
                                        partial_done=bool(state()["partial"].get(sym)))
            _note_hold(sym, held["avg"], max(high, price), price, side, reason, c)
        elif sym in cands:
            side, reason = ("BUY", cands[sym]["reason"]) if (style == "swing" or (minutes is not None and minutes <= 360)) \
                else ("HOLD", "마감 30분 전부터는 새로 안 삼")
        else:
            side, reason = "HOLD", "선택 종목 아님"
        if held["qty"] > 0 and c["ai_take_profit"] != "off" and c["ai_decide"] and not dry:   # 🤖 AI 익절
            if side in ("SELL", "SELL_PART"):
                nt = _no_tp(c)
                if style == "swing":
                    side_r = signal_swing(closes, price, held["qty"], held["avg"], high, days, nt)[0]
                else:
                    side_r = signal_surge(price, held["qty"], held["avg"], high, minutes, False, nt,
                                          partial_done=bool(state()["partial"].get(sym)))[0]
                if side_r == "HOLD":   # 이 매도는 익절 규칙이 낸 것
                    if c["ai_take_profit"] == "on" and _ai_room(c):
                        side, reason = "HOLD", f"🤖 익절은 AI 가 판단 (규칙이었으면: {reason[:60]})"
                    elif c["ai_take_profit"] == "test":
                        tp_by_rule = (dict(held), high, style, bought)
            if c["ai_take_profit"] == "on" and side == "HOLD":
                g_ = tp_guard(held["avg"], max(high, price), price, c)
                if g_:
                    side, reason = "SELL", g_
        if exits_only and side == "BUY":
            side = "HOLD"
        if side == "HOLD" and held["qty"] > 0 and c["ai_decide"] and not dry:   # 🤖 보유 중 AI 가 일찍 팔지 (따로 생각, 결과는 10초 확인 때도 반영)
            dec = _ai_peek(("hold", market, sym))
            if dec and dec != "PENDING" and dec[0]:
                side, reason = "SELL", f"🤖 AI 판단: {dec[1]}"
                event("AI 판단", f"{sym} 일찍 팔기 — {dec[1]}", symbol=sym, chart=dec[2])
                _ai_jobs.pop(("hold", market, sym), None)
            elif not exits_only and dec is None and _ai_hold_due(sym, c):
                _ai_async(("hold", market, sym), _ai_decide_hold, sym, dict(held), price, high, style, reason, c)
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
            if halted:
                results.append(f"{sym}: ⛔ 오늘 정지 중이라 안 삼"); continue
            if minutes is not None and minutes < 0 and state().get("no_premarket_until", "") >= datetime.now(KST).strftime("%Y-%m-%d"):
                results.append(f"{sym}: 토스가 프리장 주문을 안 받아서 정규장부터 삼"); continue
            if target_hit and c["stop_at_target"]:
                results.append(f"{sym}: 이번 달 목표 달성 → 새로 안 삼"); continue
            if stop_why and style == "surge":
                results.append(f"{sym}: 📚 장중 학습 — {stop_why}"); continue
            if state()["orders_today"] >= c["max_orders_per_day"]:
                results.append(f"{sym}: 오늘 매수 횟수 한도({c['max_orders_per_day']}) 도달"); continue
            if len(pos) >= c["pick_count"]:
                results.append(f"{sym}: 이미 {c['pick_count']}종목 보유 중"); continue
            buy_px = price * (1 + c["limit_slip_pct"] / 100) if style == "surge" else price
            est, size_note = cands[sym]["est"], ""
            nw = cands[sym].get("news")
            if nw and nw.get("pending"):
                results.append(f"{sym}: 📰 공시 확인 중 (끝나면 다음 바퀴에 판단)"); continue
            if nw and nw.get("ok") and nw["bad"]:   # 📰 악재 공시 → 안 삼
                msg = news.note(nw)
                if not _seen_news_block(sym):
                    event("공시 악재", f"{sym} 안 삼 — {msg}", symbol=sym)
                tl.flag_watch(wday, sym, news="악재")
                results.append(f"{sym}: {msg} → 안 삼"); continue
            cap_pos, cap_ord = caps["position"], caps["order"]
            if focus_ok(est, c) and caps["total"] * c["focus_max_pct"] / 100 > cap_pos:   # 🎯 확신할 때만 몰아넣기 ('종목당 최대'는 그대로 지킴)
                cap_pos = _cap(caps["total"] * c["focus_max_pct"] / 100, c["max_position_krw"])
                cap_ord = _cap(cap_pos, c["max_order_krw"])
                if cap_pos > caps["position"]:
                    size_note = f"🎯 확신 집중: 비슷한 거래 {est['n']}건 기대 {est['ev']:+.2f}% → 한도 {cap_pos:,.0f}원"
            budget = min(max(caps["order"], cap_ord), cap_pos - held["qty"] * price * f, caps["total"] - invested, cash_local * fx)
            if nw and nw.get("ok") and not nw["items"] and c["news_unknown_ratio"] < 1:   # 이유 모르는 급등 → 비중 축소
                budget *= c["news_unknown_ratio"]
                size_note = " · ".join(x for x in (size_note, f"📰 급등 이유 모름 → 금액 {c['news_unknown_ratio'] * 100:.0f}%") if x)
            full_budget = budget
            if c["prob_sizing"]:   # 🎲 비슷한 조건의 지난 거래로 승률 추정 → 금액 조절
                ratio_, note_ = tl.size_ratio(est, c)
                size_note = " · ".join(x for x in (size_note, note_) if x)
                if ratio_ <= 0:
                    event("확률 판단", f"{sym} 안 삼 — {size_note}", symbol=sym)
                    results.append(f"{sym}: 🎲 {size_note}"); continue
                full_budget, budget = budget, budget * ratio_
            qty = int(budget // (buy_px * f))
            if qty < 1 and c["prob_sizing"] and full_budget >= buy_px * f:   # 줄인 금액으론 1주가 안 되지만 한도 안에서 1주는 살 수 있으면 1주만
                qty = 1
                size_note += " (1주 미만이라 최소 1주)"
            if qty < 1:
                results.append(f"{sym}: 살 돈이 부족해요 (가능 {budget:,.0f}원, 1주 {buy_px * f:,.0f}원)"); continue
            chart_id, why = "", ""
            if c["ai_decide"]:   # 🤖 AI 판단: 차트·조건·기대 수익률 보고 살지·얼마나 (따로 생각 → 손절 확인은 안 멈춤)
                dec = _ai_async(("buy", market, sym), _ai_decide_buy, sym, cands[sym], price, c)
                if dec == "PENDING":
                    results.append(f"{sym}: 🤖 AI 가 판단 중 (끝나면 바로 다시 확인)"); continue
                if dec:
                    ok, mult, why, chart_id = dec
                    tl.flag_watch(wday, sym, ai="찬성" if ok else "반대", why=why)
                    if not ok:
                        event("AI 판단", f"{sym} 안 삼 — {why}", symbol=sym, chart=chart_id)
                        results.append(f"{sym}: 🤖 AI 판단으로 안 삼 — {why}"); continue
                    qty2 = int(budget * mult // (buy_px * f))
                    qty = max(1, qty2) if qty2 < 1 and full_budget >= buy_px * f else qty2
                    if qty < 1:
                        results.append(f"{sym}: AI 가 줄인 금액으론 1주도 못 사요"); continue
                    size_note += f" · 🤖 AI 금액 {mult * 100:.0f}%"
            elif c["ai_chart_check"] and style == "surge":   # 📊 AI 가 1분봉 차트 그림을 직접 보고 판단
                ok, why, chart_id = _ai_chart_ok(sym, reason, price)
                tl.flag_watch(wday, sym, ai="찬성" if ok else "반대", why=why)
                if not ok:
                    event("AI 차트 반대", f"{sym} 매수 보류 — {why}", symbol=sym, chart=chart_id)
                    results.append(f"{sym}: 📊 AI 가 차트 보고 반대 — {why}"); continue
            if c["ai_check"]:
                ok, why2 = _ai_ok(sym, side, reason, price)
                if not ok:
                    event("AI 반대", f"{sym} 매수 보류 — {why2}", symbol=sym); results.append(f"{sym}: AI 반대 — {why2}"); continue
            feat = dict(cands[sym]["features"])
            if c["ai_decide"] and why:
                feat["ai_decide"] = "산다"
            nnote = news.note(nw)
            results.append(_execute(sym, "BUY", qty, buy_px, reason + (f" · {nnote}" if nnote else "") + (f" · 🎲 {size_note}" if size_note else "")
                                    + (f" · 🤖 AI: {why}" if why else ""), live, fx, chart=chart_id, est=est, feat=feat, quote=price))
            invested += qty * buy_px * f; cash_local -= qty * buy_px; pos[sym] = {"qty": qty, "avg": buy_px}
        else:
            part = side == "SELL_PART"
            qty = int(held["qty"] * c["partial_tp_ratio"]) if part else int(held["qty"])
            if part and (qty < 1 or qty >= int(held["qty"])):   # 1주뿐이라 나눌 수 없으면 나눠 팔기 대신 계속 보유
                with _lock:
                    s4 = state(); s4["partial"][sym] = True; _save(s4)
                results.append(f"{sym}: 나눠 팔 수량이 안 돼서(보유 {held['qty']:g}주) 계속 보유 — 손절선은 본전으로"); continue
            if live and qty >= 1:   # 🔒 잠긴 봇 장부 기준 수량까지만 (사장님 주식 보호)
                try:
                    can = int(toss.bot_sellable(sym))
                except toss.TossError:
                    can = qty
                if can < qty:
                    results.append(f"{sym}: 봇 장부상 팔 수 있는 건 {can}주 → {can}주만")
                    qty = can
            if qty < 1:
                results.append(f"{sym}: 팔 수량 없음"); continue
            sell_px = price * (1 - c["limit_slip_pct"] / 100) if style == "surge" else price
            if tp_by_rule:
                _ghost_open(sym, tp_by_rule[0], tp_by_rule[1], price, tp_by_rule[2], tp_by_rule[3])
            results.append(_execute(sym, "SELL", qty, sell_px, reason, live, fx, partial=part, quote=price))
    if c["ai_take_profit"] == "test" and c["ai_decide"] and not dry:
        try:
            results += _ghost_step(c, market, minutes)
        except Exception as e:
            results.append(f"🤖 익절 비교 오류: {type(e).__name__}: {e}")
    if c["shadow_on"] and not dry:   # 🧪 같은 후보·시세로 규칙 조합들을 가상으로
        try:
            shadow.step(c, market, cands, quotes, minutes, fx, exits_only, signal_surge, signal_swing, market_open,
                        lambda x: toss.daily(x, 60)[0], s["day"])
        except Exception as e:
            results.append(f"🧪 그림자 매매 오류: {type(e).__name__}: {e}")
    return {"ok": True, "mode": mode, "results": results}


def _execute(sym: str, side: str, qty: int, price: float, reason: str, live: bool, fx: float, partial: bool = False,
             chart: str = "", est: dict | None = None, feat: dict | None = None, quote: float | None = None) -> str:
    coid = "cai-" + uuid.uuid4().hex[:20]
    px = toss.order_price(sym, price, side)
    label = f"{sym} {'매수' if side == 'BUY' else '매도'} {qty}주 @ {px}"
    rec = {"t": datetime.now(KST).strftime("%m-%d %H:%M:%S"), "symbol": sym, "side": side, "qty": qty, "price": float(px),
           "reason": reason, "mode": "live" if live else "paper", "client_order_id": coid, "partial": partial, "chart": chart}
    if side == "BUY":
        rec["features"] = dict(feat or {"style": "surge", "mkt": _mkt(sym)})
        if chart:
            rec["features"]["ai_chart"] = "찬성"
        if est:
            rec["features"].update(p=est["p"], ev=est["ev"])
    if live:
        try:
            r = toss.place_limit_order(sym, side, qty, price, coid)
            rec.update(order_id=r.get("orderId"), status="sent")
        except toss.TossError as e:
            rec.update(status="rejected", error=str(e))
            with _lock:
                s = state(); s["orders"].append(rec); _save(s)
            event("주문 거절", f"{label} — {e}", symbol=sym)
            mins_ = minutes_now(_mkt(sym))
            if side == "BUY" and mins_ is not None and mins_ < 0 and ("hours" in str(e).lower() or "시간" in str(e)):
                with _lock:   # 토스가 프리장 주문을 안 받음 → 일주일 동안 프리장 매수 안 함
                    s = state(); s["no_premarket_until"] = (datetime.now(KST) + timedelta(days=7)).strftime("%Y-%m-%d"); _save(s)
                event("실데이터", "토스가 프리장 주문을 받지 않아요 → 일주일 동안 프리장에는 사지 않고 정규장부터 살게요")
            return f"{sym}: 주문 거절 — {e}"
    else:
        f = 1 if _mkt(sym) == "KR" else fx
        p_ = tl.fill_px(config(), float(quote or price), side, float(px))   # 현재가 + 미끄러짐, 지정가보다 나쁘게는 X
        rec["fill"] = round(p_, 4)
        with _lock:
            s = state(); p = s["paper"]
            cur = p["positions"].get(sym, {"qty": 0, "avg": 0})
            if side == "BUY":
                p["cash"] -= qty * p_ * f * (1 + tl.side_cost(config(), _mkt(sym), "BUY") / 100)
                tot = cur["qty"] + qty
                p["positions"][sym] = {"qty": tot, "avg": (cur["avg"] * cur["qty"] + p_ * qty) / tot}
                s["highs"][sym] = max(float(s["highs"].get(sym, 0)), p_)
                s["style"][sym] = rec["features"].get("style", "surge")
                s["buy_day"].setdefault(sym, datetime.now(KST).strftime("%Y-%m-%d"))
                tl.open_trade(sym, qty, p_, rec.get("features"), "paper", s["day"])
            else:
                c_ = config(); mk_ = _mkt(sym)   # 수수료·세금 포함
                p["cash"] += qty * p_ * f * (1 - tl.side_cost(c_, mk_, "SELL") / 100)
                gain = (p_ * (1 - tl.side_cost(c_, mk_, "SELL") / 100) - cur["avg"] * (1 + tl.side_cost(c_, mk_, "BUY") / 100)) * qty * f
                p["realized"] += gain; s["realized_today"] = float(s.get("realized_today", 0)) + gain
                s["month"]["realized"] = float(s["month"].get("realized", 0)) + gain
                left = cur["qty"] - qty
                tl.on_sell(sym, qty, p_, reason, "paper", left <= 1e-9, config())
                if left > 1e-9:
                    p["positions"][sym] = {"qty": left, "avg": cur["avg"]}
                    if partial:
                        s["partial"][sym] = True
                else:
                    p["positions"].pop(sym, None); s["highs"].pop(sym, None); s["partial"].pop(sym, None)
                    s["trail_mode"].pop(sym, None); s["hold_log"].pop(sym, None); s["style"].pop(sym, None); s["buy_day"].pop(sym, None)
            _save(s)
        rec["status"] = "paper_filled"
    with _lock:
        s = state(); s["orders"].append(rec)
        if side == "BUY":
            s["orders_today"] += 1
        _save(s)
    event("실전 주문" if live else "가상 주문", f"{label} — {reason}", symbol=sym, **({"chart": chart} if chart else {}))
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
    c.pop("market", None)
    store.save(CONFIG, c)
    canceled, errors = 0, []
    if was == "live" and toss.enabled():
        mine: set[str] = set()
        for m in ("US", "KR"):   # 두 시장 모두
            with in_market(m):
                mine |= _bot_order_ids()
        try:
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
            "halted_today": s.get("halted_today", ""), "halt_release": halt_release_kst() if s.get("halted_today") else "", "paper": s["paper"], "orders": s["orders"][-60:][::-1],
            "events": s["events"][-100:][::-1], "running": bool(_thread and _thread.is_alive()),
            "picks": s.get("picks") or {}, "scanning": _busy().is_set(), "bot_pos": s.get("bot_pos", {}),
            "realized_today": s.get("realized_today", 0), "base_ready": picker.base_ready(c["market"]), "month": s.get("month", {}),
            "session_minutes": minutes_now(c["market"], c),
            "local_time": picker.mnow(c["market"]).strftime("%H:%M"), "progress": dict(picker.PROGRESS), "next_open": _next_open(c),
            "learn": tl.overview(s["day"], "live" if c["mode"] == "live" else "paper", c["market"]),
            "market": c["market"], "markets": c["markets"], "cands": s.get("cands", {}), "swing": s.get("swing") or {}, "style": s.get("style", {}),
            "brief": {m: _brief(m) for m in c["markets"]}, "lab": lab_overview(), "preflight": s.get("preflight") or {}}


def _brief(m: str) -> dict:
    """시장별 한 줄 상태 (화면 위쪽)"""
    with in_market(m):
        c, s = config(), state()
        pos = s["bot_pos"] if c["mode"] != "paper" else s["paper"]["positions"]
        return {"held": len(pos), "orders_today": s["orders_today"], "halted": s.get("halted_today", ""),
                "realized_today": round(float(s.get("realized_today", 0))), "minutes": minutes_now(m, c),
                "local_time": picker.mnow(m).strftime("%H:%M"), "next_open": _next_open(c)}


def _review_job(day: str, c: dict, mode_key: str) -> None:
    try:
        r = tl.daily_review(day, c, mode_key)
        with _lock:
            s = state(); s["reviewed_day"] = day; _save(s)
        t = r["today"]
        event("복기", f"{day} 거래 {t.get('n', 0)}건" + (f" · 승률 {t['win_rate']}% · 평균 {t['avg']:+.2f}%" if t.get("n") else "")
              + (f" · 조정 제안 {len(r['proposals'])}개" if r.get("proposals") else ""))
        if config()["auto_tune"]:
            auto_adjust(r, mode_key)
    except Exception as e:
        event("오류", f"복기 실패: {type(e).__name__}: {e}")
    c2 = config()
    try:
        if c2["shadow_on"] and c2["auto_promote"] and c2["mode"] != "off":
            msg = shadow.maybe_promote(c2, c2["market"], _apply_params)
            if msg:
                event("규칙 교체", msg)
    except Exception as e:
        event("오류", f"규칙 교체 실패: {type(e).__name__}: {e}")
    if c2["backtest_on"] and toss.enabled():
        backtest_now(wait=True)


def review_now(wait: bool = False) -> dict:
    """장 마감 후 복기 (자동 또는 화면 버튼)"""
    c = config()
    mode_key = "live" if c["mode"] == "live" else "paper"
    day = state()["day"]
    m = cur_market()

    def job():
        with in_market(m):
            _review_job(day, c, mode_key)

    if wait:
        job(); return {"ok": True}
    threading.Thread(target=job, daemon=True, name="review").start()
    return {"ok": True, "message": "복기 시작 (AI 요약까지 1분쯤)"}


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

    def tick(m: str, timers: dict) -> None:
        for msg in store.recovered():
            event("복구", msg)
        c = config()
        t = picker.mnow(m)
        # 장 전 준비: 미장은 뉴욕 07:00~ (프리장 켜면 00:30~), 국장은 07:30~ (평일)
        mins = minutes_now(m, c)
        prep_at = ("00:30" if _ext(m, c) else "07:00") if m == "US" else "07:30"
        if c["auto_pick"] and t.weekday() < 5 and t.strftime("%H:%M") >= prep_at:
            ready = (c["strategy"] == "trend" or picker.base_ready(m)) and \
                (c["strategy"] == "surge" or (state().get("swing") or {}).get("day") == picker.session_date(m))
            if not ready and not _busy().is_set():
                prepare_now()
        if mins is not None:
            sd = picker.session_date(m)
            s0 = state()
            if (s0.get("preflight") or {}).get("day") != sd:
                preflight()   # 🩺 그날 첫 장중 확인 전에 실데이터 점검 (휴장일·날짜 기준·형식)
                s0 = state()
            if (s0.get("preflight") or {}).get("closed"):
                return        # 휴장일
        if mins is None:
            # 📚 장 마감 후 하루 한 번 복기 (미장 뉴욕 16:05~, 국장 15:35~)
            s0 = state()
            after = t.strftime("%H:%M") >= ("16:05" if m == "US" else "15:35")
            if after and t.weekday() < 5 and s0.get("reviewed_day") != s0["day"] and s0["day"] == picker.session_date(m):
                with _lock:
                    s1 = state(); s1["reviewed_day"] = s1["day"]; _save(s1)
                review_now()
            return
        # 들고 있는 종목은 자주(기본 10초) 매도 조건만 확인
        gap = c["interval_min"] * 60
        if not (time.time() - timers["last"] >= gap - 5 or time.time() - timers["exit"] < c["exit_check_sec"]):
            with _lock:
                s0 = state()
            if s0["bot_pos"] if c["mode"] == "live" else s0["paper"]["positions"]:
                timers["exit"] = time.time()
                r = run_once(exits_only=True)
                for line in r.get("results", []):
                    if "✅" in line or "📝" in line or "거절" in line or "정지" in line:
                        _log_fn(("🇺🇸 " if m == "US" else "🇰🇷 ") + line)
        if time.time() - timers["last"] < gap and not _ai_wake[m].is_set():
            return
        _ai_wake[m].clear()
        timers["last"] = time.time()
        r = run_once()
        for line in r.get("results", []):
            if "✅" in line or "📝" in line or "거절" in line or "정지" in line:
                _log_fn(("🇺🇸 " if m == "US" else "🇰🇷 ") + line)

    def loop():
        timers = {"US": {"last": 0.0, "exit": 0.0}, "KR": {"last": 0.0, "exit": 0.0}}
        while True:
            time.sleep(5)
            if config()["mode"] == "off":
                continue
            for m in markets():   # 국장·미장을 각자 시간에 맞춰
                with in_market(m):
                    try:
                        tick(m, timers[m])
                    except Exception as e:
                        event("오류", f"{type(e).__name__}: {e}")

    _thread = threading.Thread(target=loop, daemon=True, name="autotrade")
    _thread.start()


ASK_WORDS = re.compile(r"자동\s*매매|봇|자동으로\s*(산|판|사|팔)|알아서\s*(산|판)|오늘\s*(매매|거래|수익|손익)|이번\s*달\s*(수익|목표)")


def summary_for_prompt() -> str:
    """채팅 AI 에게 붙여줄 '자동매매가 한 일' 요약 — 돌리는 시장 전부"""
    parts = []
    for m in markets():
        with in_market(m):
            parts.append(f"[{'🇺🇸 미장' if m == 'US' else '🇰🇷 국장'}]\n" + _summary_one().replace("<autotrade>", "").replace("</autotrade>", "").strip())
    return "<autotrade>\n" + "\n\n".join(parts) + "\n</autotrade>"


def _summary_one() -> str:
    """한 시장의 요약 (자동매매가 산 것만 구분해서)."""
    c, s = config(), state()
    mk = "미장" if c["market"] == "US" else "국장"
    lines = ["<autotrade>", "이 앱의 자동매매 기록이에요. '자동매매가 산 주식'은 사장님이 직접 산 주식과 별개예요. 숫자는 여기 값만 쓰고 지어내지 마세요.",
             f"상태: {dict(off='꺼짐', paper='가상 매매', live='실전')[c['mode']]} · {mk} · {STRAT_NAME[c['strategy']]} · "
             f"{'계좌 현금 전부' if c['budget_mode'] == 'all_cash' else '한도 ' + format(c['max_total_krw'], ',') + '원'} · 한 달 목표 {c['monthly_target_pct']}%"
             + (f" · 오늘 정지됨({s['halted_today']})" if s.get("halted_today") else "")]
    pos = s["bot_pos"] if c["mode"] != "paper" else s["paper"]["positions"]
    lines.append("자동매매가 지금 가진 주식: " + (", ".join(f"{k} {v['qty']:g}주(평단 {v['avg']:g}, {'스윙' if s['style'].get(k) == 'swing' else '단타'})" for k, v in pos.items()) or "없음"))
    lines.append(f"오늘({s['day']}) 실현 손익: {float(s.get('realized_today', 0)):+,.0f}원 · 오늘 매수 {s['orders_today']}번")
    mo = s.get("month") or {}
    if mo.get("start"):
        lines.append(f"이번 달 실현 손익: {float(mo.get('realized', 0)):+,.0f}원 ({float(mo.get('realized', 0)) / mo['start'] * 100:+.1f}%, 시작 예산 {mo['start']:,.0f}원)")
    recent = s["orders"][-12:]
    if recent:
        lines.append("최근 주문:")
        lines += [f"- {o['t']} {o['symbol']} {'매수' if o['side'] == 'BUY' else '매도'} {o['qty']}주 @ {o['price']:g} · {o.get('status', '')} · {o.get('reason', '')}" for o in recent]
    pk = s.get("picks") or {}
    if pk.get("picks"):
        lines.append(f"최근 고른 종목({pk.get('time', '')}): " + ", ".join(f"{x['name']}({x['symbol']})" for x in pk["picks"]))
    rv = tl.last_review_text("live" if c["mode"] == "live" else "paper", c["market"])
    if rv:
        lines.append(rv)
    lines.append("</autotrade>")
    return "\n".join(lines)



# ── 채팅으로 엔진과 대화 (🤖 자동매매 팀) ──
LABELS = {"ai_take_profit": "AI 익절 (off/test/on)", "ai_tp_guard_peak": "AI 익절 안전장치 최고 수익(%)", "ai_tp_guard_floor": "AI 익절 안전장치 바닥(%)", "news_unknown_ratio": "이유 모를 때 금액 배수", "focus_max_pct": "확신 집중 최대 비중(%)", "focus_min_samples": "확신 집중 최소 표본(건)", "focus_min_ev_pct": "확신 집중 최소 기대 수익(%)", "monthly_target_pct": "한 달 목표(%)", "pick_count": "동시 보유 종목 수", "max_total_krw": "총 투자 한도(원)",
          "max_order_krw": "한 번 주문 최대(원)", "max_position_krw": "종목당 최대(원)", "max_orders_per_day": "하루 매수 횟수",
          "max_daily_loss_pct": "하루 손실 한도(예산 %)", "max_daily_loss_krw": "하루 손실 한도(원)", "stop_loss_pct": "손절(%)",
          "take_profit_pct": "남은 물량 익절(%)", "partial_tp_pct": "나눠 팔기 수익률(%)", "partial_tp_ratio": "나눠 팔기 비율",
          "trail_pct": "고점 대비 하락 매도(%)", "trail_wide_after_pct": "넓은 기준 쓰는 최고 수익(%)", "trail_wide_pct": "넓은 고점 대비 기준(%)", "surge_min_change_pct": "급등 최소 상승률(%)", "surge_max_change_pct": "급등 최대 상승률(%)",
          "surge_volume_ratio": "거래량 배수 기준", "pre_volume_ratio": "프리장 거래량 기준", "min_trading_value_krw": "최소 거래대금(원)",
          "us_min_price": "최소 주가($)", "limit_slip_pct": "지정가 여유(%)", "interval_min": "종목 찾기 간격(분)",
          "exit_check_sec": "손절 확인 간격(초)", "loss_streak_pause": "연속 손실 쉬기 기준", "pause_min": "쉬는 시간(분)",
          "us_premarket": "프리장 매매", "stop_at_target": "목표 달성 시 쉬기", "ai_check": "주문 전 AI 검토", "auto_pick": "종목 알아서 고르기",
          "market": "시장", "strategy": "전략", "budget_mode": "예산 방식", "mode": "모드",
          "stop_min": "손절 자동 최소(%)", "stop_max": "손절 자동 최대(%)", "limit_min_mult": "한도 자동 최소 배수", "limit_max_mult": "한도 자동 최대 배수",
          "swing_stop_pct": "스윙 손절(%)", "swing_trail_pct": "스윙 고점 대비(%)", "swing_max_days": "스윙 최대 보유일",
          "ai_decide": "거래마다 AI 판단", "shadow_on": "그림자 가상 매매", "auto_promote": "규칙 자동 교체", "backtest_on": "백테스트",
          "loss_pct_min": "손실 한도 자동 최소(%)", "loss_pct_max": "손실 한도 자동 최대(%)", "auto_tune": "자동 조절", "auto_mode": "실전↔가상 자동"}
TRADE_GUIDE = """[자동매매 행동 태그 — 사장님이 원할 때만, 답 맨 아래에]
- 설정 바꾸기: [[TRADE_SET: 키=값 | 이유]] (여러 개면 줄마다 하나). 키는 <engine> 의 '설정' 목록에 있는 영문 키만. 예: [[TRADE_SET: partial_tp_pct=5 | 나눠 팔기를 조금 일찍]]
- 모드: [[TRADE_MODE: paper | 이유]] 또는 [[TRADE_MODE: off | 이유]] (live 는 채팅으로 불가)
- 긴급 정지: [[TRADE_STOP: 이유]]
- 종목 제외/해제: [[TRADE_EXCLUDE: TSLA | 이유]] · [[TRADE_INCLUDE: TSLA]]
태그는 바로 실행되지 않고 채팅에 [적용] 카드로 떠요. 사장님이 원하지 않았는데 태그를 쓰지 마세요."""


def engine_context() -> str:
    """🤖 자동매매 팀이 볼 엔진 전체 상태 — 돌리는 시장 전부"""
    parts = []
    for m in markets():
        with in_market(m):
            parts.append(f"[{'🇺🇸 미장' if m == 'US' else '🇰🇷 국장'}]\n" + _engine_one().replace("<engine>", "").replace("</engine>", "").strip())
    return "<engine>\n" + "\n\n".join(parts) + "\n</engine>"


def _engine_one() -> str:
    c, s = config(), state()
    mode_key = "live" if c["mode"] == "live" else "paper"
    m = minutes_now(c["market"], c)
    lines = ["<engine>", _summary_one().replace("<autotrade>", "").replace("</autotrade>", "").strip(),
             "설정 (키 = 값 · 뜻): " + " / ".join(f"{k}={c[k]}({LABELS[k]})" for k in LABELS if k in c),
             f"제외 종목: {', '.join(c['exclude']) or '없음'} · 직접 고른 종목: {', '.join(c['symbols']) or '없음'}",
             f"시장 시각: 현지 {picker.mnow(c['market']).strftime('%H:%M')} · "
             + ("장 닫힘" if m is None else ("프리장" if m < 0 else f"정규장 {m}분째"))
             + f" · 다음 종목 찾기 {_next_open(c) or '-'} (한국 시간)"]
    if s.get("cands"):
        lines.append("최근 후보(기대 수익률 순): " + " / ".join(f"{k}({'스윙' if v['style'] == 'swing' else '단타'}, 기대 {v['ev']:+.2f}%)" for k, v in
                                                       sorted(s["cands"].items(), key=lambda kv: -kv[1]["ev"])[:8]))
    pk = s.get("picks") or {}
    if pk.get("picks") or pk.get("note"):
        lines.append(f"최근 종목 찾기 결과({pk.get('time', '')}): {pk.get('note', '')}")
        lines += [f"- {x['name']}({x['symbol']}) 점수 {x['score']} · {x['reason']}" for x in pk.get("picks", [])]
    ev = s["events"][-25:]
    if ev:
        lines.append("최근 활동 기록:")
        lines += [f"- {e['t']} [{e['kind']}] {e['text']}" for e in ev]
    L = tl.overview(s["day"], mode_key, c["market"])
    td, g = L["today"], L["today"].get("guard", {})
    line = f"오늘 학습: 끝난 거래 {td.get('n', 0)}건"
    if td.get("n"):
        line += f" · 승률 {td['win_rate']}% · 평균 {td['avg']:+.2f}%"
    if td.get("open"):
        line += " · 보유 중 " + ", ".join(f"{o['sym']} 최대{o['mfe']:+.1f}%/최저{o['mae']:+.1f}%" for o in td["open"])
    if g.get("pause_until") or g.get("no_premarket"):
        line += f" · 조심 규칙: {g.get('pause_why') or ''} {g.get('no_premarket') or ''}"
    lines.append(line)
    for t in L["trades"][:10]:
        f = t.get("features") or {}
        lines.append(f"- 일지 {t['entry_time'][5:16]} {t['sym']} " + (f"{t.get('pnl_pct', 0):+.2f}% {t.get('exit_kind', '')}" if t["closed"] else "보유 중")
                     + f" · 최대 {t['mfe']:+.1f}% 최저 {t['mae']:+.1f}% · 살 때 {f.get('change')}% 거래량 {f.get('ratio')}배{' 프리장' if f.get('pre') else ''}")
    if L["proposals"]:
        lines.append("학습 제안: " + " / ".join(f"{p['label']} {p['from']}→{p['to']}({'적용됨' if p['status'] == 'applied' else '대기'}: {p['why']})"
                                              for p in L["proposals"][:6]))
    lines.append("</engine>")
    return "\n".join(lines)


_ACT = re.compile(r"\[\[TRADE_(SET|MODE|STOP|EXCLUDE|INCLUDE):\s*([^\]|]*?)\s*(?:\|\s*([^\]]*?))?\s*\]\]")


def find_actions(reply: str) -> list[dict]:
    """AI 답의 행동 태그 → [적용] 카드용 (실행은 안 함)"""
    c = config()
    out = []
    for kind, arg, why in _ACT.findall(reply or ""):
        a = {"id": uuid.uuid4().hex[:8], "type": kind.lower(), "why": (why or "").strip()}
        if kind == "SET":
            if "=" not in arg:
                continue
            k, v = [x.strip() for x in arg.split("=", 1)]
            if k not in LABELS or k == "mode":   # 모드는 TRADE_MODE 로만 (실전 켜기는 채팅 불가)
                continue
            a.update(param=k, value=v, label=f"{LABELS[k]}: {c.get(k)} → {v}")
        elif kind == "MODE":
            v = arg.strip().lower()
            if v not in ("paper", "off"):
                continue
            a.update(value=v, label=f"모드: {c['mode']} → {dict(paper='가상 매매', off='끄기')[v]}")
        elif kind == "STOP":
            a.update(label="⛔ 긴급 정지 (꺼짐 + 자동매매 미체결 주문 취소)", why=(arg or why or "").strip())
        else:
            sym = toss.symbol(arg.strip())
            if not re.fullmatch(r"\d{6}|[A-Z][A-Z.\-]{0,9}", sym):
                continue
            a.update(value=sym, label=f"{'사지 않을 종목에 추가' if kind == 'EXCLUDE' else '제외 해제'}: {sym}")
        out.append(a)
    return out[:8]


def apply_action(a: dict) -> dict:
    """사장님이 [적용]을 누른 행동 실행 (설정 검사는 save_config 가 함)"""
    t = a.get("type")
    c = config()
    if t == "set":
        k, v = a.get("param"), a.get("value")
        if k not in LABELS or k == "mode":
            return {"ok": False, "message": "바꿀 수 없는 설정이에요"}
        if isinstance(DEFAULTS.get(k), bool):
            v = str(v).strip().lower() in ("1", "true", "on", "켜기", "yes", "예")
        r = save_config({k: v})
    elif t == "mode":
        if a.get("value") not in ("paper", "off"):
            return {"ok": False, "message": "채팅으로는 가상 매매·끄기만 돼요 (실전은 🤖 화면에서)"}
        r = save_config({"mode": a["value"]})
    elif t == "stop":
        r = emergency_stop()
    elif t in ("exclude", "include"):
        ex = [x for x in c["exclude"] if x != a.get("value")] + ([a["value"]] if t == "exclude" else [])
        r = save_config({"exclude": ex})
    else:
        return {"ok": False, "message": "모르는 행동이에요"}
    if r.get("ok"):
        event("채팅 지시", f"{a.get('label', t)}" + (f" — {a['why']}" if a.get("why") else ""))
    return r


# ── 📊 차트 ──
def _ai_chart_ok(sym: str, reason: str, price: float) -> tuple[bool, str, str]:
    """사기 직전: 오늘 1분봉 차트를 그려서 AI 가 그림을 보고 판단. 실패하면 안전하게 '반대'."""
    from . import chart
    from . import providers as pv
    from . import team as tm
    try:
        name = chart.draw(sym, "1m", title=sym)
    except Exception as e:
        return False, f"차트를 못 그렸어요({type(e).__name__})", ""
    prompt = (f"{sym} 급등주 매수 직전이에요. 규칙상 이유: {reason}. 현재가 {price}.\n첨부한 오늘 1분봉 차트(캔들·MA5/20·VWAP·거래량)를 보고 "
              "지금 사도 되는지 판단하세요. 꼭대기에서 꺾이는 중·거래량이 줄며 밀리는 중·VWAP 아래로 이탈이면 반대, "
              "눌림 후 재상승·거래량 늘며 고점 돌파·VWAP 위 지지면 찬성. 한 줄 이유 후 마지막 줄에 '판정: 찬성' 또는 '판정: 반대'.")
    try:
        reply = pv.chat(tm.fast_spec("claudecode:sonnet"), "너는 단타 차트 판독가다. 짧게.", [{"role": "user", "content": prompt}],
                        effort="low", timeout=90, images=[str(chart.path(name))])
    except Exception as e:
        return False, f"AI 차트 확인 실패({type(e).__name__})", name
    last = [l for l in reply.strip().splitlines() if "판정" in l][-1:] or [""]
    ok = "찬성" in last[0] and "반대" not in last[0]
    return ok, reply.strip().splitlines()[0][:160], name


_live_cache: dict[str, dict] = {}


def auto_charts(limit: int = 6) -> list[dict]:
    """📊 실시간 차트: 자동매매가 가진 종목 + 방금 고른 종목 (최대 6개), 종목당 1분에 한 번만 다시 그림 (파일은 덮어씀)"""
    c, s = config(), state()
    if not toss.enabled():
        return []
    held = list((s["bot_pos"] if c["mode"] != "paper" else s["paper"]["positions"]).keys())
    picks = list((s.get("cands") or {}).keys()) or [x["symbol"] for x in (s.get("picks") or {}).get("picks", [])]
    out = []
    for sym in list(dict.fromkeys(held + picks))[:limit]:
        hit = _live_cache.get(sym)
        if not hit or time.time() - hit["at"] > 55:
            try:
                r = draw_chart(sym, "1m", live=True)
                hit = _live_cache[sym] = {**r, "at": time.time()}
            except Exception as e:
                hit = {"error": str(e), "at": time.time()}
                _live_cache[sym] = hit
        out.append({"sym": sym, "held": sym in held, **{k: v for k, v in hit.items() if k != "at"}, "v": int(hit["at"])})
    return out


def draw_chart(symbol: str, interval: str = "1m", live: bool = False) -> dict:
    with in_market(_mkt(toss.symbol(symbol or "X"))):   # 그 종목 시장의 기록 기준
        return _draw_chart(symbol, interval, live)


def _draw_chart(symbol: str, interval: str = "1m", live: bool = False) -> dict:
    """화면용 차트: 자동매매가 가진 종목이면 산 가격·손절·나눠 팔기·익절·고점 대비 하락선과 매수/매도 표시"""
    from . import chart
    sym = toss.symbol(symbol or "")
    if not re.fullmatch(r"\d{6}|[A-Z][A-Z.\-]{0,9}", sym):
        raise ValueError("종목 코드를 넣어 주세요 (예: NVDA, 005930)")
    if not toss.enabled():
        raise ValueError("토스 키가 있어야 차트를 그려요 (🧰 도구)")
    c, s = config(), state()
    pos = (s["bot_pos"] if c["mode"] != "paper" else s["paper"]["positions"]).get(sym)
    lines = {}
    if pos and pos.get("avg"):
        a = pos["avg"]
        lines = {"산 가격": a, f"손절 -{c['stop_loss_pct']:g}%": a * (1 - c["stop_loss_pct"] / 100),
                 f"익절 +{c['take_profit_pct']:g}%": a * (1 + c["take_profit_pct"] / 100)}
        if c.get("partial_tp_pct") and not s["partial"].get(sym):
            lines[f"나눠 팔기 +{c['partial_tp_pct']:g}%"] = a * (1 + c["partial_tp_pct"] / 100)
        hi = s["highs"].get(sym)
        if hi:
            tr = trail_for(a, hi, c)
            lines[f"고점 -{tr:g}%"] = hi * (1 - tr / 100)
    marks = []
    for o in s["orders"][-80:]:
        if o["symbol"] == sym and o.get("status") not in ("rejected",):
            try:
                t = datetime.strptime(f"{datetime.now(KST).year}-{o['t']}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST).isoformat()
            except ValueError:
                continue
            marks.append({"t": t, "side": o["side"], "px": o["price"]})
    name = chart.draw(sym, "1d" if interval == "1d" else "1m", lines=lines, marks=marks if interval == "1m" else None, title=sym,
                      name=f"live-{sym}-{interval}.png" if live else "")
    return {"id": name, "label": f"{sym} {'일봉' if interval == '1d' else '1분봉'}", "lines": {k: round(v, 4) for k, v in lines.items()}}



# ── 🔁 자동 조절 (매일 복기 직후, 범위 안에서만) ──
def _step(old, to, max_rel: float = 0.3):
    """자동 적용은 하루에 최대 ±30% 만큼만 (한 번에 크게 바뀌지 않게, 숫자가 아니면 그대로)"""
    if isinstance(old, bool) or isinstance(to, bool) or not isinstance(old, (int, float)) or not old:
        return to
    lim = abs(old) * max_rel
    v = old + max(-lim, min(lim, float(to) - old))
    return round(v, 2)


def auto_adjust(rep: dict, mode_key: str) -> list[str]:
    c = config()
    done: list[str] = []
    if c["mode"] == "off":   # 꺼져 있으면 아무것도 안 바꿈
        return done
    sums = tl.day_sums(mode_key, 5, c["market"])
    last_good = bool(sums) and sums[-1] > 0
    two_bad = len(sums) >= 2 and sums[-2] < 0 and sums[-1] < 0

    def change(param: str, new, why: str, evidence: str):
        old = c.get(param)
        if old == new:
            return
        r = save_config({param: new}, _by_auto=True)
        if r.get("ok"):
            tl.record_auto(param, LABELS.get(param, param), old, new, why, evidence)
            event("자동 조정", f"{LABELS.get(param, param)} {old} → {new} — {why} ({evidence})")
            c[param] = new
            done.append(param)

    # 1) 학습 제안 자동 적용 (손절은 stop_min~stop_max 로 자름)
    if c["auto_rules"] or c["auto_stop"]:
        pend = {x["id"]: x for x in tl.overview(state()["day"], mode_key, c["market"])["proposals"] if x["status"] == "pending"}
        for pid in rep.get("proposals") or []:
            p = pend.get(pid)
            if not p:
                continue
            if p["param"] == "stop_loss_pct":
                if c["auto_stop"]:
                    to = min(c["stop_max"], max(c["stop_min"], _step(c["stop_loss_pct"], float(p["to"]))))
                    if tl.decide(pid, "apply", auto=True, to=to).get("ok"):
                        done.append("stop_loss_pct")
            elif c["auto_rules"] and tl.decide(pid, "apply", auto=True, to=_step(c.get(p["param"]), p["to"])).get("ok"):
                done.append(p["param"])
        c = config()

    r30 = rep.get("recent") or {}
    # 2) 종목당·주문 한도: 최근 성적 좋으면 +20%, 나쁘면 -20% (처음 값의 배수 범위 안)
    if c["auto_limits"] and c["max_position_krw"] > 0 and r30.get("n", 0) >= 10:
        with _lock:
            s = state()
            base = s.setdefault("limit_base", {"position": c["max_position_krw"], "order": c["max_order_krw"]})
            _save(s)
        lo, hi = base["position"] * c["limit_min_mult"], base["position"] * c["limit_max_mult"]
        cur = c["max_position_krw"]
        new, why = cur, ""
        if r30["avg"] < 0 or two_bad:   # 줄이는 건 먼저 (최근 30건이 마이너스거나 2일 연속 손실)
            new, why = round(max(lo, cur * 0.8) / 1000) * 1000, "최근 성적이 나빠서 줄임" if r30["avg"] < 0 else "2일 연속 손실이라 줄임"
        elif r30["avg"] > 0 and r30["win_rate"] >= 45 and last_good:   # 늘리는 건 최근 30건 좋고 + 어제(마지막 날)도 수익일 때만
            new, why = round(min(hi, cur * 1.2) / 1000) * 1000, "최근 성적이 좋고 마지막 날도 수익이라 늘림"
        if new != cur:
            ev = f"최근 {r30['n']}건 승률 {r30['win_rate']}% · 평균 {r30['avg']:+.2f}% · 범위 {lo:,.0f}~{hi:,.0f}원"
            change("max_position_krw", new, why, ev)
            if c["max_order_krw"] > 0:
                o_lo = (base["order"] or base["position"]) * c["limit_min_mult"]
                change("max_order_krw", round(min(new, max(o_lo, c["max_order_krw"] * new / cur)) / 1000) * 1000, why, ev)

    # 3) 하루 손실 한도: 2일 연속 손실이면 조이고, 3일 연속 수익이면 늘림
    if c["auto_loss"] and len(sums) >= 2:
        three_good = len(sums) >= 3 and all(x > 0 for x in sums[-3:])
        ev_bad = f"최근 하루 손익 {sums[-2]:+.1f}%, {sums[-1]:+.1f}%"
        ev_good = "최근 하루 손익 " + ", ".join(f"{x:+.1f}%" for x in sums[-3:])
        if c["budget_mode"] == "all_cash":
            cur = c["max_daily_loss_pct"]
            if two_bad:
                change("max_daily_loss_pct", max(c["loss_pct_min"], round(cur - 1, 1)), "2일 연속 손실이라 하루 손실 한도를 조임", ev_bad)
            elif three_good:
                change("max_daily_loss_pct", min(c["loss_pct_max"], round(cur + 1, 1)), "3일 연속 수익이라 하루 손실 한도를 늘림", ev_good)
        else:
            with _lock:
                s = state(); lb = s.setdefault("loss_base", c["max_daily_loss_krw"]); _save(s)
            cur = c["max_daily_loss_krw"]
            if two_bad:
                change("max_daily_loss_krw", int(max(lb * 0.5, cur * 0.8)), "2일 연속 손실이라 하루 손실 한도를 조임", ev_bad)
            elif three_good:
                change("max_daily_loss_krw", int(min(lb * 2, cur * 1.2)), "3일 연속 수익이라 하루 손실 한도를 늘림", ev_good)

    # 4) 실전 ↔ 가상 (끄기 상태는 건드리지 않음)
    if c["auto_mode"]:
        with _lock:
            s = state()
        mo = s.get("month") or {}
        month_pct = float(mo.get("realized", 0)) / mo["start"] * 100 if mo.get("start") else 0
        if c["mode"] == "live":
            k = int(c["mode_down_days"])
            bad_days = len(sums) >= k and all(x < 0 for x in sums[-k:])
            if bad_days or month_pct <= -c["mode_down_month_pct"]:
                why = f"{k}일 연속 손실" if bad_days else f"이번 달 {month_pct:+.1f}% (기준 -{c['mode_down_month_pct']:g}%)"
                if save_config({"mode": "paper"}, _by_auto=True).get("ok"):
                    with _lock:
                        s = state(); s["auto_paper"] = True; s["auto_paper_since"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"); _save(s)
                    tl.record_auto("mode", "모드", "live", "paper", "성적이 나빠 가상으로 내림", why)
                    event("자동 조정", f"모드 실전 → 가상 — {why}. 가상 성적이 회복되면 실전으로 자동 복귀")
                    done.append("mode")
        elif c["mode"] == "paper" and s.get("auto_paper") and s.get("user_live_ok") and toss.enabled():
            st_ = tl._stats(tl.trades_since("paper", s.get("auto_paper_since", ""), c["market"]))
            if st_.get("n", 0) >= c["mode_up_trades"] and st_["win_rate"] >= 50 and st_["avg"] > 0:
                if save_config({"mode": "live"}, _by_auto=True).get("ok"):
                    with _lock:
                        s = state(); s["auto_paper"] = False; _save(s)
                    ev = f"가상 {st_['n']}건 승률 {st_['win_rate']}% · 평균 {st_['avg']:+.2f}%"
                    tl.record_auto("mode", "모드", "paper", "live", "가상 성적이 회복돼 실전으로 복귀", ev)
                    event("자동 조정", f"모드 가상 → 실전 — {ev}")
                    done.append("mode")
    if done:
        event("자동 조정", f"오늘 자동으로 바꾼 것: {', '.join(LABELS.get(x, x) for x in dict.fromkeys(done))}")
    return done


def halt_release_kst() -> str:
    """오늘 정지가 자동으로 풀리는 한국 시각 (그 시장의 날짜가 바뀌는 때)"""
    c = config()
    now = datetime.now(KST)
    day = picker.session_date(c["market"])
    for m in range(0, 60 * 48, 5):
        t = now + timedelta(minutes=m)
        if picker.session_date(c["market"], t) != day:
            return ("오늘 " if t.date() == now.date() else "내일 ") + t.strftime("%H:%M")
    return ""


def unhalt() -> dict:
    """사장님이 직접 오늘 정지 풀기 (화면 버튼 — 채팅으로는 불가)"""
    with _lock:
        s = state()
        was = s.get("halted_today", "")
        if not was:
            return {"ok": False, "message": "지금 정지 상태가 아니에요"}
        s["halted_today"] = ""
        s["day_budget"] = 0.0
        _save(s)
    event("정지 해제", f"사장님이 직접 오늘 정지를 풀었어요 (이전 이유: {was})")
    return {"ok": True, "message": "정지를 풀었어요. 하루 손실 한도는 지금 예산 기준으로 다시 계산돼요"}


# ── 🤖 거래마다 AI 판단 ──
def _ai_budget_ok(c: dict) -> bool:
    """시간당 AI 판단 횟수 제한 (넘으면 규칙대로)"""
    now = time.time()
    with _lock:
        s = state()
        calls = [t for t in s.get("ai_calls", []) if now - t < 3600]
        if len(calls) >= c["ai_max_per_hour"]:
            return False
        calls.append(now)
        s["ai_calls"] = calls; _save(s)
    return True


def _ai_json(prompt: str, image: str | None) -> dict | None:
    from . import providers as pv
    from . import team as tm
    try:
        reply = pv.chat(tm.fast_spec("claudecode:sonnet"), "너는 신중한 트레이더다. JSON 한 줄로만 답한다.",
                        [{"role": "user", "content": prompt}], effort="low", timeout=90, images=[image] if image else [])
    except Exception:
        return None
    m = re.search(r"\{.*\}", reply, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None


def _ai_decide_buy(sym: str, cd: dict, price: float, c: dict):
    """사기 전: 차트·조건·추정 승률을 보고 (살지, 금액 배수 0.25~1, 이유, 차트). 실패·한도 초과면 None → 규칙대로"""
    if not _ai_budget_ok(c):
        return None
    from . import chart
    iv = "1d" if cd["style"] == "swing" else "1m"
    try:
        name = chart.draw(sym, iv, title=sym)
        img = str(chart.path(name))
    except Exception:
        name, img = "", None
    e = cd["est"]
    prompt = (f"{'스윙(며칠 보유)' if cd['style'] == 'swing' else '단타(그날 정리)'} 매수 후보 {sym}, 현재가 {price}.\n"
              f"고른 이유: {cd['reason']}\n"
              + (f"최근 공시: {news.note(cd.get('news'))} (급등 이유가 공시로 설명되면 좋은 신호, 이유가 없으면 신중히)\n" if news.note(cd.get('news')) else "")
              + f"학습 기록 추정: 승률 {e['p'] * 100:.0f}% ({e['basis']}), 기대 수익 {e['ev']:+.2f}%\n"
              f"규칙: 손절 -{c['swing_stop_pct' if cd['style'] == 'swing' else 'stop_loss_pct']}%. "
              f"첨부 {'일봉' if iv == '1d' else '1분봉'} 차트(이동평균·VWAP·거래량)를 보고 지금 살지, 산다면 금액을 얼마나(0.25~1) 할지 정하세요. "
              "꼭대기 추격·거래량 감소 하락·지지선 이탈이면 안 사요. 수익률을 최대화하는 쪽으로.\n"
              '형식: {"buy": true/false, "size": 0.25~1, "reason": "한 줄"}')
    r = _ai_json(prompt, img)
    if not r or "buy" not in r:
        return None
    size = max(0.25, min(1.0, float(r.get("size") or 1)))
    return bool(r["buy"]), size, str(r.get("reason", ""))[:140], name


_ai_jobs: dict[tuple, dict] = {}
_ai_wake = {"US": threading.Event(), "KR": threading.Event()}


def _ai_async(key: tuple, fn, *args):
    """AI 판단을 따로(백그라운드) 돌림 → 끝났으면 결과, 아직이면 'PENDING', 실패면 None (규칙대로)"""
    job = _ai_jobs.get(key)
    if job:
        if job["status"] == "pending":
            return "PENDING"
        if time.time() - job["t"] < 180:   # 3분 안의 판단만 씀
            _ai_jobs.pop(key, None)
            return job["result"]
        _ai_jobs.pop(key, None)
    m = cur_market()
    _ai_jobs[key] = {"status": "pending", "t": time.time(), "result": None}

    def run():
        with in_market(m):
            try:
                res = fn(*args)
            except Exception:
                res = None
            _ai_jobs[key] = {"status": "done", "t": time.time(), "result": res}
            _ai_wake[m].set()   # 결과 나오면 바로 한 바퀴 더
    threading.Thread(target=run, daemon=True, name="ai-decide").start()
    return "PENDING"


def _ai_peek(key: tuple):
    job = _ai_jobs.get(key)
    if not job:
        return None
    if job["status"] == "pending":
        return "PENDING"
    if time.time() - job["t"] >= 180:
        _ai_jobs.pop(key, None)
        return None
    return job["result"]


def _ai_hold_due(sym: str, c: dict) -> bool:
    with _lock:
        s = state()
        last = s.setdefault("ai_hold_at", {}).get(sym, 0)
        if time.time() - last < c["ai_hold_check_min"] * 60:
            return False
        s["ai_hold_at"][sym] = time.time(); _save(s)
    return True


def _ai_decide_hold(sym: str, held: dict, price: float, high: float, style: str, rule_note: str, c: dict):
    """보유 중: AI 가 차트 보고 일찍 팔지 (팔기, 이유, 차트). 손절·마감 정리는 규칙이 따로 처리"""
    if not _ai_budget_ok(c):
        return None
    from . import chart
    try:
        name = chart.draw(sym, "1d" if style == "swing" else "1m", title=sym, lines={"산 가격": held["avg"]})
        img = str(chart.path(name))
    except Exception:
        name, img = "", None
    pnl = (price / held["avg"] - 1) * 100 if held["avg"] else 0
    prompt = (f"{'스윙' if style == 'swing' else '단타'} 보유 종목 {sym}: 평단 {held['avg']:g}, 현재가 {price:g} ({pnl:+.1f}%), 산 뒤 최고가 {high:g}.\n"
              f"지금 규칙 판단: {rule_note}\n첨부 차트를 보고 지금 일찍 파는 게 수익률에 나은지 판단하세요. "
              "추세가 살아 있으면 들고 가고, 꺾이는 게 뚜렷하면 팔아요. 애매하면 들고 가요.\n"
              '형식: {"sell": true/false, "reason": "한 줄"}')
    r = _ai_json(prompt, img)
    if not r or "sell" not in r:
        return None
    return bool(r["sell"]), str(r.get("reason", ""))[:140], name


# ── 🧪📼 실험실 ──
def _apply_params(params: dict, why: str, evidence: str) -> bool:
    """그림자 매매에서 나은 조합 → 실전 설정 (손절은 자동 범위 안으로)"""
    c = config()
    new = {k: v for k, v in params.items() if k in shadow.TUNE_KEYS and c.get(k) != v}
    if "stop_loss_pct" in new:
        new["stop_loss_pct"] = min(c["stop_max"], max(c["stop_min"], float(new["stop_loss_pct"])))
    new = {k: v for k, v in new.items() if c.get(k) != v}
    if not new:
        return False
    r = save_config(new, _by_auto=True)
    if not r.get("ok"):
        return False
    for k, v in new.items():
        tl.record_auto(k, LABELS.get(k, k), c.get(k), v, why, evidence)
    return True


def backtest_now(wait: bool = False) -> dict:
    c = config()
    m = cur_market()

    def job():
        with in_market(m):
            try:
                r = bt.run(c, m, signal_surge, signal_swing, state().get("swing_hist", []), log=_log_fn)
                n = shadow.add_seeds(config(), m, bt.seeds(m)) if c["shadow_on"] else 0
                event("백테스트", r["note"] + (f" · 상위 조합 {n}개를 그림자 매매에 추가" if n else ""))
            except Exception as e:
                event("오류", f"백테스트 실패: {type(e).__name__}: {e}")
    if wait:
        job(); return {"ok": True}
    threading.Thread(target=job, daemon=True, name="backtest").start()
    return {"ok": True, "message": "백테스트 시작 (몇 분)"}


def lab_overview() -> dict:
    c = config()
    return {"shadow": shadow.overview(c, c["market"]) if c["shadow_on"] else {}, "backtest": bt.load(c["market"])}


# ── 🩺 실데이터 점검 (그날 처음 장이 열릴 때 자동 · 화면 버튼) ──
REF_SYMBOL = {"US": "AAPL", "KR": "005930"}


def _calendar_open(market: str, day: str):
    """토스 휴장일 정보 → True(열림)/False(휴장)/None(모름). 응답 형식이 확실치 않아 여러 이름을 확인"""
    try:
        r = toss._get(f"/api/v1/market-calendar/{market}", date=day)
    except toss.TossError:
        return None
    items = r if isinstance(r, list) else [r] if isinstance(r, dict) else []
    for it in items:
        if not isinstance(it, dict):
            continue
        if it.get("date") and str(it["date"])[:10] != day:
            continue
        for k in ("isOpen", "open", "isTradingDay", "tradingDay", "marketOpen", "isBusinessDay", "businessDay"):
            if isinstance(it.get(k), bool):
                return it[k]
        for k in ("isHoliday", "holiday", "closed", "isClosed"):
            if isinstance(it.get(k), bool):
                return not it[k]
    return None


def preflight() -> dict:
    """실제 토스 데이터로 엔진이 기대하는 형식이 맞는지 확인. 중요한 게 틀리면 그날 새로 사지 않음 (보유 종목 관리는 계속)"""
    c = config()
    m = c["market"]
    day = picker.session_date(m)
    checks: list[tuple[str, bool, str, bool]] = []   # (이름, 통과, 설명, 중요)

    def add(name, ok, detail, critical=True):
        checks.append((name, bool(ok), detail, critical))

    closed = False
    if not toss.enabled():
        add("토스 연결", False, "토스 키가 없어요", True)
    else:
        try:
            toss.account_seq(); add("로그인·계좌", True, "종합계좌 찾음")
        except toss.TossError as e:
            add("로그인·계좌", False, str(e))
        op = _calendar_open(m, day)
        if op is False:
            closed = True
        add("휴장일", True, "오늘 휴장 → 매매 안 함" if closed else ("장 열리는 날" if op else "휴장 정보 형식을 몰라서 주말만 쉬어요"), False)
        ref = REF_SYMBOL[m]
        try:
            r = toss._get("/api/v1/candles", symbol=ref, interval="1d", count=5) or {}
            cs = r.get("candles") or []
            bad = []
            for x in cs:   # 엔진은 날짜를 timestamp 앞 10글자로 비교 → 그 시장 날짜와 같아야 함
                ts = datetime.fromisoformat(str(x["timestamp"]).replace("Z", "+00:00"))
                if ts.tzinfo and picker.session_date(m, ts) != str(x["timestamp"])[:10]:
                    bad.append(str(x["timestamp"]))
            add("차트 날짜 기준", bool(cs) and not bad, f"{len(cs)}개 확인" + (f" · 날짜가 어긋나는 봉 {len(bad)}개 (예: {bad[0]})" if bad else ""))
        except (toss.TossError, ValueError, KeyError) as e:
            add("차트 날짜 기준", False, f"일봉을 못 읽었어요: {e}")
        try:
            q = toss.prices([ref]).get(ref) or {}
            ts = q.get("time")
            fresh = True
            mins = minutes_now(m, c)
            if ts and mins is not None and mins >= 5:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)).total_seconds() / 60
                fresh = age < 20
                add("현재가 신선도", fresh, f"{age:.0f}분 전 가격", False)
            else:
                add("현재가", bool(q.get("price")), f"{q.get('price')} (시각 {ts or '없음'})")
        except (toss.TossError, ValueError) as e:
            add("현재가", False, str(e))
        try:
            h = toss.holdings()
            items = h.get("items") or []
            ok = all({"symbol", "quantity", "averagePurchasePrice"} <= set(it) for it in items)
            add("보유 종목 형식", ok, f"보유 {len(items)}개" + ("" if ok else " · 필요한 칸(symbol/quantity/averagePurchasePrice)이 없어요"))
        except toss.TossError as e:
            add("보유 종목 형식", False, str(e))
        try:
            bp = toss.buying_power("KRW" if m == "KR" else "USD") or {}
            float(bp.get("cashBuyingPower"))
            add("주문 가능 현금", True, f"{bp.get('cashBuyingPower')} {'원' if m == 'KR' else '달러'}")
        except (toss.TossError, TypeError, ValueError) as e:
            add("주문 가능 현금", False, f"형식이 달라요: {e}")
        try:
            oo = toss.open_orders()
            add("미체결 주문 형식", isinstance(oo, list), f"{len(oo)}건")
        except toss.TossError as e:
            add("미체결 주문 형식", False, str(e))
    failed = [x for x in checks if x[3] and not x[1]]
    res = {"day": day, "at": datetime.now(KST).strftime("%H:%M"), "closed": closed, "ok": not failed,
           "checks": [{"name": n, "ok": o, "detail": d, "critical": cr} for n, o, d, cr in checks]}
    with _lock:
        s = state(); s["preflight"] = res; _save(s)
    if closed:
        event("휴장", f"{'미장' if m == 'US' else '국장'} 오늘({day}) 휴장 → 매매 안 함")
    elif failed:
        why = "실데이터 점검 실패: " + " · ".join(f"{n}({d})" for n, _, d, _ in failed)
        if c["mode"] == "live":
            _halt_today(why)   # 새로 사지 않음 (보유 종목 손절·정리는 계속)
        event("실데이터", why + (" → 오늘은 새로 사지 않아요" if c["mode"] == "live" else ""))
    else:
        event("실데이터", "점검 통과: " + " · ".join(f"{n}" for n, *_ in checks))
    return res
