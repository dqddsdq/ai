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
    need = {".env", "engine/toss.py", "engine/improve.py", "engine/tools.py", "engine/guard.py", "engine/__init__.py",
            "engine/__main__.py", "engine/notify.py", "engine/selftest.py", "_shared/autotrade.json", "_shared/tools_catalog.json"}
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


LOCKED_PY = {"toss.py", "selftest.py", "improve.py", "tools.py", "notify.py", "guard.py", "__init__.py", "__main__.py"}


def scan_source(src: str, name: str = "?") -> list[str]:
    """토스 우회 검사: 토스 주소·헤더 직접 쓰기, toss 모듈 내부(_token·_post·_http 등) 접근, toss 함수 바꿔치기"""
    import ast
    bad = []
    tree = ast.parse(src)
    alias = {"toss"}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            for a in n.names:
                if a.name == "toss":
                    alias.add(a.asname or "toss")
                if mod.endswith("toss") and (a.name == "*" or (a.name.startswith("_") and a.name != "_get")):
                    bad.append(f"{name}:{n.lineno} toss 내부 가져오기 ({a.name})")
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.endswith(".toss") or a.name == "toss":
                    alias.add(a.asname or a.name.split(".")[-1])
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and ("tossinvest" in n.value.lower()):
            bad.append(f"{name}:{n.lineno} 토스 주소·헤더 직접 사용")
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and ("ledger" in n.value.lower() or "toss_token" in n.value.lower()):
            bad.append(f"{name}:{n.lineno} 봇 장부·토큰 파일 직접 접근")
        elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in alias:
            if n.attr.startswith("_") and n.attr != "_get":
                bad.append(f"{name}:{n.lineno} toss.{n.attr} 접근")
            if isinstance(n.ctx, (ast.Store, ast.Del)):
                bad.append(f"{name}:{n.lineno} toss.{n.attr} 바꿔치기")
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("getattr", "setattr", "delattr", "vars") \
                and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id in alias:
            bad.append(f"{name}:{n.lineno} {n.func.id}(toss, …)")
        elif isinstance(n, ast.Constant) and n.value == "sys.modules":
            pass
        elif isinstance(n, ast.Attribute) and n.attr == "modules" and isinstance(n.value, ast.Name) and n.value.id == "sys":
            bad.append(f"{name}:{n.lineno} sys.modules 조작")
    return bad


@case
def 엔진_코드는_토스를_우회하지_않음():
    base = Path(__file__).resolve().parent
    bad = []
    for p in sorted(base.rglob("*.py")):
        if p.name in LOCKED_PY and p.parent == base or "__pycache__" in p.parts:
            continue
        bad += scan_source(p.read_text(encoding="utf-8"), p.relative_to(base.parent).as_posix())
    assert not bad, bad[:5]
    # 검사기 자체도 확인
    assert scan_source("from . import toss\ntoss._token()")
    assert scan_source("from . import toss as t\nt._post('/x', {})")
    assert scan_source("from .toss import _http")
    assert scan_source("from . import toss\ntoss.place_limit_order = None")
    assert scan_source("import urllib\nu='https://openapi.TOSSINVEST.com'")
    assert scan_source("from . import toss\ngetattr(toss, '_tok')")
    assert scan_source("open('_cache/toss_ledger.json', 'w')")
    assert not scan_source("from . import toss\ntoss._get('/api/v1/stocks')\ntoss.prices(['A'])")


@case
def 채팅으로_실전_못_켬_정지_못_풂():
    from . import autotrade as at
    acts = at.find_actions("[[TRADE_MODE: live | 수익]] [[TRADE_SET: mode=live | x]] [[TRADE_SET: halted_today= | x]]")
    assert not acts, acts
    assert not at.apply_action({"type": "mode", "value": "live"})["ok"]
    assert not at.apply_action({"type": "set", "param": "mode", "value": "live"})["ok"]
    assert not at.apply_action({"type": "unhalt"})["ok"]
    assert "unhalt" not in at.LABELS and "mode" in at.DEFAULTS


