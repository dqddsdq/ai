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
    tl.FILE, tl.LESSONS, tl.CORE = tmp / "j.json", tmp / "l.json", tmp / "core.json"
    shadow._file = lambda m: tmp / f"sh_{m}.json"
    extras.RULE_LOG, extras.GATE_FILE = tmp / "rule.json", tmp / "gate.json"
    extras.index_change = lambda m: 0.0
    at._log_fn = lambda *a: None
    at.chase_check = lambda *a, **k: (True, {}, "")
    at.rebound_check = lambda *a, **k: (True, {}, "")
    from engine import agents
    agents.Agent.remember = lambda self, note: None   # 시험 교훈이 진짜 팀 기억에 안 들어가게
    from engine import arena as _arena
    _arena.ai_json = lambda prompt: None   # 리그 AI 는 시험에서 안 부름
    _arena._file = lambda m: tmp / f"arena_{m}.json"
    from engine import edge as _edge
    _edge._file = lambda m: tmp / f"edge_{m}.json"
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
        a["trades"] = [{"sym": f"X{j}", "day": "D", "ret": 1.0 if i == 0 else -0.5, "why": "t", "mins": 5, "t": ""} for j in range(30)]
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
    assert any("리그 AI 들이 판단 중" in x for x in r["results"]), r["results"][:8]
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
    assert posted("BUY") == [], ("AI 리그 실전을 껐는데 실전 주문이 나감", posted("BUY"))
    from engine import arena
    arena.ai_json = lambda prompt: None


def _arena_fill(rets, ctrl_ret, sit="", age_days=0):
    """참가자 i 에게 rets[i] 수익 거래 30건, 대조군에 ctrl_ret 거래 30건"""
    from engine import arena
    t = (datetime.now(arena.KST) - timedelta(days=age_days)).strftime("%Y-%m-%d %H:%M:%S")
    tr = lambda r: [{"sym": f"X{j}", "day": "D", "ret": r, "why": "t", "mins": 5, "t": t, "sit": sit} for j in range(30)]   # 서로 다른 종목 (독립 표본)
    d = arena.load("US", len(rets))
    for a, r in zip(d["alive"], rets):
        a["trades"] = tr(r)
    d["ctrl"]["trades"] = tr(ctrl_ret)
    arena._save("US", d)
    return d


@case
def 무작위_대조군보다_못하면_실전_자격_없음(at, picker, tmp):
    fresh(at, picker, tmp, arena_size=2)
    from engine import arena
    _arena_fill([0.5, 0.6], ctrl_ret=0.8)   # 둘 다 플러스지만 동전 던지기보다 못함
    arena.evolve({**at.config(), "arena_on": True}, "US")
    assert arena.load("US").get("live") == [], ("대조군보다 못한 AI 가 실전 자격을 받음", arena.load("US").get("live"))


@case
def 대조군을_넘은_만큼_비중을_나누고_약한_AI_만_찬성하면_금액_줄임(at, picker, tmp):
    fresh(at, picker, tmp, arena_size=2)
    from engine import arena
    d = _arena_fill([2.0, 0.6], ctrl_ret=0.2)
    strong, weak = d["alive"][0]["id"], d["alive"][1]["id"]
    arena.evolve({**at.config(), "arena_on": True}, "US")
    d = arena.load("US")
    assert d["live"] == [strong, weak], d["live"]
    assert d["weights"][strong] > d["weights"][weak], d["weights"]
    d.setdefault("votes", {})["D1"] = {"ZZZ": [weak]}; arena._save("US", d)
    vs, who = arena.live_vote("US", "D1", "ZZZ")
    assert vs == "yes" and who["size"] < 1, ("약한 AI 만 찬성했는데 금액을 안 줄임", vs, who)
    d["votes"]["D1"]["ZZZ"] = [strong, weak]; arena._save("US", d)
    assert arena.live_vote("US", "D1", "ZZZ")[1]["size"] == 1.0


