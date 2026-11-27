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
VE_VOL = 3.0              # 📈 거래량 증가 초기 진입: 1분봉 거래량이 직전 평균의 3배 이상
VE_MAX_RISE = 3.0         #   급증 시작 전 종가 대비 상승폭 3% 이하 (초입만)
VE_PEAK_KEEP = 0.8        #   마지막 봉 거래량이 최근 급증 봉 최대의 80% 미만이면 '정점 후 감소' → 제외


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


VF_VOL = 0.6              # 📉 거래량 감소+VWAP 아래: 최근 3봉 평균 거래량이 직전 평균의 60% 이하
OB_WEAK = 0.5             # 📚 호가 약화: 매수잔량/매도잔량 0.5 이하
OB_DROP = 0.6             #   그리고 산 직후 비율의 60% 이하로 떨어짐


def vwap_fade_signal(bars: list[dict], price: float) -> str:
    """보유 중 청산 신호 (그림자 검증 전용). bars = 오늘 다 끝난 1분봉(오래된→최신).
    최근 3봉 거래량이 직전 평균보다 줄고 + 현재가가 오늘 VWAP 아래면 이유, 아니면 ''"""
    if len(bars) < 13 or not price:
        return ""
    base = _vbase(bars[:-3])
    if not base:
        return ""
    rv = sum(b["v"] for b in bars[-3:]) / 3
    if rv > base * VF_VOL:
        return ""
    pv = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in bars)
    vs = sum(b["v"] for b in bars)
    vwap = pv / vs if vs else 0
    if not vwap or price >= vwap:
        return ""
    return f"거래량 감소 {rv / base * 100:.0f}%·VWAP 아래 {(price / vwap - 1) * 100:+.1f}%"


def book_weak_signal(ratio: float | None, ratio0: float | None) -> str:
    """호가 약화 청산 신호 (그림자 검증 전용). ratio = 지금 매수잔량/매도잔량, ratio0 = 산 직후 값"""
    if not ratio or not ratio0:
        return ""
    if ratio <= OB_WEAK and ratio <= ratio0 * OB_DROP:
        return f"호가 약화 매수/매도 잔량 {ratio:.2f} (산 직후 {ratio0:.2f})"
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


def vol_early_signal(bars: list[dict], price: float) -> dict | None:
    """📈 거래량 증가 초기 진입 (그림자 검증 전용). bars = 오늘 다 끝난 1분봉(오래된→최신).
    최근 1~3봉에서 거래량이 직전 평균의 VE_VOL 배↑로 막 늘기 시작 + 시작 전 대비 상승 0~VE_MAX_RISE% + VWAP 위.
    거래량 정점 뒤 줄어드는 중이면 제외. 신호면 {start_t, vol, rise, vwap_gap, hi_gap, why}"""
    if len(bars) < 16 or not price:
        return None
    base = _vbase(bars[:-3])
    if not base:
        return None
    rec = bars[-3:]
    hits = [i for i, b in enumerate(rec) if b["v"] >= base * VE_VOL]
    if not hits:
        return None
    if any(b["v"] >= base * VE_VOL for b in bars[-6:-3]):   # 이미 그 전부터 급증 중 → 초기 아님
        return None
    vmax = max(b["v"] for b in rec[hits[0]:])
    if rec[-1]["v"] < vmax * VE_PEAK_KEEP:   # 거래량 정점 뒤 감소 구간 제외
        return None
    ref = bars[-4 + hits[0]]["c"]            # 급증 시작 직전 봉 종가
    rise = (price / ref - 1) * 100 if ref else 0
    if rise <= 0 or rise > VE_MAX_RISE:
        return None
    pv = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in bars)
    vs = sum(b["v"] for b in bars)
    vwap = pv / vs if vs else 0
    if not vwap or price <= vwap:
        return None
    hi = max(b["h"] for b in bars)
    vr, vg, hg = rec[-1]["v"] / base, (price / vwap - 1) * 100, (price / hi - 1) * 100
    return {"start_t": rec[hits[0]]["t"], "vol": round(vr, 1), "rise": round(rise, 2), "vwap_gap": round(vg, 2),
            "hi_gap": round(hg, 2), "why": f"거래량 {vr:.1f}배 시작·상승 {rise:+.1f}%·VWAP {vg:+.1f}%·고점 대비 {hg:+.1f}%"}


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
    _pb_found: list[dict] = []   # 눌림 반등 후보 (그림자 검증 전용)

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
        hist = cs[:-1] if cs[-1]["timestamp"][:10] == today else cs   # 끝난 일봉만 (눌림 검증용)
        pb = pullback_setup(hist, value20, cfg)   # 🧪 같은 일봉으로 눌림 후보도 (추가 조회 없음)
        if pb:
            pb.update(symbol=sym, name=names.get(sym, sym))
            _pb_found.append(pb)
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
    pbs = []   # 거래대금 큰 순으로 PB_MAX 개 (경고 종목 제외)
    for x in sorted(_pb_found, key=lambda x: -x["value20"]):
        if len(pbs) >= PB_MAX:
            break
        try:
            if toss._get(f"/api/v1/stocks/{x['symbol']}/warnings"):
                continue
        except toss.TossError:
            pass
        pbs.append(x)
    return {"day": today, "picks": picks, "note": note, "strategy": "trend", "market": market, "pullback": pbs}


