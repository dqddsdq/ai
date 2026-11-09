"""자동매매용 종목 고르기 — 하루 한 번 국내 시장 전체를 훑어서 '지금 상승 추세가 가장 강한' 종목 몇 개.

순서 (토스증권 오픈API + DART 회사 목록)
1. DART 상장사 목록 → 6자리 종목코드 전부
2. 토스 종목 정보로 걸러냄: 코스피·코스닥, 정상 거래, 보통주만 (우선주·레버리지·ETF/ETN 등 제외)
3. 현재가: 1,000원 이상이고 '한 번 주문 최대 금액'으로 1주는 살 수 있는 가격
4. 일봉 70일: 하루 평균 거래대금(유동성) 기준 이상, 현재가 > 20일선 > 60일선, RSI 45~과열 기준
5. 점수 = 20일 수익률 + 60일 수익률의 절반 (변동성이 너무 크면 감점) → 상위 몇 개
6. 매수 경고(투자주의·경고·위험 등)가 걸린 종목은 마지막에 뺌

토스 API 는 종목 검색 기능이 없어서 DART 목록을 씀 (DART 키 필요).
처음 훑을 때 몇 분 걸려요 (초당 7~8건으로 천천히 → 한도 초과 방지).
"""

from __future__ import annotations

import math
from datetime import datetime

from . import dart, toss

COMMON_MARKETS = {"KOSPI", "KOSDAQ"}


def _chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _rsi(xs: list[float], n: int = 14) -> float | None:
    if len(xs) <= n:
        return None
    g = l = 0.0
    for a, b in zip(xs[-n - 1:-1], xs[-n:]):
        g += max(b - a, 0); l += max(a - b, 0)
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def _is_plain_stock(info: dict) -> bool:
    lev = info.get("leverageFactor")
    try:
        lev_ok = lev in (None, "", 1, "1") or float(lev) == 1
    except (TypeError, ValueError):
        lev_ok = False
    return (info.get("market") in COMMON_MARKETS and info.get("status") == "ACTIVE"
            and info.get("isCommonShare") is not False and lev_ok
            and str(info.get("securityType") or "STOCK").upper() in ("STOCK", "COMMON", "COMMON_STOCK", "EQUITY", ""))


def scan(cfg: dict, log=print, exclude: set[str] | None = None) -> dict:
    """{'day', 'picks': [{symbol, name, price, score, reason}], 'scanned', 'note'}"""
    exclude = exclude or set()
    if not dart.enabled():
        return {"day": "", "picks": [], "scanned": 0, "note": "자동 종목 고르기는 DART 키가 필요해요 (🧰 도구)"}
    codes = sorted(dart.corps()["by_stock"])
    log(f"🔎 종목 훑기 시작: 상장사 {len(codes)}개")

    infos: dict[str, dict] = {}
    for chunk in _chunks(codes, 200):
        try:
            for it in toss._get("/api/v1/stocks", symbols=",".join(chunk)) or []:
                infos[it["symbol"]] = it
        except toss.TossError as e:
            log(f"   종목 정보 일부 실패: {e}")
    plain = [s for s, i in infos.items() if _is_plain_stock(i) and s not in exclude]

    max_price = cfg["max_order_krw"]
    prices: dict[str, float] = {}
    for chunk in _chunks(plain, 200):
        try:
            prices.update({k: v["price"] for k, v in toss.prices(chunk).items()})
        except toss.TossError as e:
            log(f"   현재가 일부 실패: {e}")
    afford = [s for s in plain if 1000 <= prices.get(s, 0) <= max_price]
    log(f"   보통주 {len(plain)}개 → 1주 살 수 있는 가격 {len(afford)}개 → 차트 확인 중 (몇 분 걸려요)")

    min_value = cfg.get("min_trading_value_krw", 1_000_000_000)
    scored = []
    for i, sym in enumerate(afford):
        if i and i % 200 == 0:
            log(f"   차트 확인 {i}/{len(afford)}")
        try:
            r = toss._get("/api/v1/candles", symbol=sym, interval="1d", count=70) or {}
        except toss.TossError:
            continue
        cs = sorted(r.get("candles") or [], key=lambda c: c["timestamp"])
        if len(cs) < 61:
            continue
        closes = [float(c["closePrice"]) for c in cs]
        vols = [float(c.get("volume") or 0) for c in cs]
        price = prices[sym]
        series = closes[:-1] + [price] if cs[-1]["timestamp"][:10] == datetime.now().strftime("%Y-%m-%d") else closes + [price]
        value20 = sum(c * v for c, v in zip(closes[-20:], vols[-20:])) / 20
        if value20 < min_value:
            continue
        ma20, ma60 = sum(series[-20:]) / 20, sum(series[-60:]) / 60
        rsi = _rsi(series)
        if not (price > ma20 > ma60 and rsi is not None and 45 <= rsi <= cfg["rsi_max_buy"]):
            continue
        r20, r60 = (price / series[-21] - 1) * 100, (price / series[-61] - 1) * 100
        rets = [math.log(b / a) for a, b in zip(series[-21:-1], series[-20:]) if a > 0 and b > 0]
        vol = (sum(x * x for x in rets) / max(len(rets), 1)) ** 0.5 * math.sqrt(252) * 100
        score = r20 + r60 / 2 - max(0.0, vol - 60) * 0.3   # 연 변동성 60% 넘는 '널뛰기' 종목은 감점
        scored.append({"symbol": sym, "name": infos[sym].get("name", sym), "price": price, "score": round(score, 1),
                       "reason": f"20일 {r20:+.1f}% · 60일 {r60:+.1f}% · RSI {rsi:.0f} · 거래대금 {value20 / 1e8:,.0f}억/일 · 변동성 {vol:.0f}%"})

    scored.sort(key=lambda x: -x["score"])
    picks = []
    for c in scored:
        if len(picks) >= cfg.get("pick_count", 3):
            break
        try:
            warns = toss._get(f"/api/v1/stocks/{c['symbol']}/warnings") or []
        except toss.TossError:
            warns = []
        if warns:
            continue
        picks.append(c)
    note = f"{len(afford)}개 중 조건 통과 {len(scored)}개 → {len(picks)}개 선택"
    log(f"🔎 종목 훑기 끝: {note} — " + ", ".join(f"{p['name']}({p['symbol']})" for p in picks))
    return {"day": datetime.now(toss_kst()).strftime("%Y-%m-%d"), "picks": picks, "scanned": len(afford), "note": note,
            "candidates": scored[:15]}


def toss_kst():
    from datetime import timedelta, timezone
    return timezone(timedelta(hours=9))
