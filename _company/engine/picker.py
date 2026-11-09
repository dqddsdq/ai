"""자동매매용 종목 고르기 — 미국장(US) / 국장(KR).

전략
- surge (급등주, 기본): 장중 몇 분마다 '오늘 크게 오르는 중 + 거래량이 평소보다 폭증' 종목
- trend (추세): 하루 한 번 '꾸준히 오르는' 종목 (국장만)

종목 목록
- 미국: 나스닥이 매일 공개하는 전체 상장 목록 (nasdaqtrader.com) → ETF·워런트·우선주·유닛·테스트 종목 제외
- 국내: DART 상장사 목록 (DART 키 필요)
→ 토스 종목 정보로 정상 거래·보통주·레버리지 아님만 남김 (토스 API 는 종목 검색 기능이 없음)

급등주 판단 기준값(전일 종가·20일 평균 거래량)은 장 전에 한 번 계산해서 _cache 에 보관 (처음 몇 분~10여 분).
토스 요청은 초당 7~8건으로 천천히 (한도 초과 방지).
"""

from __future__ import annotations

import json
import math
import re
import urllib.request
from datetime import date, datetime, timedelta, timezone

from . import dart, toss
from .agents import COMPANY_DIR

KST = timezone(timedelta(hours=9))
MARKETS = {"KR": {"KOSPI", "KOSDAQ"}, "US": {"NYSE", "NASDAQ", "AMEX"}}
US_LISTS = ["https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
            "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"]
BAD_NAME = re.compile(r"warrant|\bunits?\b|\brights?\b|preferred|\bnotes?\b|debenture|%|\bdue 20|acquisition corp|trust units", re.I)


# ── 시장 시간 ──
def _us_dst(d: date) -> bool:
    """미국 서머타임: 3월 둘째 일요일 ~ 11월 첫째 일요일"""
    def nth_sunday(y, m, n):
        first = date(y, m, 1)
        return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))
    return nth_sunday(d.year, 3, 2) <= d < nth_sunday(d.year, 11, 1)


def mnow(market: str, now: datetime | None = None) -> datetime:
    """그 시장의 현지 시각 (KR=한국, US=뉴욕)"""
    now = (now or datetime.now(KST)).astimezone(timezone.utc)
    if market == "KR":
        return now.astimezone(KST)
    est = now.astimezone(timezone(timedelta(hours=-5)))
    return now.astimezone(timezone(timedelta(hours=-4 if _us_dst(est.date()) else -5)))


def session_minutes(market: str, now: datetime | None = None) -> int | None:
    """정규장 시작 후 지난 분 (주말·장 전·장 후면 None). KR 09:00~15:30, US 09:30~16:00 (현지)"""
    t = mnow(market, now)
    if t.weekday() >= 5:
        return None
    start = 9 * 60 + (0 if market == "KR" else 30)
    m = t.hour * 60 + t.minute - start
    return m if 0 <= m <= 390 else None


def session_date(market: str, now: datetime | None = None) -> str:
    return mnow(market, now).strftime("%Y-%m-%d")


def _fx(market: str, cfg: dict) -> float:
    return 1.0 if market == "KR" else float(cfg.get("usd_krw") or 1400)


# ── 종목 목록 ──
def _chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def us_symbols(log=print) -> list[str]:
    """미국 전체 상장 보통주 후보 (하루 한 번 받아서 보관)."""
    f = COMPANY_DIR / "_cache" / "us_symbols.json"
    today = session_date("US")
    try:
        c = json.loads(f.read_text(encoding="utf-8"))
        if c.get("day") == today and c.get("symbols"):
            return c["symbols"]
    except Exception:
        pass
    syms: set[str] = set()
    for url in US_LISTS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (company-ai)"})
            text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
        except Exception as e:
            log(f"   미국 종목 목록 실패 ({url.rsplit('/', 1)[1]}): {type(e).__name__}")
            continue
        lines = text.splitlines()
        head = lines[0].split("|")
        idx = {h: i for i, h in enumerate(head)}
        sym_col = "Symbol" if "Symbol" in idx else "ACT Symbol"
        for ln in lines[1:]:
            p = ln.split("|")
            if len(p) < len(head) or ln.startswith("File Creation"):
                continue
            s, name = p[idx[sym_col]].strip(), p[idx["Security Name"]]
            if p[idx.get("Test Issue", 0)] == "Y" or p[idx.get("ETF", 0)] == "Y":
                continue
            if not re.fullmatch(r"[A-Z]{1,5}", s) or BAD_NAME.search(name):
                continue
            syms.add(s)
    out = sorted(syms)
    if out:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"day": today, "symbols": out}), encoding="utf-8")
    return out