# ── 🧪 눌림 반등 (그림자 검증 전용 · 일봉 계산만, 조회 없음) ──
PB_VALUE_X = 3.0        # 거래대금: 최소 기준의 3배 이상 (큰 종목만)
PB_DIP = (4.0, 15.0)    # 20일 고점 대비 최근 5일 저점 -4~-15% 눌림
PB_MA_TOUCH = 1.03      # 최근 5일 저점이 20일선 +3% 안까지 내려옴
PB_R20_MAX = 20.0       # 과열 제외: 20일 상승률 20% 이하
PB_RSI_MAX = 62         # 과열 제외: RSI 62 이하
PB_MAX = 20             # 하루 후보 최대 개수
PB_NEAR_HI = 0.97       # 장중: 20일 고점의 97% 넘으면 고점 근처 → 제외
PB_DAY_MAX = 5.0        # 장중: 오늘 +5% 넘으면 급등 → 제외


def pullback_setup(cs: list[dict], value20: float, cfg: dict) -> dict | None:
    """상승 추세(20일선>60일선·60일선 오름) 중 20일선 근처까지 눌린 종목. cs = 끝난 일봉(오래된→최신).
    반등 확인은 장중에 (전일 고가 돌파) 그림자에서 함. 조건 맞으면 기준값, 아니면 None"""
    if len(cs) < 68 or value20 < cfg["min_trading_value_krw"] * PB_VALUE_X:
        return None
    closes = [float(c["closePrice"]) for c in cs]
    lows = [float(c.get("lowPrice") or c["closePrice"]) for c in cs]
    highs = [float(c.get("highPrice") or c["closePrice"]) for c in cs]
    ma20, ma60 = sum(closes[-20:]) / 20, sum(closes[-60:]) / 60
    ma60_old = sum(closes[-68:-8]) / 60   # 8일 전 60일선
    if not (ma20 > ma60 and closes[-1] > ma60 and ma60 > ma60_old):
        return None
    hi20, lo5 = max(highs[-20:]), min(lows[-5:])
    dip = (1 - lo5 / hi20) * 100 if hi20 else 0
    r20 = (closes[-1] / closes[-21] - 1) * 100 if closes[-21] else 0
    rsi = _rsi(closes)
    if not (PB_DIP[0] <= dip <= PB_DIP[1] and lo5 <= ma20 * PB_MA_TOUCH and r20 <= PB_R20_MAX
            and rsi is not None and rsi <= PB_RSI_MAX):
        return None
    return {"prev": closes[-1], "prev_high": highs[-1], "hi20": hi20, "ma20": round(ma20, 4), "value20": round(value20),
            "dip": round(dip, 1), "r20": round(r20, 1), "rsi": round(rsi),
            "why": f"20일 고점 대비 -{dip:.1f}% 눌림·20일선 근처·RSI {rsi:.0f}·거래대금 {value20 / 1e8:,.0f}억원/일"}


def pullback_rebound(pb: dict, price: float) -> str:
    """장중 반등 확인: 전일 고가 돌파 + 20일선 위 + 고점 근처·오늘 급등 아님. 맞으면 이유, 아니면 ''"""
    if not price or not pb.get("prev"):
        return ""
    chg = (price / pb["prev"] - 1) * 100
    if price <= pb["prev_high"] or price < pb["ma20"] or price > pb["hi20"] * PB_NEAR_HI or not 0 < chg <= PB_DAY_MAX:
        return ""
    return f"[눌림반등] {pb['why']} → 전일 고가 돌파 {chg:+.1f}%"


