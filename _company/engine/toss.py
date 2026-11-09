"""토스증권 오픈API — 시세·일봉·종목 정보·내 잔고 + (자동매매용) 주문·취소.

.env 에 TOSS_CLIENT_ID, TOSS_CLIENT_SECRET 이 있으면 동작합니다 (🧰 도구 화면에서 입력).
💰 돈은 계좌 밖으로 못 나가요:
   - 토스 API 자체에 출금·이체·송금 기능이 없어요 (쓰기 기능은 주문 생성·정정·취소뿐)
   - 이 앱은 그중에서도 '지정가 주문'과 '취소'만 보낼 수 있게 허용 목록으로 막아요 (정정도 안 씀)
   - 주문은 autotrade.py 의 안전장치(한도·종목 제한·긴급 정지)를 거친 것만 들어와요
   토스 API 에는 모의투자 환경이 없어서 주문이 곧 실제 돈이에요.

문서: https://developers.tossinvest.com (openapi 1.1.1 기준)
- 토큰은 한 번에 하나만 유효 (새로 받으면 이전 것 즉시 무효) → 파일에 보관해 재사용
- 실시간 스트리밍(웹소켓)은 아직 없음 → 필요할 때마다 현재가 조회
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .agents import COMPANY_DIR

BASE = "https://openapi.tossinvest.com"
TOKEN_CACHE = COMPANY_DIR / "_cache" / "toss_token.json"   # _cache/ 는 git 에 안 올라감
ALLOWED = re.compile(r"^/api/v1/(prices|candles|stocks|orderbook|trades|price-limits|accounts|holdings|buying-power|"
                     r"sellable-quantity|commissions|orders|orders/[A-Za-z0-9\-_]+|"
                     r"exchange-rate|market-calendar/(KR|US)|stocks/[A-Za-z0-9.\-]+/warnings)$")
WRITE_ALLOWED = re.compile(r"^/api/v1/orders(/[A-Za-z0-9\-_]+/cancel)?$")   # 주문 생성·취소만. 그 외 쓰기는 전부 차단
_lock = threading.Lock()
_mem: dict[str, tuple[float, object]] = {}


class TossError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(os.getenv("TOSS_CLIENT_ID", "").strip() and os.getenv("TOSS_CLIENT_SECRET", "").strip())


def _http(req: urllib.request.Request, timeout: int = 15) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except Exception:
            body = {}
        err = body.get("error")
        msg = (err.get("message") if isinstance(err, dict) else body.get("error_description") or err) or ""
        if e.code == 429:
            raise TossError(f"토스: 요청이 너무 잦아요 ({e.headers.get('Retry-After', '1')}초 뒤 다시)") from None
        raise TossError(f"토스 {e.code}: {msg or e.reason}") from None
    except Exception as e:  # 네트워크 오류 — 주소·키가 메시지에 섞이지 않게
        raise TossError(f"토스 접속 실패: {type(e).__name__}") from None


def _issue(cid: str, secret: str) -> dict:
    data = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cid, "client_secret": secret}).encode()
    req = urllib.request.Request(BASE + "/oauth2/token", data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    return _http(req)


def _token(force: bool = False) -> str:
    with _lock:
        if not force and TOKEN_CACHE.is_file():
            try:
                c = json.loads(TOKEN_CACHE.read_text())
                if c.get("exp", 0) - 60 > time.time() and c.get("cid") == os.getenv("TOSS_CLIENT_ID", "").strip():
                    return c["token"]
            except Exception:
                pass
        cid, secret = os.getenv("TOSS_CLIENT_ID", "").strip(), os.getenv("TOSS_CLIENT_SECRET", "").strip()
        if not (cid and secret):
            raise TossError("토스 키가 없어요 (🧰 도구 화면에서 클라이언트 ID·시크릿 키 입력)")
        try:
            d = _issue(cid, secret)
        except TossError as e:
            if " 401" not in f" {e}" and "400" not in str(e):
                raise
            d = _issue(secret, cid)  # 두 칸을 바꿔 넣은 경우
        tok = d.get("access_token")
        if not tok:
            raise TossError("토스 토큰을 못 받았어요 (키를 다시 확인해 주세요)")
        TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE.write_text(json.dumps({"token": tok, "exp": time.time() + int(d.get("expires_in", 3600)), "cid": cid}))
        return tok


def _get(path: str, account: bool = False, **params) -> object:
    """조회(GET)만. 허용 목록에 없는 주소(주문 등)는 보내기 전에 막음."""
    if not ALLOWED.match(path):
        raise TossError(f"이 앱은 조회만 해요 — 막힌 주소: {path}")
    q = {k: v for k, v in params.items() if v is not None}
    url = BASE + path + ("?" + urllib.parse.urlencode(q) if q else "")
    for attempt in (0, 1):
        headers = {"Authorization": "Bearer " + _token(force=attempt == 1), "Accept": "application/json"}
        if account:
            headers["X-Tossinvest-Account"] = str(account_seq())
        try:
            return _http(urllib.request.Request(url, headers=headers, method="GET")).get("result")
        except TossError as e:
            if "401" in str(e) and attempt == 0:  # 다른 곳에서 토큰을 새로 받아 무효가 됨 → 한 번만 재발급
                continue
            raise


def _post(path: str, body: dict) -> object:
    """쓰기는 주문 생성·취소 두 가지만."""
    if not WRITE_ALLOWED.match(path):
        raise TossError(f"허용되지 않은 쓰기 요청이라 막았어요: {path}")
    data = json.dumps(body).encode()
    for attempt in (0, 1):
        headers = {"Authorization": "Bearer " + _token(force=attempt == 1), "Content-Type": "application/json",
                   "Accept": "application/json", "X-Tossinvest-Account": str(account_seq())}
        try:
            return _http(urllib.request.Request(BASE + path, data=data, headers=headers, method="POST")).get("result")
        except TossError as e:
            if "401" in str(e) and attempt == 0:
                continue
            raise


def kr_tick(price: float) -> int:
    """국내 주식 호가 단위 (코스피·코스닥 공통, 2023~)"""
    for limit, tick in ((2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500)):
        if price < limit:
            return tick
    return 1000


def order_price(sym: str, price: float, side: str) -> str:
    """호가 단위에 맞춘 지정가 문자열. 사는 건 내림, 파는 건 올림 (불리한 가격에 체결 안 되게)."""
    import math
    if re.fullmatch(r"\d{6}", symbol(sym)):
        t = kr_tick(price)
        v = (math.floor if side == "BUY" else math.ceil)(price / t) * t
        return str(int(v))
    q = 100 if price >= 1 else 10000
    return f"{(math.floor if side == 'BUY' else math.ceil)(price * q) / q:.{2 if q == 100 else 4}f}"


def place_limit_order(sym: str, side: str, quantity: int, price: float, client_order_id: str) -> dict:
    """지정가 주문 한 건. 호출은 autotrade.py 의 안전장치를 통과한 경우에만."""
    if side not in ("BUY", "SELL") or int(quantity) < 1:
        raise TossError("주문 값이 이상해요")
    body = {"symbol": symbol(sym), "side": side, "orderType": "LIMIT", "quantity": str(int(quantity)),
            "price": order_price(sym, price, side), "timeInForce": "DAY", "clientOrderId": client_order_id[:36]}
    return _post("/api/v1/orders", body) or {}


def cancel_order(order_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9\-_]+", order_id):
        raise TossError("주문번호가 이상해요")
    return _post(f"/api/v1/orders/{order_id}/cancel", {}) or {}


def open_orders() -> list[dict]:
    return (_get("/api/v1/orders", account=True, status="OPEN") or {}).get("orders") or []


def get_order(order_id: str) -> dict:
    return _get(f"/api/v1/orders/{order_id}", account=True) or {}


def _cached(key: str, ttl: float, fn):
    hit = _mem.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    v = fn()
    _mem[key] = (time.time(), v)
    return v


def symbol(ticker: str) -> str:
    """'005930.KS' → '005930', 'AAPL' → 'AAPL'"""
    t = ticker.strip().upper()
    return t.split(".")[0] if re.fullmatch(r"\d{6}(\.(KS|KQ))?", t) else t


# ── 시세 ──
def prices(symbols: list[str]) -> dict[str, dict]:
    res = _get("/api/v1/prices", symbols=",".join(symbol(s) for s in symbols)) or []
    return {r["symbol"]: {"price": float(r["lastPrice"]), "currency": r.get("currency"), "time": r.get("timestamp")} for r in res}


def daily(sym: str, days: int = 260) -> tuple[list[float], list[float], list[str]]:
    """일봉 (오래된 것 → 최신). 200개씩 나눠 받음."""
    def fetch():
        closes, vols, dates, before = [], [], [], None
        while len(closes) < days:
            r = _get("/api/v1/candles", symbol=symbol(sym), interval="1d", count=min(200, days - len(closes)), before=before) or {}
            cs = r.get("candles") or []
            for c in cs:
                closes.append(float(c["closePrice"])); vols.append(float(c.get("volume") or 0)); dates.append(c["timestamp"])
            before = r.get("nextBefore")
            if not cs or not before:
                break
        order = sorted(range(len(dates)), key=lambda i: dates[i])
        return [closes[i] for i in order], [vols[i] for i in order], [dates[i] for i in order]
    return _cached(f"d:{symbol(sym)}:{days}", 300, fetch)


def stock_info(sym: str) -> dict:
    def fetch():
        r = _get("/api/v1/stocks", symbols=symbol(sym)) or []
        return r[0] if r else {}
    return _cached("s:" + symbol(sym), 86400, fetch)


# ── 내 계좌 (조회만) ──
def account_seq() -> int:
    def fetch():
        accs = [a for a in (_get("/api/v1/accounts") or []) if a.get("accountType") == "BROKERAGE"]
        if not accs:
            raise TossError("토스 종합계좌를 못 찾았어요")
        return int(accs[0]["accountSeq"])
    return _cached("acc", 3600, fetch)


def holdings() -> dict:
    return _get("/api/v1/holdings", account=True) or {}


def buying_power(currency: str = "KRW") -> dict:
    return _get("/api/v1/buying-power", account=True, currency=currency) or {}


def _won(v) -> str:
    """금액 표시. Price 객체({krw, usd}) 나 {amount: Price} 도 받음."""
    if isinstance(v, dict):
        if "amount" in v:
            return _won(v["amount"])
        parts = []
        if v.get("krw") not in (None, "0", 0) or v.get("usd") in (None,):
            parts.append(f"{_won(v.get('krw'))}원")
        if v.get("usd") is not None:
            parts.append(f"${float(v['usd']):,.2f}")
        return " + ".join(parts) if parts else "0원"
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def _pct(v) -> str:
    try:
        return f"{float(v) * 100:+.2f}%"
    except (TypeError, ValueError):
        return ""


def portfolio_for_prompt() -> str:
    """보유 종목·평가손익·주문가능 현금 요약 (AI 에게 붙여줄 글)."""
    h = holdings()
    lines = ["<my_account>", "토스증권에서 방금 조회한 사장님 실제 계좌예요. 채팅에서는 주문하지 않아요 (주문은 🤖 자동매매 화면의 규칙으로만). 계좌번호는 말하지 마세요."]
    if h:
        pl, dpl = h.get("profitLoss") or {}, h.get("dailyProfitLoss") or {}
        lines.append(f"총 매수금액 {_won(h.get('totalPurchaseAmount'))} · 평가금액 {_won(h.get('marketValue'))} · "
                     f"평가손익 {_won(pl)} ({_pct(pl.get('rate'))}) · 오늘 손익 {_won(dpl)} ({_pct(dpl.get('rate'))})")
        for it in h.get("items") or []:
            ipl = it.get("profitLoss") or {}
            lines.append(f"- {it.get('name') or it.get('symbol')} ({it.get('symbol')}): {it.get('quantity')}주 · 평단 {it.get('averagePurchasePrice')} · "
                         f"현재가 {it.get('lastPrice')} {it.get('currency', '')} · 평가손익 {_won(ipl.get('amount') if isinstance(ipl, dict) else ipl)} {_pct(ipl.get('rate')) if isinstance(ipl, dict) else ''}")
        if not h.get("items"):
            lines.append("- 보유 종목 없음")
    for cur in ("KRW", "USD"):
        try:
            bp = buying_power(cur)
            if bp:
                lines.append(f"주문 가능 현금({cur}): {_won(bp.get('cashBuyingPower'))}")
        except TossError:
            pass
    lines.append("</my_account>")
    return "\n".join(lines)


ACCOUNT_WORDS = re.compile(r"내\s*(계좌|잔고|주식|종목|포트|보유)|보유\s*(종목|주식)|잔고|포트폴리오|평단|수익률|손익|물렸|매수가")
