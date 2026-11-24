"""🧪 매매 흐름 시나리오 시험 — 가짜 토스 서버로 실제 사고파는 흐름을 처음부터 끝까지 (인터넷·진짜 계좌·AI 없이, 1~2분).

  cd _company && python -m tests.scenarios

자기 개선이 코드를 바꿀 때와 GitHub 에서 새 코드를 받을 때 자동으로 돌아요. 하나라도 실패하면 적용하지 않아요.
이 폴더(tests/)는 자기 개선이 못 고쳐요 (고칠 수 있는 폴더 밖).

시험하는 것
1. 프리장 강한 급등주는 실전으로 사고, 사장님이 직접 산 주식(AAPL)은 절대 안 건드림
2. 손절은 현재가보다 낮은 지정가로 바로 팔림 · 시간 손절(안 오르는 종목 정리)
3. '검증된 조건만 실전'이 켜져 있으면 기록 없는 종목은 실전 주문 없이 가상 장부로
4. 하루 손실 한도를 넘으면 새로 안 사고, 들고 있는 종목 정리는 계속
5. 같은 바퀴를 여러 번 돌아도 같은 종목을 두 번 사지 않음
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 종목: (어제 종가, 오늘 배수, 거래량 배수) · AAPL = 사장님이 직접 산 주식
U = {"SURG": (20.0, 1.25, 8), "PUMP": (8.0, 1.22, 6), "SLOW": (30.0, 1.08, 1.2), "AAPL": (190.0, 1.0, 1)}
S: dict = {}


def _reset():
    S.clear()
    S.update(orders=[], posts=[], mult={}, hold={"AAPL": ("3", "150")}, usd="1000")


def px(s):
    return U[s][0] * U[s][1] * S["mult"].get(s, 1.0)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _j(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/oauth2/token":
            return self._j(200, {"access_token": "T", "token_type": "Bearer", "expires_in": 86400})
        body = json.loads(raw or b"{}")
        S["posts"].append((self.path, body))
        if self.path == "/api/v1/orders":
            oid = f"bot{len(S['orders']) + 1}"
            S["orders"].append({"orderId": oid, "symbol": body["symbol"], "side": body["side"], "quantity": body["quantity"], "price": body["price"],
                                "status": "PENDING", "orderedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                                "execution": {"filledQuantity": "0"}})
            return self._j(200, {"result": {"orderId": oid}})
        if self.path.endswith("/cancel"):
            for o in S["orders"]:
                if o["orderId"] == self.path.split("/")[4]:
                    o["status"] = "CANCELED"
            return self._j(200, {"result": {"orderId": "c"}})
        self._j(500, {})

    def do_GET(self):
        from engine import picker
        today = picker.session_date("US")
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/api/v1/stocks":
            return self._j(200, {"result": [{"symbol": s, "name": s + " Inc", "market": "NASDAQ", "status": "ACTIVE", "isCommonShare": True,
                                             "leverageFactor": None} for s in q["symbols"].split(",") if s in U]})
        if u.path == "/api/v1/prices":
            return self._j(200, {"result": [{"symbol": s, "lastPrice": f"{px(s):.4f}", "currency": "USD"} for s in q["symbols"].split(",") if s in U]})
        if u.path == "/api/v1/candles":
            s = q.get("symbol")
            if s not in U:
                return self._j(200, {"result": {"candles": [], "nextBefore": None}})
            base, _, ratio = U[s]
            past = [{"timestamp": (datetime.fromisoformat(today) - timedelta(days=d)).strftime("%Y-%m-%dT16:00:00-05:00"), "closePrice": str(base),
                     "openPrice": str(base), "highPrice": str(base), "lowPrice": str(base), "volume": "1000000"} for d in range(1, 70)]
            now = {"timestamp": today + "T12:00:00-05:00", "closePrice": f"{px(s):.4f}", "openPrice": str(base), "highPrice": f"{px(s):.4f}",
                   "lowPrice": str(base), "volume": str(int(1000000 * ratio * 120 / 390))}
            return self._j(200, {"result": {"candles": ([now] + past)[:int(q.get("count", 30))], "nextBefore": None}})
        if u.path.endswith("/warnings"):
            return self._j(200, {"result": []})
        if u.path == "/api/v1/exchange-rate":
            return self._j(200, {"result": {"rate": "1400"}})
        if u.path == "/api/v1/accounts":
            return self._j(200, {"result": [{"accountSeq": 7, "accountType": "BROKERAGE"}]})
        if u.path == "/api/v1/holdings":
            return self._j(200, {"result": {"items": [{"symbol": k, "quantity": v[0], "averagePurchasePrice": v[1], "currency": "USD"} for k, v in S["hold"].items()]}})
        if u.path == "/api/v1/buying-power":
            return self._j(200, {"result": {"cashBuyingPower": S["usd"] if q.get("currency") == "USD" else "5000000"}})
        if u.path == "/api/v1/orders":
            return self._j(200, {"result": {"orders": [o for o in S["orders"] if o["status"] == "PENDING"], "nextCursor": None, "hasNext": False}})
        if u.path.startswith("/api/v1/orders/"):
            o = next((o for o in S["orders"] if o["orderId"] == u.path.split("/")[4]), None)
            return self._j(200, {"result": o}) if o else self._j(404, {"error": {"message": "없는 주문"}})
        self._j(404, {"error": {"message": u.path}})


def setup():
    """가짜 토스 서버 + 임시 폴더에 기록 (진짜 _cache·설정은 안 건드림)"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["TOSS_CLIENT_ID"], os.environ["TOSS_CLIENT_SECRET"] = "x", "y"
    os.environ.pop("SEC_CONTACT_EMAIL", None); os.environ.pop("TELEGRAM_BOT_TOKEN", None); os.environ.pop("DART_API_KEY", None)
    from engine import autotrade as at, extras, picker, shadow, store, toss, tradelearn as tl
    tmp = Path(tempfile.mkdtemp())
    toss.BASE = f"http://127.0.0.1:{srv.server_port}"; toss.TOKEN_CACHE = tmp / "t.json"; toss._throttle = lambda gap=0: None
    picker.us_symbols = lambda log=print: list(U)
    picker._base_file = lambda m: tmp / f"base_{m}.json"
    at.CONFIG, at.STATE = tmp / "c.json", tmp / "s.json"
    tl.FILE, tl.LESSONS = tmp / "j.json", tmp / "l.json"
    shadow._file = lambda m: tmp / f"sh_{m}.json"
    extras.RULE_LOG, extras.GATE_FILE = tmp / "rule.json", tmp / "gate.json"
    extras.index_change = lambda m: 0.0
    at._log_fn = lambda *a: None
    at.chase_check = lambda *a, **k: (True, {}, "")
    at.rebound_check = lambda *a, **k: (True, {}, "")
    from engine import agents
    agents.Agent.remember = lambda self, note: None   # 시험 교훈이 진짜 팀 기억에 안 들어가게
    from engine import arena as _arena
    _arena.ai_json = lambda prompt: None   # 생존전 AI 는 시험에서 안 부름
    _arena._file = lambda m: tmp / f"arena_{m}.json"
    return at, picker, tmp, store