@case
def 지금_상황에서_대조군보다_못했던_AI_판단은_실전_안_감(at, picker, tmp):
    fresh(at, picker, tmp, arena_size=2)
    from engine import arena
    d = _arena_fill([-1.0, 0.0], ctrl_ret=0.5, sit="장 초반·하락장")
    d["alive"][0]["trades"] += [{"sym": "Y", "day": "D", "ret": 3.0, "why": "t", "mins": 5, "t": "", "sit": "전반·상승장"} for _ in range(40)]
    d["live"] = [d["alive"][0]["id"]]; d.setdefault("votes", {})["D1"] = {"ZZZ": [d["alive"][0]["id"]]}
    arena._save("US", d)
    old = arena._situation_now
    try:
        arena._situation_now = lambda c, m: "장 초반·하락장"
        vs, who = arena.live_vote("US", "D1", "ZZZ")
        assert vs == "no" and who and who["skipped"], ("약했던 상황인데 실전으로 감", vs, who)
        arena._situation_now = lambda c, m: "전반·상승장"
        assert arena.live_vote("US", "D1", "ZZZ")[0] == "yes", "잘했던 상황인데 막힘"
    finally:
        arena._situation_now = old


@case
def 오래된_성적은_덜_침(at, picker, tmp):
    from engine import arena
    old = [{"ret": 5.0, "t": (datetime.now(arena.KST) - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")}] * 10
    new = [{"ret": -1.0, "t": datetime.now(arena.KST).strftime("%Y-%m-%d %H:%M:%S")}] * 10
    w = arena._wavg(old + new, 14)
    assert w < 0, ("두 달 전 좋은 성적이 최근 나쁜 성적을 덮음", w)


@case
def 생존전_AI_자기_규칙은_바로_바뀌고_되돌릴_수_있고_공통_설정은_적용해야_바뀜(at, picker, tmp):
    fresh(at, picker, tmp, arena_size=2)
    from engine import agents as ag, arena, arena_voice, improve as im
    d = _arena_fill([1.5, -0.8], ctrl_ret=0.2)
    a0, a1 = d["alive"][0], d["alive"][1]
    reply = {a0["id"]: {"opinion": "익절을 조금 늦추면 더 먹을 수 있어요", "self": {"tp": 6, "sl": 999}, "habit": "하락장 장 초반엔 안 삼",
                        "engine": {"key": "stop_loss_pct", "value": 5, "why": "손절이 너무 빨라요"}, "code": "상황별 성적을 차트로 보여 주기"},
             a1["id"]: {"opinion": "저는 실전 켜자고 할게요", "self": None, "habit": "", "engine": {"key": "arena_live", "value": True, "why": "x"}, "code": ""}}
    old_sd, old_create, old_vf = ag.SESSIONS_DIR, im.create, arena_voice._file
    ag.SESSIONS_DIR = tmp / "sessions"
    im.create = lambda what, why, agent: {"id": "job1", "request": what}
    arena_voice._file = lambda: tmp / "arena_voice.json"
    try:
        stop0 = at.config()["stop_loss_pct"]
        # 의견은 모으지 않고 하나씩 바로: 첫 AI 만 물어봄 → 하나 옴
        assert arena_voice.ask(at.config(), "US", {a0["id"]: "거래 5건 더 쌓임"}, ai_fn=lambda p: reply) == 1
        cid = json.loads((tmp / "arena_voice.json").read_text(encoding="utf-8"))["conv"]["US"]
        read = lambda: json.loads((ag.SESSIONS_DIR / cid / "conversation.json").read_text(encoding="utf-8"))
        assert len(read()["messages"]) == 2, read()["messages"]   # 안내 1 + 의견 1
        # 할 말 없는 AI 는 안 보냄
        assert arena_voice.ask(at.config(), "US", {a1["id"]: "x"}, ai_fn=lambda p: {a1["id"]: {"opinion": ""}}) == 0
        # 다음 AI 의견은 같은 대화에 이어 붙음
        assert arena_voice.ask(at.config(), "US", {a1["id"]: "3번 연속 손실"}, ai_fn=lambda p: reply) == 1
        conv = read()
        assert conv["agent"] == "trader" and len(conv["messages"]) == 3, conv["messages"]
        assert "왜 지금: 거래 5건 더 쌓임" in conv["messages"][1]["content"]
        m0 = next(m for m in conv["messages"] if a0["name"] in m["content"])
        kinds = sorted(x["type"] for x in m0.get("trade_actions", []))
        assert kinds == ["arena_undo", "set"], kinds
        assert m0.get("improvements") == ["job1"]
        assert "✅ 스스로 바꿈" in m0["content"], m0["content"]
        m1 = next(m for m in conv["messages"] if a1["name"] in m["content"])
        assert not m1.get("trade_actions"), ("실전 켜기 제안이 카드로 나옴", m1)
        # 그 AI 자기 규칙만 바로 바뀜 (범위 밖 손절 999 는 걸러짐) · 공통 설정은 [적용] 전까지 그대로
        a0n = arena.load("US")["alive"][0]
        assert a0n["tp"] == 6 and a0n["sl"] == a0["sl"] and a0n["habits"] == ["하락장 장 초반엔 안 삼"], a0n
        assert arena.load("US")["alive"][1]["tp"] == a1["tp"], "다른 AI 규칙이 바뀜"
        assert at.config()["stop_loss_pct"] == stop0
        # [되돌리기] → 원래대로 · 두 번은 안 됨
        undo = next(x for x in m0["trade_actions"] if x["type"] == "arena_undo")
        assert at.apply_action(undo)["ok"]
        a0u = arena.load("US")["alive"][0]
        assert a0u["tp"] == a0["tp"] and not a0u.get("habits"), a0u
        assert not at.apply_action(undo)["ok"], "되돌리기가 두 번 됨"
        # 그 뒤에 또 바뀌었으면 예전 되돌리기는 안 함 · 범위 밖 값은 안 바뀜
        r1 = arena.set_rules("US", a0["id"], {"tp": 7}); arena.set_rules("US", a0["id"], {"tp": 8})
        assert not arena.undo_rules("US", a0["id"], r1["t"])["ok"] and arena.load("US")["alive"][0]["tp"] == 8
        assert not arena.set_rules("US", a0["id"], {"hold": 99999})["ok"], "범위 밖 값이 적용됨"
    finally:
        ag.SESSIONS_DIR, im.create, arena_voice._file = old_sd, old_create, old_vf


@case
def 다들_하고_물으면_리그_AI_가_각자_자기_이름으로_답함(at, picker, tmp):
    fresh(at, picker, tmp, arena_size=2)
    from engine import ai_ask
    d = _arena_fill([1.5, -0.8], ctrl_ret=0.2)
    a0, a1 = d["alive"][0]["name"], d["alive"][1]["name"]
    who = ai_ask.match("다들 오늘 왜 손해 봤어?")
    assert who and who["kind"] == "all", who
    assert ai_ask.match("리그 전원 오늘 어땠어?")["kind"] == "all"
    assert ai_ask.match("모두 팔아줘") is None and ai_ask.match("전원 꺼") is None, "매매 지시가 리그 질문으로 감"
    assert ai_ask.match(f"{a0}, 왜 샀어?")["kind"] == "arena"   # 이름 부르면 그 AI 만
    assert ai_ask.match(f"@{a0}")["name"] == a0, "이름만 불렀을 때 엔진 AI 가 대신 답함"
    assert a0 in ai_ask.system_prompt(who) and a1 in ai_ask.system_prompt(who)
    reply = f'답입니다 {{"{a0}": "저는 장 초반에 너무 빨리 샀어요", "{a1}": "", "없는AI": "x"}}'
    assert ai_ask.split_answers(reply, "US") == [(a0, "저는 장 초반에 너무 빨리 샀어요")]   # 빈 답·모르는 이름은 뺌
    assert ai_ask.split_answers("JSON 아님", "US") == []


@case
def 채팅_답변_중에_온_생존전_의견이_안_사라짐(at, picker, tmp):
    fresh(at, picker, tmp, arena_size=2)
    from engine import agents as ag, arena_voice, web
    d = _arena_fill([1.0, 1.0], ctrl_ret=0.0)
    old_sd, old_vf = ag.SESSIONS_DIR, arena_voice._file
    ag.SESSIONS_DIR = tmp / "sessions2"
    arena_voice._file = lambda: tmp / "arena_voice2.json"
    try:
        ok = lambda p: {d["alive"][0]["id"]: {"opinion": "첫 의견"}}
        assert arena_voice.ask(at.config(), "US", {d["alive"][0]["id"]: "x"}, ai_fn=ok) == 1
        cid = json.loads((tmp / "arena_voice2.json").read_text(encoding="utf-8"))["conv"]["US"]
        # 사장님이 그 대화에서 질문 → (채팅 처리와 같은 순서) 질문 저장
        conv = web.load_conv(cid)
        base = len(conv["messages"])
        conv["messages"].append({"role": "user", "content": "왜?"})
        base = web.save_conv_merged(conv, base)
        # 답변 만드는 사이에 다른 AI 의견 도착
        assert arena_voice.ask(at.config(), "US", {d["alive"][1]["id"]: "y"}, ai_fn=lambda p: {d["alive"][1]["id"]: {"opinion": "끼어든 의견"}}) == 1
        # 답변 끝 → 저장 (예전엔 여기서 끼어든 의견을 덮어씀)
        conv["messages"].append({"role": "assistant", "content": "답변"})
        web.save_conv_merged(conv, base)
        texts = [m["content"] for m in web.load_conv(cid)["messages"]]
        assert any("끼어든 의견" in t for t in texts), ("답변 저장이 의견을 덮어씀", texts)
        assert texts[-1] == "답변" and any(t == "왜?" for t in texts), texts
    finally:
        ag.SESSIONS_DIR, arena_voice._file = old_sd, old_vf


@case
def 제안_현황은_대기_적용_바꾼_뒤_성적을_모으고_하루_한_번만_보내고_노트는_안_건드림(at, picker, tmp):
    fresh(at, picker, tmp)
    from engine import agents as ag, digest, improve as im, notify, tradelearn as tl, web
    old = (ag.SESSIONS_DIR, im.HISTORY, digest.SENT, notify.send)
    sent = []
    ag.SESSIONS_DIR, im.HISTORY, digest.SENT = tmp / "sessions4", tmp / "imp.json", tmp / "sent.json"
    notify.send = lambda t: sent.append(t)
    try:
        conv = web.new_conv("시험 대화"); conv["id"] += "-dg"
        conv["messages"].append({"role": "assistant", "content": "x", "trade_actions": [
            {"id": "a1", "type": "set", "label": "손절(%): 4 → 5"}, {"id": "a2", "type": "set", "label": "끝난 카드", "status": "applied"}]})
        web.save_conv(conv)
        im.create("상황별 성적 차트", "시험", "trader")
        n_lessons = len(tl.lessons("US", 80))
        assert at.apply_action({"type": "set", "param": "stop_loss_pct", "value": "5", "label": "손절(%): 4 → 5"})["ok"]   # 사장님이 카드 적용
        d = digest.collect("US")
        assert [x["label"] for x in d["pending"]["cards"]] == ["손절(%): 4 → 5"], d["pending"]["cards"]
        assert len(d["pending"]["code"]) == 1
        assert any("사장님 적용" in x and "손절" in x for x in d["applied"]), d["applied"]
        t = digest.text("US", d)
        assert "대기 2개" in t and "손절(%): 4 → 5" in t, t
        digest.send("US"); digest.send("US")
        assert len(sent) == 1, "하루 두 번 보냄"
        assert len(tl.lessons("US", 80)) == n_lessons, "요약이 공유 노트에 들어감"
    finally:
        ag.SESSIONS_DIR, im.HISTORY, digest.SENT, notify.send = old


@case
def 자동_개선은_멈춘_작업을_정리하고_성공만_세고_장중엔_시작_안_함(at, picker, tmp):
    fresh(at, picker, tmp)
    from datetime import datetime, timedelta
    from engine import improve as im, gitsync
    old_git = gitsync.enabled
    gitsync.enabled = lambda: False   # 시험에서 GitHub 에 올리지 않게
    old = (im.HISTORY, im.AUTO_FILE, im.VERIFY, im.run, im.apply, im.market_busy, im._notify)
    im.HISTORY, im.AUTO_FILE, im.VERIFY = tmp / "imp2.json", tmp / "auto.json", tmp / "verify.json"
    im._notify = lambda text, log: None
    now = datetime.now()
    iso = lambda dt: dt.isoformat(timespec="seconds")
    try:
        im._save([{"id": "old", "status": "working", "time": iso(now - timedelta(days=9)), "request": "옛 작업"},
                  {"id": "live", "status": "working", "time": iso(now), "started": iso(now), "request": "지금 작업"}])
        assert im.clear_stale() == ["old"]
        assert im.get("old")["status"] == "failed" and im.get("live")["status"] == "working"
        assert im.auto_once(print, lambda: None) == "다른 작업 중"   # 진짜 돌고 있는 건 안 건드림
        im._update("live", status="applied")
        # 검사 실패는 성공 횟수에 안 셈 → 한도에 안 걸림
        im._save(im._load() + [{"id": f"p{i}", "status": "proposed", "time": iso(now), "request": f"제안 {i}"} for i in range(8)])
        im.run = lambda jid: (im._update(jid, status="blocked"), {"ok": False, "checks": [{"name": "검사", "ok": False}]})[1]
        im.market_busy = lambda now=None: []
        for _ in range(im.AUTO_DAILY_MAX):
            assert im.auto_once(print, lambda: None) == "검사 실패"
        assert im._today_auto() == 0 and im._today_auto("auto_at") == im.AUTO_DAILY_MAX
        im.run = lambda jid: (im._update(jid, status="ready"), {"ok": True, "files": [], "checks": []})[1]
        im.apply = lambda jid: (im._update(jid, status="applied"), {"ok": True})[1]
        assert im.auto_once(print, lambda: None) == "적용" and im._today_auto() == 1
        im.market_busy = lambda now=None: ["KR"]
        assert im.auto_once(print, lambda: None) == "장 중"
        old_sm = picker.session_minutes   # 시험 환경은 장 시각을 바꿔 둬서 직접 흉내
        picker.session_minutes = lambda m, now=None, extended=False: 60 if m == "KR" else None
        try:
            assert old[5]() == ["KR"]
        finally:
            picker.session_minutes = old_sm
    finally:
        im.HISTORY, im.AUTO_FILE, im.VERIFY, im.run, im.apply, im.market_busy, im._notify = old
        gitsync.enabled = old_git


@case
def 큰_기록을_쓰는_동안에도_다른_기록은_안_기다리고_옛_내용이_새_내용을_안_덮음(at, picker, tmp):
    from engine import store
    big, small = tmp / "big.json", tmp / "small.json"
    orig = store.write_text
    gate, entered = threading.Event(), threading.Event()

    def slow(path, text):
        if path == big and '"v":1' in text:   # 첫 큰 저장만 멈춰 둠
            entered.set(); gate.wait(5)
        orig(path, text)
    store.write_text = slow
    try:
        store.save(big, {"v": 0})
        th = threading.Thread(target=store.save, args=(big, {"v": 1})); th.start()
        assert entered.wait(5)
        t0 = time.time()
        store.save(small, {"x": 1})                      # 다른 파일은 안 기다림
        assert time.time() - t0 < 1, "큰 파일 쓰는 동안 다른 기록 저장이 막힘"
        assert store.load(big)["v"] == 1, "쓰는 중엔 메모리의 새 내용을 줘야 함"
        th2 = threading.Thread(target=store.save, args=(big, {"v": 2})); th2.start()
        time.sleep(0.2)
        gate.set(); th.join(5); th2.join(5)
        assert json.loads(big.read_text(encoding="utf-8"))["v"] == 2, "옛 내용이 새 내용을 덮음"
        assert store.load(big)["v"] == 2
    finally:
        store.write_text = orig
        gate.set()


@case
def 노트_정리_회의는_서로_의견을_주고받고_다수가_동의한_것만_핵심_노트로(at, picker, tmp):
    fresh(at, picker, tmp)
    from engine import agents as ag, notes_council as nc, tradelearn as tl, web
    for i in range(8):
        tl.add_lesson("US", f"교훈 {i}: 기대값 마이너스면 진입 보류", "시험")
    calls = []

    def fake(role, prompt):
        calls.append(role)
        if role == "editor" and "반대 의견" not in prompt:   # ① 초안
            return {"notes": [{"id": "N1", "text": "기대값 마이너스면 진입 보류", "from": ["L1", "L2", "L3"], "evidence": "음수 기대 20건 평균 -2.1%"},
                              {"id": "N2", "text": "느낌 좋으면 크게 산다", "from": ["L4"]},
                              {"id": "N3", "text": "장 초반 30분은 금액 절반", "from": ["L5"]}], "drop": [{"from": ["L6"], "why": "중복"}], "comment": "초안"}
        if role == "editor":   # ③ 반대 반영 — N3 를 구체적으로 고침
            assert "느낌 좋으면" in prompt and "근거 없음" in prompt, "정리 담당이 반대 의견을 못 받음"
            return {"notes": [{"id": "N1", "text": "기대값 마이너스면 진입 보류", "from": ["L1"]},
                              {"id": "N2", "text": "느낌 좋으면 크게 산다", "from": ["L4"]},
                              {"id": "N3", "text": "정규장 시작 30분은 금액 50%", "from": ["L5"]}], "comment": "N3 고침, N2 는 유지"}
        revised = "정규장 시작 30분" in prompt
        if role == "skeptic":
            return {"votes": {"N1": {"ok": True, "why": "숫자 있음"}, "N2": {"ok": False, "why": "근거 없음"},
                              "N3": {"ok": revised, "why": "구간 애매" if not revised else "좋음", "fix": "정규장 시작 30분으로"}}, "comment": "N2 반대"}
        if role == "trader":
            if not revised:
                assert "🔍 검증 담당 의견" in prompt, "현장 담당이 검증 담당 말을 못 봄 (대화 아님)"
            return {"votes": {"N1": {"ok": True}, "N2": {"ok": False, "why": "실행 불가"}, "N3": {"ok": revised}}, "comment": "동의"}

    old_sd = ag.SESSIONS_DIR
    ag.SESSIONS_DIR = tmp / "sessions3"
    try:
        r = nc.run_and_post(at.config(), "US", ai_fn=fake)
        assert r["ok"], r
        assert calls.count("editor") == 2 and calls.count("skeptic") == 2, calls   # 초안 → 투표 → 고침 → 다시 투표
        texts = [n["text"] for n in r["agreed"]]
        assert texts == ["기대값 마이너스면 진입 보류", "정규장 시작 30분은 금액 50%"], texts
        assert [n["text"] for n in r["split"]] == ["느낌 좋으면 크게 산다"], "둘 다 반대한 건 핵심 노트에서 빠져야 함"
        notes = tl.lessons_text("US", 6)
        assert "정규장 시작 30분" in notes and "느낌 좋으면" not in notes, notes
        assert len(tl.lessons("US", 80)) == 8, "원본 교훈이 지워짐"
        conv = web.load_conv(r["conversation_id"])
        assert len(conv["messages"]) >= 6, len(conv["messages"])   # 안내 + 초안 + 투표 2 + 고침 + 투표 2 + 결과
        card = conv["messages"][-1]["trade_actions"][0]
        assert card["type"] == "notes_undo"
        assert at.apply_action(card)["ok"] and not tl.core("US"), "되돌리기가 안 됨"
    finally:
        ag.SESSIONS_DIR = old_sd
    # 교훈이 적으면 회의 안 함
    fresh(at, picker, tmp)
    tl.add_lesson("US", "하나뿐", "시험")
    assert not nc.run(at.config(), "US", ai_fn=fake)["ok"]


@case
def AI_리그는_안_산_판단까지_채점하고_계속_지는_구간은_안_물어봄(at, picker, tmp):
    fresh(at, picker, tmp, arena_size=2)
    from engine import arena
    c = {**at.config(), "arena_on": True}
    d = arena.load("US", 2)
    a0, a1 = d["alive"][0]["id"], d["alive"][1]["id"]
    d["alive"][0]["tp"] = d["alive"][1]["tp"] = 4.0
    arena._save("US", d)
    cd = {"style": "surge", "reason": "t", "features": {"change": 45, "ratio": 12, "pre": True}}
    prompts = []
    ai = lambda p: (prompts.append(p), {a0: True, a1: False})[1]
    # ① 채점: a0 '산다' · a1 '안 산다' → 둘 다 '샀다면' 으로 계산 → +5% 가면 a1 은 놓친 기회
    arena._decide(c, "US", "ZZ", cd, 100.0, "D1", ai)
    assert len(arena.load("US")["probes"]) == 1
    assert prompts[-1].count("🔰 기록 적음 (거래 0건)") == 2, "거래 적은 AI 에게 가상으로 사 보라는 표시가 없음"
    arena.step(c, "US", {}, {"ZZ": {"price": 105.0}}, 30, "D1")
    d = arena.load("US")
    assert not d["probes"] and len(d["graded"]) == 1, (d.get("probes"), d.get("graded"))
    g = d["graded"][0]
    assert g["votes"][a0] is True and g["votes"][a1] is False and set(g["res"]) >= {a0, a1, "ctrl"}, g
    idx = arena._grade_index(d)
    assert arena.card(idx[a1])["missed"] == 1, "안 산 것 중 오른 걸 놓친 기회로 못 셈"
    assert "상승 40%+" in g["keys"] and "프리장" in g["keys"], g["keys"]
    # ② 거름망: a0 가 '상승 40%+' 에서 '산다' 12번 다 -2% → 그 구간 후보는 a0 에게 안 물어봄
    bad = [{"sym": f"B{i}", "day": "D0", "t": "", "keys": ["상승 40%+"], "votes": {a0: True, a1: False}, "res": {a0: -2.0, a1: -2.0, "ctrl": -2.0}} for i in range(12)]
    d["graded"] = bad + d["graded"]; arena._save("US", d)
    old_ex = arena.EXPLORE; arena.EXPLORE = 0.0
    try:
        prompts.clear()
        arena._decide(c, "US", "YY", cd, 100.0, "D1", ai)
        assert prompts and a0 not in prompts[-1] and a1 in prompts[-1], "걸러진 AI 에게 또 물어봄"
        assert "내 기록:" in prompts[-1], "판단할 때 자기 기록을 안 보여 줌"
        assert arena.load("US")["votes"]["D1"]["YY"] == [], "걸러진 AI 가 산 걸로 됨"
        # 둘 다 걸러지면 AI 를 아예 안 부름
        d = arena.load("US")
        for x in d["graded"][:12]:
            x["votes"][a1] = True
        arena._save("US", d)
        prompts.clear()
        arena._decide(c, "US", "XX", cd, 100.0, "D1", ai)
        assert not prompts, "다 걸러졌는데 AI 를 부름"
        assert arena.load("US")["saved"]["calls"] == 1
        # ③ 거름망 풀기: 그 구간 후보가 최근 (내 규칙으로 샀다면) 플러스로 바뀌면 다시 물어봄
        d = arena.load("US")
        d["graded"] += [{"sym": f"G{i}", "day": "D1", "t": "", "keys": ["상승 40%+"], "votes": {a0: None, a1: None}, "res": {a0: 3.0, a1: 3.0, "ctrl": 3.0}} for i in range(15)]
        arena._save("US", d)
        prompts.clear()
        arena._decide(c, "US", "WW", cd, 100.0, "D1", ai)
        assert prompts and a0 in prompts[-1], "시장이 바뀌었는데 거름망이 안 풀림"
        assert any(r.get("blocks") == [] for r in arena.board("US", c) if not r["ctrl"])
    finally:
        arena.EXPLORE = old_ex


@case
def 생존전_AI_id_는_안_겹치고_예전에_겹친_것도_고쳐짐(at, picker, tmp):
    fresh(at, picker, tmp, arena_size=10)
    from engine import arena, store
    for _ in range(5):   # 같은 순간에 10명 만들어도 안 겹침
        (tmp / "arena_US.json").unlink(missing_ok=True); store._cache.clear()
        ids = [a["id"] for a in arena.load("US", 10)["alive"]]
        assert len(set(ids)) == 10, ids
    d = arena.load("US", 10)
    d["alive"][8]["id"] = d["alive"][7]["id"]; d["alive"][8]["trades"] = [{"ret": 1.0}]
    arena._save("US", d); store._cache.clear()
    d2 = arena.load("US")
    ids = [a["id"] for a in d2["alive"]]
    assert len(set(ids)) == 10, ("겹친 id 가 안 고쳐짐", ids)
    assert d2["alive"][8]["trades"] == [{"ret": 1.0}], "고치면서 거래 기록이 사라짐"
    store._cache.clear()
    assert [a["id"] for a in arena.load("US")["alive"]] == ids, "다시 읽으니 id 가 또 바뀜"


@case
def 생존전_AI_는_거래가_쌓이거나_연속_손실일_때만_하루_1번_물어봄(at, picker, tmp):
    fresh(at, picker, tmp, arena_size=2)
    from engine import arena
    c = {**at.config(), "arena_on": True, "arena_voice": True, "arena_voice_every": 5}
    d = arena.load("US", 2)
    t = lambda r: {"sym": "X", "day": "D", "ret": r, "why": "t", "mins": 5, "t": ""}
    d["alive"][0]["trades"] = [t(1.0)] * 6                  # 6건 쌓임 → 물어봄
    d["alive"][1]["trades"] = [t(1.0), t(1.0), t(-1.0), t(-1.0)]   # 4건 → 아직
    due = arena.voice_due(c, d, "D1")
    assert list(due) == [d["alive"][0]["id"]], due
    assert arena.voice_due(c, d, "D1") == {}, "같은 날 또 물어봄"
    d["alive"][1]["trades"].append(t(-2.0))                 # 5건째 · 3연속 손실 → 바로
    due = arena.voice_due(c, d, "D2")
    assert d["alive"][1]["id"] in due and "연속 손실" in due[d["alive"][1]["id"]], due
    assert d["alive"][0]["id"] not in due, "새 거래 없는데 또 물어봄"
    assert arena.voice_due({**c, "arena_voice": False}, d, "D3") == {}, "꺼도 물어봄"


@case
def 지는_구간이면_실전_대신_가상으로(at, picker, tmp):
    fresh(at, picker, tmp)
    from engine import edge, store as st_
    keys = ["상승률 ~10%", "상승률 10~20%", "상승률 20~30%", "상승률 30%+", "거래량 ~3배", "거래량 3~5배", "거래량 5~10배", "거래량 10배+",
            "시간 프리장", "시간 장 초반 30분", "시간 30~120분", "시간 120분 뒤"]
    d = edge.load("US"); d["blocked"] = {k: {"n": 25, "avg": -1.0} for k in keys}; st_.save(edge._file("US"), d)
    r = at.run_once()
    assert posted("BUY") == [], ("지는 구간인데 실전 주문이 나감", posted("BUY"))
    assert at.state()["paper"]["positions"], ("가상으로도 안 삼", r["results"][:8])


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