@case
def 설정_허용_범위():
    """손실 한도·손절·주문 수 같은 안전 범위를 넓히지 못하게 (매매 코드가 바뀌어도)"""
    from . import autotrade as at
    limit = {"stop_loss_pct": 50, "max_daily_loss_pct": 50, "loss_pct_max": 50, "stop_max": 30, "limit_max_mult": 20,
             "max_orders_per_day": 100, "pick_count": 100, "focus_max_pct": 100, "ai_max_per_hour": 600}
    for k, hi in limit.items():
        assert k in at.NUMS and at.NUMS[k][1] <= hi, (k, at.NUMS.get(k))
    assert at.DEFAULTS["mode"] != "live" and at.CHOICES["mode"] == ("off", "paper", "live")


@case
def 폰_접속은_같은_와이파이_PIN_만():
    """개인정보 보호: 앱 화면은 이 PC 나 같은 와이파이(사설 IP)에서만, 폰은 PIN, 다른 사이트의 몰래 요청은 차단"""
    import inspect
    from . import web
    assert web._private_ip("192.168.0.5") and web._private_ip("10.1.2.3") and web._private_ip("172.16.0.9")
    assert not web._private_ip("8.8.8.8") and not web._private_ip("1.2.3.4")
    src = inspect.getsource(web)
    for need in ('"X-Company-AI"', "secrets_eq(", "PIN", '"127.0.0.1"'):
        assert need in src, f"web.py 에서 보안 장치({need})가 빠졌어요"
    assert '"0.0.0.0" if ip else "127.0.0.1"' in src, "폰 접속을 안 켰는데 외부에 열리면 안 돼요"


@case
def 키는_화면_기록에_안_나옴():
    """개인정보·키: .env 의 값이 화면용 도구 목록·진단에 그대로 나가지 않게"""
    import os
    from . import tools
    os.environ["TOSS_CLIENT_SECRET"] = os.environ.get("TOSS_CLIENT_SECRET") or "tssk_selftest_secret_value"
    secret = os.environ["TOSS_CLIENT_SECRET"]
    try:
        dump = json.dumps(tools.overview() if hasattr(tools, "overview") else {}, ensure_ascii=False)
    except Exception:
        dump = ""
    assert secret not in dump
    if secret == "tssk_selftest_secret_value":
        os.environ.pop("TOSS_CLIENT_SECRET", None)


@case
def 미수_신용_안씀():
    """현금보다 큰 매수는 토스에 보내기 전에 막히고, 미수·신용 한도는 아예 안 넘어옴"""
    from . import toss
    sent = []
    orig = toss._get, toss._post, toss.TOKEN_CACHE
    tmpd = tempfile.mkdtemp()
    toss.TOKEN_CACHE = Path(tmpd) / "t.json"
    (Path(tmpd) / "toss_ledger.json").write_text('{"v": 1, "seed": {"AAPL": 5}, "orders": {}}')
    toss._get = lambda path, account=False, **kw: {"cashBuyingPower": "100", "marginBuyingPower": "100000", "creditLimit": "9", "currency": kw.get("currency")}
    toss._post = lambda path, body: (sent.append(body), {"orderId": f"x{len(sent)}"})[1]
    try:
        bp = toss.buying_power("USD")
        assert set(bp) == {"cashBuyingPower", "currency"}, bp
        try:
            toss.place_limit_order("AAPL", "BUY", 1, 150, "t1")
            raise AssertionError("현금보다 큰 매수가 통과됨")
        except toss.TossError:
            pass
        assert not sent
        toss.place_limit_order("AAPL", "BUY", 1, 90, "t2")
        toss.place_limit_order("AAPL", "SELL", 5, 150, "t3")   # 매도는 현금 확인 없음
        assert len(sent) == 2
    finally:
        toss._get, toss._post, toss.TOKEN_CACHE = orig


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