# ── 초반 돌파 감지 (1분봉 기준 VWAP 돌파) ──
def early_breakout_scan(cfg: dict, log=print, exclude: set[str] | None = None) -> dict:
    """초반 돌파 감지: 1분봉 기준 최근 60분 평균 거래량 대비 크게 증가 + VWAP 위로 돌파 + 오늘 등락률 +8% 미만 후보.
    (강한 거래량 동반 초반 상승이 실제 급등 기회 신호인지 검증용)"""
    market = cfg["market"]
    base = load_base(market)
    rows = base.get("rows") or {}
    exclude = exclude or set()
    t = mnow(market)
    if not rows or base.get("day") != session_date(market):
        return {"day": "", "picks": [], "note": "초반 돌파 기준값이 아직 없어요", "strategy": "breakout"}
    fx = _fx(market, cfg)
    max_price = cfg["max_order_krw"] / fx
    min_price = 1000 if market == "KR" else cfg.get("us_min_price", 1.0)

    # 거래량·거래대금이 충분한 종목 필터링
    candidates = [s for s, r in rows.items() if s not in exclude and r["value20_krw"] >= cfg["min_trading_value_krw"] * 0.3
                  and r["prev"] * 0.8 <= max_price and r["prev"] >= min_price * 1.0]

    # 현재가 조회
    prices = {}
    for chunk in _chunks(candidates, 200):
        try:
            prices.update({k: v["price"] for k, v in toss.prices(chunk).items()})
        except toss.TossError as e:
            log(f"   현재가 일부 실패: {e}")

    today_str = session_date(market)
    picks = []
    from concurrent.futures import ThreadPoolExecutor

    def check(sym: str):
        try:
            # 1분봉 최근 70개(약 70분) 받기
            r = toss._get("/api/v1/candles", symbol=sym, interval="1m", count=70) or {}
            candles_1m = sorted(r.get("candles") or [], key=lambda c: c["timestamp"])
            if len(candles_1m) < 60:
                return None

            # 오늘 데이터만 필터
            today_candles = [c for c in candles_1m if c["timestamp"][:10] == today_str]
            if len(today_candles) < 10:  # 최소 10분은 거래되었어야 함
                return None

            # 최근 60분 평균 거래량 계산 (전 60분)
            vol_60m = [float(c.get("volume") or 0) for c in candles_1m[-60:]]
            avg_vol_60m = sum(vol_60m) / len(vol_60m) if vol_60m else 0

            # 현재 1분봉 거래량 (마지막)
            last_1m = today_candles[-1]
            curr_vol = float(last_1m.get("volume") or 0)

            # VWAP 계산 (오늘 데이터만)
            pv_sum = v_sum = 0.0
            for c in today_candles:
                tp = (float(c.get("highPrice") or c["closePrice"]) + float(c.get("lowPrice") or c["closePrice"])
                      + float(c["closePrice"])) / 3
                vol = float(c.get("volume") or 0)
                pv_sum += tp * vol
                v_sum += vol
            vwap = pv_sum / v_sum if v_sum > 0 else None

            # 현재가
            curr_price = prices.get(sym)
            if not curr_price or not vwap:
                return None

            # 조건 검사
            # ① 거래량 최근 60분 평균 대비 2배 이상 증가
            vol_ratio = curr_vol / max(avg_vol_60m, 1)
            if vol_ratio < 2.0:
                return None

            # ② 가격이 VWAP 위로
            if curr_price <= vwap * 1.001:  # 거의 같거나 아래면 제외
                return None

            # ③ 오늘 등락률 +8% 미만
            daily_change = ((curr_price / rows[sym]["prev"]) - 1) * 100
            if daily_change >= 8.0:
                return None

            # 추가 필터: 거래대금 확인
            value_today = curr_vol * curr_price * fx
            if value_today < cfg["min_trading_value_krw"] * 0.1:
                return None

            cur_unit = "원" if market == "KR" else "$"
            return {
                "symbol": sym, "name": rows[sym]["name"], "price": curr_price,
                "change": round(daily_change, 1), "vwap": round(vwap, 2),
                "vol_ratio": round(vol_ratio, 2), "score": round(vol_ratio * (curr_price / vwap - 1) * 100, 1),
                "reason": (f"오늘 {daily_change:+.1f}% · 거래량 최근 60분 평균의 {vol_ratio:.1f}배 · VWAP {vwap:,.2f}{cur_unit} "
                          f"→ 현재 {curr_price:,.2f}{cur_unit}")
            }
        except (toss.TossError, KeyError, ValueError):
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = [x for x in pool.map(check, prices.keys()) if x]

    # 스코어로 정렬
    results.sort(key=lambda x: -x["score"])

    # 경고 제거
    for c in results:
        if len(picks) >= cfg.get("pick_count", 3):
            break
        try:
            if toss._get(f"/api/v1/stocks/{c['symbol']}/warnings"):
                continue
        except toss.TossError:
            pass
        picks.append(c)

    note = f"{t:%H:%M} 초반 돌파: {len(prices)}개 중 {len(results)}개 감지 → {len(picks)}개"
    return {"day": today_str, "time": t.strftime("%H:%M"), "picks": picks, "note": note, "strategy": "breakout", "market": market}