MIN = [-200]


def fresh(at, picker, tmp, **cfg):
    _reset()
    for f in tmp.glob("*.json"):
        f.unlink()
    from engine import store
    store._cache.clear()
    picker.session_minutes = lambda m, now=None, extended=False: MIN[0]
    MIN[0] = -200
    at.CONFIG.write_text(json.dumps({"ai_decide": False, "v": 5, "markets": ["US"]}))
    base = {"mode": "live", "strategy": "surge", "budget_mode": "all_cash", "pick_count": 3, "min_trading_value_krw": 1000000000,
            "live_needs_proof": False, "lane_surge_mode": "live", "shadow_on": False, "backtest_on": False, "news_on": False,
            "regime_on": False, "tod_filter": False, "max_per_sector": 0, "surge_min_change_regular": 0, "max_position_krw": 0, "max_order_krw": 0}
    base.update(cfg)
    r = at.save_config(base)
    assert r.get("ok"), r
    s = at.state(); s["user_live_ok"] = True; at._save(s)
    picker.prepare_day(at.config(), log=lambda *a: None)


def posted(side):
    return [(b["symbol"], float(b["price"])) for p, b in S["posts"] if p == "/api/v1/orders" and b["side"] == side]


def fill_all():
    for o in S["orders"]:
        if o["status"] == "PENDING":
            o["status"] = "FILLED"; o["execution"] = {"filledQuantity": o["quantity"], "averageFilledPrice": o["price"]}
            if o["side"] == "BUY":
                S["hold"][o["symbol"]] = (o["quantity"], o["price"])
            else:
                S["hold"].pop(o["symbol"], None)


CASES = []


def case(fn):
    CASES.append(fn); return fn


@case
def 강한_급등주는_실전으로_사고_사장님_주식은_안_건드림(at, picker, tmp):
    fresh(at, picker, tmp)
    at.run_once()
    buys = [s for s, _ in posted("BUY")]
    assert "SURG" in buys or "PUMP" in buys, ("강한 급등주 실전 매수가 안 나감", buys, at.run_once.__name__)
    assert all(s != "AAPL" for s, _ in posted("BUY") + posted("SELL")), "사장님 주식을 건드림"


@case
def 같은_종목_두_번_안_삼(at, picker, tmp):
    fresh(at, picker, tmp)
    at.run_once(); at.run_once(); at.run_once()
    buys = [s for s, _ in posted("BUY")]
    assert len(buys) == len(set(buys)), ("같은 종목을 두 번 삼", buys)


@case
def 손절은_낮은_지정가로_바로_팔림(at, picker, tmp):
    fresh(at, picker, tmp)
    at.run_once(); fill_all(); at.run_once(exits_only=True)
    sym = posted("BUY")[0][0]
    S["mult"][sym] = 0.95
    n0 = len(S["posts"])
    at.run_once(exits_only=True)
    sells = [(b["symbol"], float(b["price"])) for p, b in S["posts"][n0:] if b.get("side") == "SELL"]
    assert sells and sells[0][0] == sym, ("손절 매도가 안 나감", sells)
    assert sells[0][1] <= px(sym) * 0.99, ("손절 지정가가 현재가보다 충분히 낮지 않음", sells[0][1], px(sym))
    assert all(s != "AAPL" for s, _ in posted("SELL")), "사장님 주식을 팜"