@case
def 사장님_주식_주문은_안_건드림():
    """봇 장부(잠긴 토스 파일): 팔기는 봇이 산 체결 수량까지만, 취소는 봇이 낸 주문만"""
    from . import toss
    orig = toss._get, toss._post, toss.TOKEN_CACHE
    tmpd = Path(tempfile.mkdtemp())
    toss.TOKEN_CACHE = tmpd / "t.json"
    orders = {}

    def fake_get(path, account=False, **kw):
        if path.startswith("/api/v1/orders/"):
            return orders[path.split("/")[4]]
        return {"cashBuyingPower": "100000"}

    def fake_post(path, body):
        if path.endswith("/cancel"):
            return {"ok": True}
        oid = f"o{len(orders) + 1}"
        orders[oid] = {"status": "PENDING", "execution": {"filledQuantity": "0"}}
        return {"orderId": oid}
    toss._get, toss._post = fake_get, fake_post
    try:
        def blocked(fn, *a):
            try:
                fn(*a)
                return False
            except toss.TossError:
                return True
        assert blocked(toss.place_limit_order, "TSLA", "SELL", 1, 100, "a")           # 봇이 안 산 종목(사장님 주식) 매도 막힘
        toss.place_limit_order("TSLA", "BUY", 3, 100, "b")                               # 봇 매수 3주 (아직 미체결)
        assert blocked(toss.place_limit_order, "TSLA", "SELL", 1, 100, "c")           # 체결 전엔 못 팖
        orders["o1"] = {"status": "FILLED", "execution": {"filledQuantity": "3"}}
        assert blocked(toss.place_limit_order, "TSLA", "SELL", 4, 100, "d")           # 산 것보다 많이 못 팖 (사장님 주식 섞여 있어도)
        toss.place_limit_order("TSLA", "SELL", 2, 100, "e")                              # 2주 매도 걸어 둠
        assert toss.bot_sellable("TSLA") == 1                                            # 걸린 매도는 빼고 계산
        assert blocked(toss.place_limit_order, "TSLA", "SELL", 2, 100, "f")
        assert blocked(toss.cancel_order, "manual123")                                   # 사장님이 낸 주문 취소 막힘
        toss.cancel_order("o2")                                                          # 봇 주문은 취소 가능
        orders["o2"] = {"status": "CANCELED", "execution": {"filledQuantity": "1"}}
        assert toss.bot_sellable("TSLA") == 2                                            # 1주만 팔리고 취소 → 2주 남음
    finally:
        toss._get, toss._post, toss.TOKEN_CACHE = orig


@case
def 공시_악재_판정():
    from . import news
    k = news.classify_kr(["주요사항보고서(유상증자결정)", "전환사채권발행결정", "주권매매거래정지해제", "무상증자결정",
                          "단일판매ㆍ공급계약체결", "임원ㆍ주요주주특정증권등소유상황보고서", "감자결정", "상장폐지사유발생"])
    assert k["bad"] == ["주요사항보고서(유상증자결정)", "전환사채권발행결정", "감자결정", "상장폐지사유발생"], k
    assert k["items"] == ["주권매매거래정지해제", "무상증자결정", "단일판매ㆍ공급계약체결"], k
    u = news.classify_us([{"form": "424B5"}, {"form": "8-K", "items": "3.01"}, {"form": "8-K", "items": "2.02,9.01"},
                          {"form": "4"}, {"form": "S-3"}, {"form": "8-K", "items": "1.01"}])
    assert len(u["bad"]) == 3 and len(u["items"]) == 2, u
    assert news.note({"ok": True, "src": "SEC", "bad": [], "items": []}).endswith("급등 이유 모름)")
    assert news.note({"ok": False}) == ""


