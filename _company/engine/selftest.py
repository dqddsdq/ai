"""🧪 자체 점검 — 돈과 안전에 관련된 핵심 규칙이 그대로인지 확인 (인터넷·토스·AI 없이, 몇 초).

  python -m engine selftest

자기 개선이 코드를 바꿀 때도 적용 전에 자동으로 돌려서, 하나라도 실패하면 적용하지 않아요.
기록 파일(_cache)은 건드리지 않아요 (임시 폴더만 씀).
"""

from __future__ import annotations

import json
import tempfile
import traceback
from pathlib import Path

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def _cfg(**kw) -> dict:
    from . import autotrade as at
    return {**at.DEFAULTS, **kw}


# ── 안전 ────────────────────────────────────────────────
@case
def 토스_쓰기는_주문_생성_취소만():
    from . import toss
    ok = ["/api/v1/orders", "/api/v1/orders/abc-123/cancel"]
    bad = ["/api/v1/orders/abc/modify", "/api/v1/transfers", "/api/v1/withdraw", "/api/v1/accounts/1/transfer",
           "/api/v1/orders/../transfers", "/api/v1/orders/abc/cancel/x"]
    assert all(toss.WRITE_ALLOWED.match(p) for p in ok)
    assert not any(toss.WRITE_ALLOWED.match(p) for p in bad), [p for p in bad if toss.WRITE_ALLOWED.match(p)]


@case
def 자기개선_보호_파일():
    from . import improve
    need = {".env", "engine/toss.py", "engine/autotrade.py", "engine/tradelearn.py", "engine/improve.py", "engine/tools.py",
            "engine/notify.py", "engine/store.py", "engine/selftest.py", "_shared/autotrade.json"}
    assert need <= improve.PROTECTED, need - improve.PROTECTED


@case
def 텔레그램으로_실전_못_켬():
    from . import autotrade as at
    from . import notify
    calls = []
    orig = at.save_config, at.emergency_stop
    at.save_config = lambda new, **k: (calls.append(new), {"ok": True})[1]
    at.emergency_stop = lambda: (calls.append("STOP"), {"message": "x"})[1]
    try:
        for t in ("/실전", "/live", "실전 켜", "/mode live"):
            notify._handle(t)
        assert not any(isinstance(c, dict) and c.get("mode") == "live" for c in calls), calls
        notify._handle("/가상"); notify._handle("/끄기")
        assert calls[-2:] == [{"mode": "paper"}, {"mode": "off"}], calls
    finally:
        at.save_config, at.emergency_stop = orig


@case
def 미수_신용_안씀():
    import inspect
    from . import toss
    src = inspect.getsource(toss)
    assert "cashBuyingPower" in src


# ── 비용·체결 ──────────────────────────────────────────
@case
def 수수료_세금():
    from . import tradelearn as tl
    c = _cfg(fee_us_pct=0.1, fee_kr_pct=0.015, tax_kr_pct=0.18)
    assert abs(tl.rt_cost(c, "US") - 0.2) < 1e-9
    assert abs(tl.rt_cost(c, "KR") - (0.03 + 0.18)) < 1e-9
    assert tl.side_cost(c, "KR", "BUY") < tl.side_cost(c, "KR", "SELL")


@case
def 가상_체결은_불리하게_지정가보다_나쁘진_않게():
    from . import tradelearn as tl
    c = _cfg(sim_slip_pct=0.2)
    assert abs(tl.fill_px(c, 100, "BUY") - 100.2) < 1e-9
    assert abs(tl.fill_px(c, 100, "SELL") - 99.8) < 1e-9
    assert tl.fill_px(c, 100, "BUY", limit=100.1) == 100.1      # 지정가 이상으로는 안 삼
    assert tl.fill_px(c, 100, "SELL", limit=99.9) == 99.9       # 지정가 이하로는 안 팜
    assert tl.fill_px(c, 100, "BUY", limit=101) > 100           # 지정가 여유 있으면 현재가+미끄러짐