@case
def 시간_손절(at, picker, tmp):
    fresh(at, picker, tmp, time_stop_min=15, time_stop_up_pct=0.5)
    at.run_once(); fill_all(); at.run_once(exits_only=True)
    s = at.state()
    sym = next(iter(s["bot_pos"]))
    s["buy_t"][sym] = time.time() - 20 * 60; at._save(s)
    n0 = len(S["posts"])
    r = at.run_once(exits_only=True)
    assert any("시간 손절" in x for x in r["results"]), ("시간 손절이 안 됨", r["results"][:6])
    assert any(b.get("symbol") == sym and b.get("side") == "SELL" for _, b in S["posts"][n0:])


@case
def 검증_전에는_실전_주문_없이_가상으로(at, picker, tmp):
    fresh(at, picker, tmp, live_needs_proof=True)
    at.run_once()
    assert posted("BUY") == [], ("검증 전인데 실전 주문이 나감", posted("BUY"))
    assert at.state()["paper"]["positions"], "가상 장부에도 안 들어감"


@case
def 하루_손실_한도_넘으면_새로_안_삼_정리는_계속(at, picker, tmp):
    fresh(at, picker, tmp, max_daily_loss_pct=0.5)
    at.run_once(); fill_all(); at.run_once(exits_only=True)
    for k in list(S["mult"]) + [s for s, _ in posted("BUY")]:
        S["mult"][k] = 0.9
    at.run_once()
    n0 = len(S["posts"])
    S["mult"]["SLOW"] = 1.3   # 새 급등 후보가 생겨도
    at.run_once()
    new_buys = [b for p, b in S["posts"][n0:] if b.get("side") == "BUY"]
    assert not new_buys, ("손실 한도를 넘었는데 새로 삼", new_buys)
    assert at.state().get("halted_today"), "정지가 안 걸림"


def _arena_setup(live_idx, vote_idx):
    from engine import arena
    d = arena.load("US", 3)
    for i, a in enumerate(d["alive"]):
        a["trades"] = [{"sym": "X", "day": "D", "ret": 1.0 if i == 0 else -0.5, "why": "t", "mins": 5, "t": ""} for _ in range(30)]
    d["live"] = [d["alive"][live_idx]["id"]]
    arena._save("US", d)
    yes = d["alive"][vote_idx]["id"]
    arena.ai_json = lambda prompt: {yes: True}


def _arena_wait():
    from engine import arena
    for _ in range(100):
        if not arena._pending:
            return
        time.sleep(0.05)


@case
def 생존전_상위_AI_가_사면_실전으로(at, picker, tmp):
    fresh(at, picker, tmp, live_needs_proof=True, lane_surge_mode="paper", arena_size=3)
    _arena_setup(0, 0)
    r = at.run_once()
    assert any("생존전 AI 들이 판단 중" in x for x in r["results"]), r["results"][:8]
    _arena_wait()
    at.run_once()
    buys = [s for s, _ in posted("BUY")]
    assert buys, ("상위 AI 가 샀는데 실전 주문이 안 나감", at.state().get("events", [])[-3:])
    assert all(s != "AAPL" for s in buys), "사장님 주식을 건드림"
    assert set(at.state().get("arena_live_pos") or {}) <= set(buys)


@case
def 생존전_자격_없는_AI_판단은_가상으로만(at, picker, tmp):
    fresh(at, picker, tmp, live_needs_proof=True, lane_surge_mode="paper", arena_size=3)
    _arena_setup(0, 1)   # 실전 자격은 0번, 산다고 한 건 1번
    at.run_once(); _arena_wait(); at.run_once()
    assert posted("BUY") == [], ("자격 없는 AI 판단으로 실전 주문이 나감", posted("BUY"))
    from engine import arena
    arena.ai_json = lambda prompt: None


@case
def 생존전_실전_끄면_실전_주문_없음(at, picker, tmp):
    fresh(at, picker, tmp, live_needs_proof=True, lane_surge_mode="paper", arena_size=3, arena_live=False)
    _arena_setup(0, 0)
    at.run_once(); _arena_wait(); at.run_once()
    assert posted("BUY") == [], ("생존전 실전을 껐는데 실전 주문이 나감", posted("BUY"))
    from engine import arena
    arena.ai_json = lambda prompt: None


def main() -> int:
    print("🧪 매매 흐름 시나리오 시험")
    at, picker, tmp, _ = setup()
    fails = 0
    for fn in CASES:
        try:
            fn(at, picker, tmp)
            print(f"  ✅ {fn.__name__.replace('_', ' ')}")
        except Exception as e:
            fails += 1
            print(f"  ❌ {fn.__name__.replace('_', ' ')} — {type(e).__name__}: {e}")
            traceback.print_exc(limit=3)
    print(f"\n{len(CASES) - fails}/{len(CASES)} 통과" + (" — 실패한 게 있어요" if fails else ""))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