def _is_plain_stock(info: dict, market: str = "KR") -> bool:
    lev = info.get("leverageFactor")
    try:
        lev_ok = lev in (None, "", 1, "1") or float(lev) == 1
    except (TypeError, ValueError):
        lev_ok = False
    return (info.get("market") in MARKETS[market] and info.get("status") == "ACTIVE"
            and info.get("isCommonShare") is not False and lev_ok
            and str(info.get("securityType") or "STOCK").upper() in ("STOCK", "COMMON", "COMMON_STOCK", "EQUITY", ""))


def universe(market: str, log=print) -> dict[str, dict]:
    """{종목: 토스 종목 정보} — 정상 거래 보통주만"""
    if market == "KR":
        if not dart.enabled():
            raise RuntimeError("국장 종목 목록은 DART 키가 필요해요 (🧰 도구)")
        codes = sorted(dart.corps()["by_stock"])
    else:
        codes = us_symbols(log)
        if not codes:
            raise RuntimeError("미국 종목 목록을 못 받았어요 (nasdaqtrader.com 접속 확인)")
    infos = {}
    for chunk in _chunks(codes, 200):
        try:
            for it in toss._get("/api/v1/stocks", symbols=",".join(chunk)) or []:
                infos[it["symbol"]] = it
        except toss.TossError as e:
            log(f"   종목 정보 일부 실패: {e}")
    return {s: i for s, i in infos.items() if _is_plain_stock(i, market)}


# ── 급등주 ──
def _base_file(market: str):
    return COMPANY_DIR / "_cache" / f"picker_base_{market}.json"


def base_ready(market: str) -> bool:
    try:
        b = json.loads(_base_file(market).read_text(encoding="utf-8"))
        return b.get("day") == session_date(market) and bool(b.get("rows"))
    except Exception:
        return False


def load_base(market: str) -> dict:
    try:
        return json.loads(_base_file(market).read_text(encoding="utf-8"))
    except Exception:
        return {}


def prepare_day(cfg: dict, log=print) -> dict:
    """장 전 하루 한 번: 종목별 전일 종가·20일 평균 거래량·거래대금(원 환산)."""
    market = cfg["market"]
    if base_ready(market):
        return load_base(market)
    today = session_date(market)
    infos = universe(market, log)
    fx = _fx(market, cfg)
    syms = sorted(infos)
    log(f"🔎 {'미장' if market == 'US' else '국장'} 급등주 준비: 보통주 {len(syms)}개 기준값 계산 중 (처음엔 몇 분~10여 분)")
    rows = {}
    for i, sym in enumerate(syms):
        if i and i % 500 == 0:
            log(f"   {i}/{len(syms)}")
        try:
            r = toss._get("/api/v1/candles", symbol=sym, interval="1d", count=22) or {}
        except toss.TossError:
            continue
        cs = [c for c in sorted(r.get("candles") or [], key=lambda c: c["timestamp"]) if c["timestamp"][:10] < today]
        if len(cs) < 20:
            continue
        closes = [float(c["closePrice"]) for c in cs[-20:]]
        vols = [float(c.get("volume") or 0) for c in cs[-20:]]
        rows[sym] = {"name": infos[sym].get("name") or sym, "prev": closes[-1], "vol20": sum(vols) / 20,
                     "value20_krw": sum(c * v for c, v in zip(closes, vols)) / 20 * fx}
    b = {"day": today, "market": market, "rows": rows}
    _base_file(market).parent.mkdir(parents=True, exist_ok=True)
    _base_file(market).write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
    log(f"🔎 급등주 준비 끝: {len(rows)}개")
    return b


