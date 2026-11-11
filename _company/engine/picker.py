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


def session_minutes(market: str, now: datetime | None = None, extended: bool = False) -> int | None:
    """정규장 시작 후 지난 분 (주말·장 밖이면 None). KR 09:00~15:30, US 09:30~16:00 (현지).
    extended=True(미장 프리장 포함)면 뉴욕 04:00 부터 → 프리장은 음수 (-330 ~ -1)"""
    t = mnow(market, now)
    if t.weekday() >= 5:
        return None
    start = 9 * 60 + (0 if market == "KR" else 30)
    m = t.hour * 60 + t.minute - start
    low = -330 if (extended and market == "US") else 0
    return m if low <= m <= 390 else None


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
PROGRESS = {"done": 0, "total": 0, "what": ""}   # 화면에 보여줄 준비 진행률


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


def prepare_day(cfg: dict, log=print, workers: int = 6) -> dict:
    """장 전 하루 한 번: 종목별 전일 종가·20일 평균 거래량·거래대금(원 환산).
    빠르게: ① 예산으로 1주도 못 사는 종목·동전주는 미리 뺌 ② 6개씩 동시에 조회(속도 제한은 toss 가 지킴)
    ③ 300개마다 중간 저장 → 다시 켜도 이어서"""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    market = cfg["market"]
    if base_ready(market):
        return load_base(market)
    today = session_date(market)
    PROGRESS.update(done=0, total=0, what="종목 목록 받기")
    infos = universe(market, log)
    fx = _fx(market, cfg)
    syms = sorted(infos)

    # ① 가격으로 미리 거르기 (예산의 1.5배까지는 남김 — 장중에 올라도 잡히게)
    min_price = 1000 if market == "KR" else cfg.get("us_min_price", 1.0)
    max_price = cfg["max_order_krw"] / fx * 1.5
    PROGRESS.update(what="현재가로 거르기")
    prices = {}
    for chunk in _chunks(syms, 200):
        try:
            prices.update({k: v["price"] for k, v in toss.prices(chunk).items()})
        except toss.TossError as e:
            log(f"   현재가 일부 실패: {e}")
    if prices:
        no_cap = max_price < (5 if market == "US" else 5000)   # 예산을 아직 모르면(현금 0 등) 가격 상한은 안 씀
        syms = [x for x in syms if x in prices and prices[x] >= min_price * 0.7 and (no_cap or prices[x] <= max_price)]

    # ③ 중간 저장분 이어받기
    part_file = _base_file(market).with_suffix(".partial.json")
    rows: dict[str, dict] = {}
    done: set[str] = set()
    try:
        part = json.loads(part_file.read_text(encoding="utf-8"))
        if part.get("day") == today:
            rows, done = part.get("rows", {}), set(part.get("done", []))
    except Exception:
        pass
    todo = [x for x in syms if x not in done]
    log(f"🔎 {'미장' if market == 'US' else '국장'} 급등주 준비: {len(syms)}개 기준값 계산" + (f" (이어서 {len(todo)}개 남음)" if done else "")
        + f" · 동시 {workers}개")
    PROGRESS.update(done=len(syms) - len(todo), total=len(syms), what="급등주 기준값 계산")
    lock = threading.Lock()

    def save_part():
        part_file.parent.mkdir(parents=True, exist_ok=True)
        part_file.write_text(json.dumps({"day": today, "rows": rows, "done": sorted(done)}, ensure_ascii=False), encoding="utf-8")

    def one(sym: str):
        try:
            r = toss._get("/api/v1/candles", symbol=sym, interval="1d", count=22) or {}
        except toss.TossError:
            r = None
        row = None
        if r is not None:
            cs = [c for c in sorted(r.get("candles") or [], key=lambda c: c["timestamp"]) if c["timestamp"][:10] < today]
            if len(cs) >= 20:
                closes = [float(c["closePrice"]) for c in cs[-20:]]
                vols = [float(c.get("volume") or 0) for c in cs[-20:]]
                row = {"name": infos[sym].get("name") or sym, "prev": closes[-1], "vol20": sum(vols) / 20,
                       "value20_krw": sum(c * v for c, v in zip(closes, vols)) / 20 * fx}
        with lock:
            if row:
                rows[sym] = row
            if r is not None:   # 실패한 건 다음에 다시
                done.add(sym)
            PROGRESS["done"] += 1
            n = PROGRESS["done"]
            if n % 300 == 0:
                save_part()
                log(f"   {n}/{PROGRESS['total']}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, todo))
    PROGRESS.update(done=PROGRESS["total"], what="")
    b = {"day": today, "market": market, "rows": rows}
    _base_file(market).parent.mkdir(parents=True, exist_ok=True)
    _base_file(market).write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
    part_file.unlink(missing_ok=True)
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
    lo, hi = cfg["surge_min_change_pct"], cfg["surge_max_change_pct"]
    # 시세 묻기 전에 미리 빼기: 거래가 너무 적은 종목 · 급등해도 예산으로 못 살 만큼 비싼 종목
    cands = [s for s, r in rows.items() if s not in exclude and r["value20_krw"] >= cfg["min_trading_value_krw"] * 0.3
             and r["prev"] * (1 + lo / 100) <= max_price and r["prev"] * (1 + hi / 100) >= min_price]
    prices = {}
    for chunk in _chunks(cands, 200):
        try:
            prices.update({k: v["price"] for k, v in toss.prices(chunk).items()})
        except toss.TossError as e:
            log(f"   현재가 일부 실패: {e}")
    movers = []
    for s, p in prices.items():
        r = rows[s]
        chg = (p / r["prev"] - 1) * 100 if r["prev"] else 0
        if lo <= chg <= hi and min_price <= p <= max_price and r["value20_krw"] >= cfg["min_trading_value_krw"] * 0.3:
            movers.append((chg, s, p))
    movers.sort(reverse=True)
    mins = session_minutes(market, extended=True)
    pre = mins is not None and mins < 0      # 프리장: 거래가 원래 적음 → '평소 하루 거래량의 몇 %가 벌써 거래됐나'로 판단
    elapsed = max(10, mins if mins is not None and mins > 0 else 390)
    frac = min(1.0, elapsed / 390)          # 정규장: 하루 중 지금까지 비율 → 거래량 비교 보정
    from concurrent.futures import ThreadPoolExecutor

    def check(m):
        chg, s, p = m
        try:
            cs = (toss._get("/api/v1/candles", symbol=s, interval="1d", count=1) or {}).get("candles") or []
        except toss.TossError:
            return None
        today_vol = float(cs[0].get("volume") or 0) if cs and cs[0]["timestamp"][:10] == session_date(market) else 0
        value_today = today_vol * p * fx
        if pre:
            ratio = today_vol / max(rows[s]["vol20"], 1)
            if ratio < cfg.get("pre_volume_ratio", 0.2) or value_today < cfg["min_trading_value_krw"] * 0.1:
                return None
        else:
            ratio = today_vol / max(rows[s]["vol20"] * frac, 1)
            if ratio < cfg["surge_volume_ratio"] or value_today < cfg["min_trading_value_krw"] * frac:
                return None
        cur = "원" if market == "KR" else "$"
        return {"symbol": s, "name": rows[s]["name"], "price": p, "change": round(chg, 1), "score": round(chg * min(ratio, 10), 1),
                "ratio": round(ratio, 2), "pre": pre, "minute": mins,
                "reason": (f"[프리장] {chg:+.1f}% · 벌써 평소 하루 거래량의 {ratio * 100:.0f}%" if pre else f"오늘 {chg:+.1f}% · 거래량 평소의 {ratio:.1f}배")
                          + f" · 거래대금 {value_today / 1e8:,.0f}억원 · 현재가 {p:,.2f}{cur}"}

    with ThreadPoolExecutor(max_workers=6) as pool:   # 후보 40개 거래량 확인을 동시에
        found = [x for x in pool.map(check, movers[:40]) if x]
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
    note = f"{t:%H:%M}({'뉴욕' if market == 'US' else '서울'}){' 프리장' if pre else ''} 급등 {len(movers)}개 중 거래량 동반 {len(found)}개 → {len(out)}개"
    return {"day": session_date(market), "time": t.strftime("%H:%M"), "picks": out, "note": note, "strategy": "surge", "market": market}


# ── 급락 신호 (그림자 검증 전용 · 1분봉 계산만, 조회 없음) ──
FLASH_VOL_DRY = 0.5       # 거래량 급감: 직전 평균의 50% 이하
FLASH_EXIT_DROP = -2.0    # 1분 안에 -2% 이상 급락
FLASH_CRASH_PCT = -5.0    # 반등 진입: 1~3분 안에 -5% 이상 급락
FLASH_CRASH_VOL = 3.0     #   그때 거래량 직전 평균의 3배 이상
FLASH_HOLD_VOL = 0.5      #   반등 1분봉 거래량이 급락 때 최대의 50% 이상 (거래량 유지)


def _vbase(bars: list[dict]) -> float | None:
    """직전 10~20개 1분봉 평균 거래량 (10개 미만이면 None)"""
    b = bars[-20:]
    if len(b) < 10:
        return None
    m = sum(x["v"] for x in b) / len(b)
    return m if m > 0 else None


def flash_exit_signal(bars: list[dict], price: float) -> str:
    """보유 중 청산 신호. bars = 다 끝난 1분봉(오래된→최신). 신호면 이유, 아니면 ''
    (호가 잔량 급감은 toss.py 에 호가 조회 함수가 없어서 안 씀)"""
    if len(bars) < 11 or not price:
        return ""
    last = bars[-1]
    base = _vbase(bars[:-1])
    if base and last["v"] <= base * FLASH_VOL_DRY:
        return f"거래량 급감 {last['v'] / base * 100:.0f}% (직전 평균 대비)"
    d1 = (price / last["c"] - 1) * 100 if last["c"] else 0
    d0 = (last["c"] / last["o"] - 1) * 100 if last["o"] else 0
    if min(d1, d0) <= FLASH_EXIT_DROP:
        return f"1분 급락 {min(d1, d0):+.1f}%"
    return ""


def flash_rebound_signal(bars: list[dict], price: float) -> dict | None:
    """급락 반등 진입: 1~3분 안에 -5%↓ + 거래량 3배↑ → 바로 다음 1분봉이 양봉 반등 + 거래량 유지.
    bars = 다 끝난 1분봉 (마지막 = 반등 봉). 신호면 {low, crash_t, drop, vol, why}"""
    if len(bars) < 13 or not price:
        return None
    reb = bars[-1]
    for w in (1, 2, 3):
        win = bars[-1 - w:-1]
        if len(bars) < w + 12:
            break
        ref = bars[-2 - w]["c"]
        base = _vbase(bars[:-1 - w])
        if not ref or not base:
            continue
        low = min(x["l"] for x in win)
        drop = (low / ref - 1) * 100
        vmax = max(x["v"] for x in win)
        if drop > FLASH_CRASH_PCT or vmax < base * FLASH_CRASH_VOL:
            continue
        if not (reb["c"] > reb["o"] and reb["c"] > win[-1]["c"] and reb["v"] >= vmax * FLASH_HOLD_VOL and price > low):
            continue
        return {"low": low, "crash_t": win[-1]["t"], "drop": round(drop, 1), "vol": round(vmax / base, 1),
                "why": f"{w}분 {drop:+.1f}%·거래량 {vmax / base:.1f}배 → 반등"}
    return None


# ── 추세 (국장, 하루 한 번) ──
def _rsi(xs: list[float], n: int = 14) -> float | None:
    if len(xs) <= n:
        return None
    g = l = 0.0
    for a, b in zip(xs[-n - 1:-1], xs[-n:]):
        g += max(b - a, 0); l += max(a - b, 0)
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def scan(cfg: dict, log=print, exclude: set[str] | None = None, workers: int = 6) -> dict:
    """스윙(추세) 후보: 현재가 > 20일선 > 60일선, RSI 적당, 유동성 · 점수 높은 순 N*2 개.
    급등주 기준값(base)이 있으면 그 목록에서 거래대금·가격으로 먼저 걸러서 빠르게 (미장도 가능), 6개씩 동시에 조회."""
    from concurrent.futures import ThreadPoolExecutor
    market = cfg.get("market", "KR")
    exclude = exclude or set()
    fx = _fx(market, cfg)
    min_price = 1000 if market == "KR" else cfg.get("us_min_price", 1.0)
    max_price = (cfg["max_order_krw"] / fx) if cfg.get("max_order_krw") else 1e12
    base = load_base(market) if base_ready(market) else {}
    if base.get("rows"):
        names = {k: v["name"] for k, v in base["rows"].items()}
        cands = [k for k, v in base["rows"].items() if k not in exclude and v["value20_krw"] >= cfg["min_trading_value_krw"]
                 and min_price <= v["prev"] <= max_price]
    else:
        infos = universe(market, log)
        names = {k: v.get("name", k) for k, v in infos.items()}
        cands = [k for k in infos if k not in exclude]
    prices = {}
    for chunk in _chunks(cands, 200):
        try:
            prices.update({k: v["price"] for k, v in toss.prices(chunk).items()})
        except toss.TossError as e:
            log(f"   현재가 일부 실패: {e}")
    afford = [x for x in cands if min_price <= prices.get(x, 0) <= max_price]
    log(f"🔎 스윙(추세) 후보 찾기: {len(afford)}개 차트 확인 (동시 {workers}개)")
    today = session_date(market)

    def one(sym):
        try:
            r = toss._get("/api/v1/candles", symbol=sym, interval="1d", count=70) or {}
        except toss.TossError:
            return None
        cs = sorted(r.get("candles") or [], key=lambda c: c["timestamp"])
        if len(cs) < 61:
            return None
        closes = [float(c["closePrice"]) for c in cs]
        vols = [float(c.get("volume") or 0) for c in cs]
        price = prices[sym]
        series = (closes[:-1] if cs[-1]["timestamp"][:10] == today else closes) + [price]
        value20 = sum(c * v for c, v in zip(closes[-20:], vols[-20:])) / 20 * fx
        if value20 < cfg["min_trading_value_krw"]:
            return None
        ma20, ma60, rsi = sum(series[-20:]) / 20, sum(series[-60:]) / 60, _rsi(series)
        if not (price > ma20 > ma60 and rsi is not None and 45 <= rsi <= cfg["rsi_max_buy"]):
            return None
        r20, r60 = (price / series[-21] - 1) * 100, (price / series[-61] - 1) * 100
        rets = [math.log(b / a) for a, b in zip(series[-21:-1], series[-20:]) if a > 0 and b > 0]
        vol = (sum(x * x for x in rets) / max(len(rets), 1)) ** 0.5 * math.sqrt(252) * 100
        return {"symbol": sym, "name": names.get(sym, sym), "price": price, "r20": round(r20, 1), "r60": round(r60, 1), "rsi": round(rsi),
                "score": round(r20 + r60 / 2 - max(0.0, vol - 60) * 0.3, 1),
                "reason": f"20일 {r20:+.1f}% · 60일 {r60:+.1f}% · RSI {rsi:.0f} · 거래대금 {value20 / 1e8:,.0f}억원/일 · 변동성 {vol:.0f}%"}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        scored = [x for x in pool.map(one, afford) if x]
    scored.sort(key=lambda x: -x["score"])
    picks = []
    for c in scored:
        if len(picks) >= cfg.get("pick_count", 3) * 2:
            break
        try:
            if toss._get(f"/api/v1/stocks/{c['symbol']}/warnings"):
                continue
        except toss.TossError:
            pass
        picks.append(c)
    note = f"{len(afford)}개 중 추세 조건 통과 {len(scored)}개 → {len(picks)}개"
    return {"day": today, "picks": picks, "note": note, "strategy": "trend", "market": market}