@case
def 공시_확인이_느려도_매매가_안_멈춤():
    import time
    from . import news
    orig = news.check
    news.check = lambda sym, m: (time.sleep(2), {"ok": True, "bad": [], "items": []})[1]
    try:
        t0 = time.time()
        r = news.peek("ZZZTEST", "US")
        assert r.get("pending") and time.time() - t0 < 0.5, r
    finally:
        news.check = orig


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
def AI_익절_모드():
    """익절 규칙만 꺼지고 손절·마감 정리는 그대로, 안전장치(+3% → +0.3%)는 꼭 작동"""
    from . import autotrade as at
    c = _cfg(stop_loss_pct=3, partial_tp_pct=4, partial_tp_ratio=0.5, trail_pct=4, trail_wide_after_pct=10, trail_wide_pct=6,
             limit_slip_pct=0.5, ai_tp_guard_peak=3, ai_tp_guard_floor=0.3)
    nt = at._no_tp(c)
    s = lambda price, high, mins=100: at.signal_surge(price, 10, 100, high, mins, False, nt)[0]  # noqa: E731
    assert s(104.2, 104.2) == "HOLD" and s(100.1, 104.4) == "HOLD"   # 익절 규칙은 꺼짐
    assert s(96.9, 100) == "SELL" and s(101, 101, mins=375) == "SELL"   # 손절·마감 정리는 그대로
    assert at.tp_guard(100, 103.5, 100.2, c) and not at.tp_guard(100, 103.5, 101, c) and not at.tp_guard(100, 102, 100, c)
    assert at.signal_swing([100.0] * 30, 93.9, 1, 100, 100, 2, {**at._no_tp(_cfg(swing_stop_pct=6))})[0] == "SELL"
    assert "ai_take_profit" in at.CHOICES and at.DEFAULTS["ai_take_profit"] != "on"   # 처음부터 AI 에 맡기지 않음


@case
def 두_AI_장부_분리():
    """가상 AI 는 절대 실전 주문 못 냄 · 같은 장부면 몫으로 나눔 · 채팅으로 AI 실전 켜기 불가"""
    from . import autotrade as at
    c = _cfg(mode="live", lane_swing_mode="live", lane_surge_mode="paper", lane_share_surge=50)
    assert at.lane_live(c, "swing") and not at.lane_live(c, "surge")
    assert at.lane_book(c, "surge") == "paper" and at.lane_share(c, "swing") == 1.0 and at.lane_share(c, "surge") == 1.0
    c2 = {**c, "lane_surge_mode": "live", "lane_share_surge": 30}
    assert at.lane_live(c2, "surge") and abs(at.lane_share(c2, "surge") - 0.3) < 1e-9 and abs(at.lane_share(c2, "swing") - 0.7) < 1e-9
    c3 = {**c, "mode": "paper", "lane_surge_mode": "live"}
    assert not at.lane_live(c3, "surge") and not at.lane_live(c3, "swing")   # 전체가 가상이면 둘 다 가상
    assert not at.find_actions("[[TRADE_SET: lane_surge_mode=live | x]]")
    assert at.find_actions("[[TRADE_SET: lane_surge_mode=paper | x]]")
    assert not at.apply_action({"type": "set", "param": "lane_swing_mode", "value": "live"})["ok"]
    assert at.PERSONA["surge"] != at.PERSONA["swing"]


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


@case
def 윈도우_액세스_거부여도_저장되고_안_멈춤():
    import os
    from . import store
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.json"
        real, n = os.replace, [0]

        def flaky(a, b):
            n[0] += 1
            if n[0] <= 3:
                raise PermissionError(5, "액세스가 거부되었습니다")
            return real(a, b)
        os.replace = flaky
        try:
            store.save(p, {"a": 1})
            assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}
            os.replace = lambda a, b: (_ for _ in ()).throw(PermissionError(5, "거부"))
            store.save(p, {"a": 2})            # 계속 거부돼도 예외 없이 직접 덮어쓰기
            assert json.loads(p.read_text(encoding="utf-8")) == {"a": 2}
        finally:
            os.replace = real
        assert not list(Path(d).glob("*.tmp"))
        store.errors()


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