def surge_scan(cfg: dict, log=print, exclude: set[str] | None = None) -> dict:
    """지금 급등 중: 전일 대비 +N% 이상(너무 오른 건 제외) · 평소보다 거래량 폭증 · 1주 살 수 있는 가격 · 경고 없음."""
    market = cfg["market"]
    base = load_base(market)
    rows = base.get("rows") or {}
    exclude = exclude or set()
    t = mnow(market)
    if not rows or base.get("day") != session_date(market):
        return {"day": "", "picks": [], "note": "급등주 기준값이 아직 없어요 (장 전에 자동 준비)"}
    fx = _fx(market, cfg)
    max_price = cfg["max_order_krw"] / fx
    min_price = 1000 if market == "KR" else cfg.get("us_min_price", 1.0)
    prices = {}
    for chunk in _chunks([s for s in rows if s not in exclude], 200):
        try:
            prices.update({k: v["price"] for k, v in toss.prices(chunk).items()})
        except toss.TossError as e:
            log(f"   현재가 일부 실패: {e}")
    lo, hi = cfg["surge_min_change_pct"], cfg["surge_max_change_pct"]
    movers = []
    for s, p in prices.items():
        r = rows[s]
        chg = (p / r["prev"] - 1) * 100 if r["prev"] else 0
        if lo <= chg <= hi and min_price <= p <= max_price and r["value20_krw"] >= cfg["min_trading_value_krw"] * 0.3:
            movers.append((chg, s, p))
    movers.sort(reverse=True)
    elapsed = max(10, session_minutes(market) or 390)
    frac = min(1.0, elapsed / 390)          # 하루 중 지금까지 비율 → 거래량 비교 보정
    found = []
    for chg, s, p in movers[:40]:
        try:
            cs = (toss._get("/api/v1/candles", symbol=s, interval="1d", count=1) or {}).get("candles") or []
        except toss.TossError:
            continue
        today_vol = float(cs[0].get("volume") or 0) if cs and cs[0]["timestamp"][:10] == session_date(market) else 0
        ratio = today_vol / max(rows[s]["vol20"] * frac, 1)
        value_today = today_vol * p * fx
        if ratio < cfg["surge_volume_ratio"] or value_today < cfg["min_trading_value_krw"] * frac:
            continue
        cur = "원" if market == "KR" else "$"
        found.append({"symbol": s, "name": rows[s]["name"], "price": p, "change": round(chg, 1), "score": round(chg * min(ratio, 10), 1),
                      "reason": f"오늘 {chg:+.1f}% · 거래량 평소의 {ratio:.1f}배 · 오늘 거래대금 {value_today / 1e8:,.0f}억원 · 현재가 {p:,.2f}{cur}"})
    found.sort(key=lambda x: -x["score"])
    out = []
    for c in found:
        if len(out) >= cfg.get("pick_count", 3):
            break
        try:
            if toss._get(f"/api/v1/stocks/{c['symbol']}/warnings"):
                continue
        except toss.TossError:
            pass
        out.append(c)
    note = f"{t:%H:%M}({'뉴욕' if market == 'US' else '서울'}) 급등 {len(movers)}개 중 거래량 동반 {len(found)}개 → {len(out)}개"
    return {"day": session_date(market), "time": t.strftime("%H:%M"), "picks": out, "note": note, "strategy": "surge", "market": market}


# ── 추세 (국장, 하루 한 번) ──
def _rsi(xs: list[float], n: int = 14) -> float | None:
    if len(xs) <= n:
        return None
    g = l = 0.0
    for a, b in zip(xs[-n - 1:-1], xs[-n:]):
        g += max(b - a, 0); l += max(a - b, 0)
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def scan(cfg: dict, log=print, exclude: set[str] | None = None) -> dict:
    """꾸준한 상승 추세 종목 (현재가 > 20일선 > 60일선, RSI 적당, 유동성) 상위 N개."""
    market = cfg.get("market", "KR")
    exclude = exclude or set()
    infos = universe(market, log)
    fx = _fx(market, cfg)
    plain = [s for s in infos if s not in exclude]
    prices = {}
    for chunk in _chunks(plain, 200):
        try:
            prices.update({k: v["price"] for k, v in toss.prices(chunk).items()})
        except toss.TossError as e:
            log(f"   현재가 일부 실패: {e}")
    min_price = 1000 if market == "KR" else cfg.get("us_min_price", 1.0)
    afford = [s for s in plain if min_price <= prices.get(s, 0) <= cfg["max_order_krw"] / fx]
    log(f"🔎 추세 종목 찾기: {len(afford)}개 차트 확인 (몇 분)")
    today = session_date(market)
    scored = []
    for sym in afford:
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
        series = (closes[:-1] if cs[-1]["timestamp"][:10] == today else closes) + [price]
        value20 = sum(c * v for c, v in zip(closes[-20:], vols[-20:])) / 20 * fx
        if value20 < cfg["min_trading_value_krw"]:
            continue
        ma20, ma60, rsi = sum(series[-20:]) / 20, sum(series[-60:]) / 60, _rsi(series)
        if not (price > ma20 > ma60 and rsi is not None and 45 <= rsi <= cfg["rsi_max_buy"]):
            continue
        r20, r60 = (price / series[-21] - 1) * 100, (price / series[-61] - 1) * 100
        rets = [math.log(b / a) for a, b in zip(series[-21:-1], series[-20:]) if a > 0 and b > 0]
        vol = (sum(x * x for x in rets) / max(len(rets), 1)) ** 0.5 * math.sqrt(252) * 100
        scored.append({"symbol": sym, "name": infos[sym].get("name", sym), "price": price,
                       "score": round(r20 + r60 / 2 - max(0.0, vol - 60) * 0.3, 1),
                       "reason": f"20일 {r20:+.1f}% · 60일 {r60:+.1f}% · RSI {rsi:.0f} · 거래대금 {value20 / 1e8:,.0f}억원/일 · 변동성 {vol:.0f}%"})
    scored.sort(key=lambda x: -x["score"])
    picks = []
    for c in scored:
        if len(picks) >= cfg.get("pick_count", 3):
            break
        try:
            if toss._get(f"/api/v1/stocks/{c['symbol']}/warnings"):
                continue
        except toss.TossError:
            pass
        picks.append(c)
    note = f"{len(afford)}개 중 조건 통과 {len(scored)}개 → {len(picks)}개 선택"
    return {"day": today, "picks": picks, "note": note, "strategy": "trend", "market": market}