@case
def 확신할_때만_몰아넣기():
    from . import autotrade as at
    c = _cfg(focus_max_pct=50, focus_min_samples=20, focus_min_ev_pct=2)
    assert at.focus_ok({"n": 25, "ev": 2.5}, c)
    assert not at.focus_ok({"n": 10, "ev": 5}, c)          # 표본 부족 → 분산
    assert not at.focus_ok({"n": 40, "ev": 1.0}, c)        # 기대 수익 부족 → 분산
    assert not at.focus_ok({"n": 40, "ev": 5}, {**c, "focus_max_pct": 0})   # 꺼짐


# ── 매도 규칙 ──────────────────────────────────────────
@case
def 단타_매도_규칙():
    from . import autotrade as at
    c = _cfg(stop_loss_pct=3, partial_tp_pct=4, partial_tp_ratio=0.5, trail_pct=4, trail_wide_after_pct=10, trail_wide_pct=6,
             limit_slip_pct=0.5)
    s = lambda price, high, mins=100, part=False: at.signal_surge(price, 10, 100, high, mins, False, c, partial_done=part)[0]  # noqa: E731
    assert s(96.9, 100) == "SELL"                      # 손절
    assert s(104.2, 104.2) == "SELL_PART"              # 나눠 팔기
    assert s(100.5, 104.4, part=True) == "SELL"        # 나눠 판 뒤 본전 정리
    assert s(100.1, 104.4) == "SELL"                   # 무장된 고점 대비 하락 (YMAT 사례: +4.4% → 평단 근처)
    assert s(101, 103) == "HOLD"                       # 최고 +3% → 아직 무장 전
    assert s(106, 111, part=True) == "HOLD"                    # +11% 뒤엔 넓은 기준(-6%) → -4.5% 는 유지
    assert s(104, 111, part=True) == "SELL"                    # -6.3% → 매도
    assert s(101, 101, mins=375) == "SELL"             # 마감 15분 전 정리
    assert at.signal_surge(100, 0, 0, 0, 400, True, c)[0] == "HOLD"   # 마감 30분 전엔 새로 안 삼


@case
def 스윙_매도_규칙():
    from . import autotrade as at
    c = _cfg(swing_stop_pct=6, swing_trail_pct=8, swing_max_days=20)
    closes = [100.0] * 30
    assert at.signal_swing(closes, 93.9, 1, 100, 100, 2, c)[0] == "SELL"
    assert at.signal_swing(closes, 110, 1, 100, 120, 2, c)[0] == "SELL"
    assert at.signal_swing(closes, 101, 1, 100, 102, 20, c)[0] == "SELL"
    assert at.signal_swing(closes, 101, 1, 100, 102, 3, c)[0] == "HOLD"


@case
def 백테스트_비용_반영():
    from . import autotrade as at
    from . import backtest as bt
    c = _cfg(sim_slip_pct=0.1, limit_slip_pct=0.5, stop_loss_pct=3, partial_tp_pct=0, trail_pct=4)
    path = [{"t": "2026-01-05T15:00:00Z", "c": 100.0, "h": 100.0, "l": 100.0}]
    r = bt._sim_surge(path, 100, {}, c, at.signal_surge, "US")
    assert r is not None and r < 0, r   # 제자리여도 미끄러짐만큼 손해


# ── 저장 ──────────────────────────────────────────────
@case
def 기록_파일_깨지면_백업으로_복구():
    from . import store
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.json"
        store.save(p, {"a": 1})
        store._cache.pop(str(p), None)
        p.write_text("{깨짐", encoding="utf-8")
        assert store.load(p) == {"a": 1}
        assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}
        store.recovered()


def run(verbose: bool = True) -> tuple[int, int, list[str]]:
    fails = []
    for fn in CASES:
        try:
            fn()
            if verbose:
                print(f"  ✅ {fn.__name__.replace('_', ' ')}")
        except Exception as e:
            fails.append(f"{fn.__name__}: {type(e).__name__} {e}"[:300])
            if verbose:
                print(f"  ❌ {fn.__name__.replace('_', ' ')} — {type(e).__name__}: {e}")
                traceback.print_exc(limit=2)
    return len(CASES) - len(fails), len(CASES), fails


def main() -> int:
    print("🧪 자체 점검")
    ok, n, fails = run()
    print(f"\n{ok}/{n} 통과" + ("" if not fails else " — 실패한 게 있어요"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
