"""🧰 추가 기능 모음 — 시장 분위기·시간대·업종 쏠림·호가 스프레드·토스 장애·백업·헬스체크·성적표·세금·내일 계획·자동 개선 효과.

매매 로직에 '조심하는 방향'으로만 끼어듦 (사는 걸 줄이거나 쉬게만 함 · 파는 건 절대 막지 않음).
조회는 전부 toss.py 공개 함수로만 (조회 속도 제한·계좌 안전장치 그대로).
"""

from __future__ import annotations


import math
import os

import statistics as st
import threading
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import toss
from . import tradelearn as tl
from .agents import COMPANY_DIR

KST = timezone(timedelta(hours=9))
INDEX = {"US": "QQQ", "KR": "069500"}          # 시장 분위기·비교 기준 (나스닥100 ETF · KODEX 200)
INDEX_NAME = {"US": "나스닥100(QQQ)", "KR": "코스피200(KODEX 200)"}
_mem: dict = {}
_mlock = threading.Lock()


def _cached(key, ttl: float, fn):
    with _mlock:
        hit = _mem.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    try:
        v = fn()
    except Exception:
        v = None
    with _mlock:
        _mem[key] = (time.time(), v)
    return v


def _now() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


# ── 1. 시장 분위기 (지수가 크게 빠지는 날 급등주 덜 사기) ──
def index_change(market: str) -> float | None:
    """기준 지수 ETF 의 오늘 등락률(%) — 5분 보관"""
    def fn():
        sym = INDEX[market]
        closes, _, dates = toss.daily(sym, 5)
        px = (toss.prices([sym]).get(sym) or {}).get("price")
        if not px or len(closes) < 2:
            return None
        from . import picker
        prev = closes[-2] if dates and dates[-1][:10] >= picker.session_date(market) else closes[-1]
        return round((float(px) / prev - 1) * 100, 2) if prev else None
    return _cached(("idx", market), 300, fn)


def regime(market: str, c: dict) -> tuple[float, str]:
    """(급등주 금액 배수, 이유) — 지수 -1.5% 이하면 절반, -3% 이하면 0 (새로 안 삼)"""
    if not c.get("regime_on", True):
        return 1.0, ""
    ch = index_change(market)
    if ch is None:
        return 1.0, ""
    if ch <= -float(c.get("regime_stop_pct", 3)):
        return 0.0, f"📉 {INDEX_NAME[market]} 오늘 {ch:+.1f}% — 시장이 크게 빠지는 날이라 급등주 새로 안 삼"
    if ch <= -float(c.get("regime_half_pct", 1.5)):
        return 0.5, f"📉 {INDEX_NAME[market]} 오늘 {ch:+.1f}% → 급등주 금액 절반"
    return 1.0, ""


def crash_day(market: str, c: dict) -> str:
    """지수가 -regime_stop_pct(기본 3)% 이하면 이유, 아니면 '' — 스윙 신규 매수 막기·손절 확인 촘촘히"""
    if not c.get("regime_on", True):
        return ""
    try:
        ch = index_change(market)
    except Exception:
        return ""
    if ch is None or ch > -float(c.get("regime_stop_pct", 3)):
        return ""
    return f"📉 {INDEX_NAME[market]} 오늘 {ch:+.1f}% — 시장이 크게 빠지는 날이라 스윙 새로 안 삼"


# ── 2. 시간대별 성적 (계속 지는 시간대는 피하기) ──
def tod_bucket(minutes) -> str:
    if minutes is None:
        return "장외"
    m = int(minutes)
    return "프리장" if m < 0 else "장 시작 30분" if m < 30 else "오전" if m < 150 else "오후" if m < 330 else "마감 전"


def tod_stats(market: str, days: int = 30) -> dict:
    since = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    with tl._lock:
        ts = [t for t in tl._load()["trades"] if t["closed"] and tl._tm(t) == market and tl._ts(t) == "surge" and t["day"] >= since
              and (t.get("features") or {}).get("minute") is not None]
    out = {}
    for t in ts:
        out.setdefault(tod_bucket(t["features"]["minute"]), []).append(t)
    return {k: tl._stats(v) for k, v in out.items()}


def tod_block(market: str, minutes, c: dict) -> str:
    """이 시간대가 최근 30일 10건↑ · 승률 35% 미만 · 평균 마이너스면 이유, 아니면 ''"""
    if not c.get("tod_filter", True):
        return ""
    b = tod_bucket(minutes)
    s = _cached(("tod", market), 600, lambda: tod_stats(market)) or {}
    x = s.get(b) or {}
    if x.get("n", 0) >= 10 and x["win_rate"] < 35 and x["avg"] < 0:
        return f"🕐 '{b}' 시간대 최근 {x['n']}건 승률 {x['win_rate']}% · 평균 {x['avg']:+.2f}% → 이 시간대엔 급등주 안 삼"
    return ""


# ── 3. 업종 쏠림 ──
def sector(sym: str) -> str:
    def fn():
        info = toss.stock_info(sym) or {}
        for k in ("sector", "sectorName", "industry", "industryName", "category", "sectorCode"):
            if info.get(k):
                return str(info[k])
        return ""
    return _cached(("sec", sym), 86400, fn) or ""


def sector_block(sym: str, held: list[str], c: dict) -> str:
    cap = int(c.get("max_per_sector", 3) or 0)
    if cap <= 0:
        return ""
    sec = sector(sym)
    if not sec:
        return ""
    n = sum(1 for h in held if h != sym and sector(h) == sec)
    return f"🧺 같은 업종({sec[:20]}) 이미 {n}종목 보유 → 쏠림 방지로 안 삼 (한도 {cap})" if n >= cap else ""


# ── 4. 호가 스프레드 (가상 체결을 현실에 가깝게) ──
def spread_pct(sym: str) -> float | None:
    """매수1·매도1 호가 차이(%) — 1분 보관. 호가 형식을 모르면 None (그땐 기본 미끄러짐만)"""
    def fn():
        ob = toss._get("/api/v1/orderbook", symbol=sym) or {}
        def first(side_keys, price_keys=("price", "p")):
            for k in side_keys:
                v = ob.get(k)
                if isinstance(v, list) and v:
                    x = v[0]
                    if isinstance(x, dict):
                        for pk in price_keys:
                            if x.get(pk):
                                return float(x[pk])
                    elif isinstance(x, (int, float, str)):
                        return float(x)
                elif isinstance(v, (int, float, str)) and str(v):
                    return float(v)
            return None
        ask = first(("asks", "askOrders", "sell", "bestAsk", "askPrice"))
        bid = first(("bids", "bidOrders", "buy", "bestBid", "bidPrice"))
        if not ask or not bid or ask <= bid:
            return None
        return round((ask / bid - 1) * 100, 3)
    return _cached(("spr", sym), 60, fn)


def slip_cfg(cfg: dict, sym: str | None = None, spread: float | None = None) -> dict:
    """가상 체결 미끄러짐 = max(기본, 스프레드의 절반)"""
    sp = spread if spread is not None else (spread_pct(sym) if sym else None)
    if not sp:
        return cfg
    return {**cfg, "sim_slip_pct": max(float(cfg.get("sim_slip_pct", 0.1)), sp / 2)}


# ── 5. 토스 장애 ──
_fail: dict[str, list] = {}


def toss_fail(market: str, err) -> str:
    """연속 실패 기록 → 3번째에 한 번 알림 · 장애 중엔 새 매수 멈춤 (매도는 계속 시도)"""
    f = _fail.setdefault(market, [0, 0.0, False])
    f[0] += 1
    f[1] = time.time()
    if f[0] >= 3 and not f[2]:
        f[2] = True
        msg = f"{'🇺🇸' if market == 'US' else '🇰🇷'} ⚠️ 토스 조회가 {f[0]}번 연속 실패 — 새 매수는 멈추고 들고 있는 종목 확인은 계속 시도해요 ({str(err)[:80]})"
        try:
            from . import notify
            notify.send(msg)
        except Exception:
            pass
        return msg
    return ""


def toss_ok(market: str) -> str:
    f = _fail.get(market)
    if f and f[2]:
        _fail[market] = [0, 0.0, False]
        msg = f"{'🇺🇸' if market == 'US' else '🇰🇷'} ✅ 토스 연결 회복 — 다시 정상 매매"
        try:
            from . import notify
            notify.send(msg)
        except Exception:
            pass
        return msg
    if f:
        f[0] = 0
    return ""


def degraded(market: str) -> bool:
    f = _fail.get(market)
    return bool(f and f[2])


# ── 매수 관문 (위 1·2·3·5 를 한 번에) ──
def buy_gate(sym: str, style: str, minutes, market: str, held: list[str], c: dict) -> tuple[float, str]:
    """(금액 배수, 이유) — 배수 0 이면 안 삼. 파는 건 여기 안 거침"""
    if degraded(market):
        return 0.0, "⚠️ 토스 연결이 불안정해서 새로 안 삼"
    try:
        why = sector_block(sym, held, c)
        if why:
            return 0.0, why
    except Exception:
        pass
    if style != "surge":
        return 1.0, ""
    why = tod_block(market, minutes, c)
    if why:
        return 0.0, why
    return regime(market, c)


# ── 6. 백업 ──
def backup_dir(c: dict) -> Path:
    d = str(c.get("backup_dir") or "").strip()
    return Path(d) if d else Path.home() / "Documents" / "자동매매_백업"


def daily_backup(c: dict, log=print) -> str:
    """하루 한 번 _cache 를 zip 으로 (1분봉 보관·임시 작업·토큰 제외) · 14개 보관. Documents 가 OneDrive 면 클라우드에도 올라감"""
    if not c.get("backup_on", True):
        return ""
    day = datetime.now(KST).strftime("%Y%m%d")
    marker = COMPANY_DIR / "_cache" / "last_backup.txt"
    try:
        if marker.read_text().strip() == day:
            return ""
    except OSError:
        pass
    dest = backup_dir(c)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"자동매매_{day}.zip"
        src = COMPANY_DIR / "_cache"
        skip_dirs = {"bt", "improve", "backups", "uploads", "charts"}
        skip_files = {toss.TOKEN_CACHE.name, "app.lock", "last_backup.txt"}   # 토스 로그인 토큰은 백업 안 함
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for f in src.rglob("*"):
                rel = f.relative_to(src)
                if f.is_file() and not (set(rel.parts[:-1]) & skip_dirs) and f.name not in skip_files and not f.name.endswith((".tmp", ".png")):
                    try:
                        z.write(f, rel.as_posix())
                    except OSError:
                        pass
        for old in sorted(dest.glob("자동매매_*.zip"))[:-14]:
            old.unlink(missing_ok=True)
        marker.write_text(day)
        msg = f"💾 학습·계좌 기록 백업: {out}"
        log(msg)
        return msg
    except OSError as e:
        return f"💾 백업 실패: {type(e).__name__}: {e}"


# ── 7. 헬스체크 (PC·인터넷이 꺼지면 폰으로 알림 — 외부 감시 서비스) ──
_hb = [0.0]


def heartbeat() -> None:
    """🧰 도구의 HEALTHCHECK_URL 로 5분마다 신호 (예: healthchecks.io 무료 · 신호가 끊기면 그 서비스가 폰·메일로 알림)"""
    url = os.getenv("HEALTHCHECK_URL", "").strip()
    if not url.startswith("https://") or time.time() - _hb[0] < 300:
        return
    _hb[0] = time.time()
    def go():
        try:
            urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "CompanyAI-heartbeat"}), timeout=10).read()
        except Exception:
            pass
    threading.Thread(target=go, daemon=True, name="heartbeat").start()


# ── 8. 성적표 (최대 낙폭 · 시장 대비 · 체결률 · AI 판단 · 세금 · 환율) ──
def _month_start(market: str) -> str:
    return datetime.now(KST).strftime("%Y-%m-01")


def index_return(market: str, since: str) -> float | None:
    def fn():
        closes, _, dates = toss.daily(INDEX[market], 40)
        base = next((c for c, d in zip(closes, dates) if d[:10] >= since), None)
        idx = next((i for i, d in enumerate(dates) if d[:10] >= since), None)
        if idx is None:
            return None
        prev = closes[idx - 1] if idx > 0 else base
        px = (toss.prices([INDEX[market]]).get(INDEX[market]) or {}).get("price") or closes[-1]
        return round((float(px) / prev - 1) * 100, 2) if prev else None
    return _cached(("idxr", market, since), 900, fn)


def _krw(t: dict, fx: float) -> float:
    r = float(t.get("realized") or 0)
    return r * (float(t.get("fx_close") or fx) if tl._tm(t) == "US" else 1)


def scorecard(market: str, mode: str, fx: float, month_start_krw: float, state_orders: list[dict]) -> dict:
    since = _month_start(market)
    with tl._lock:
        all_ts = [t for t in tl._load()["trades"] if t["closed"] and tl._tm(t) == market and t["mode"] == mode]
    ts = [t for t in all_ts if t["day"] >= since[:10]]
    # 최대 낙폭: 이번 달 끝난 거래 손익(원)을 시간순으로 쌓은 곡선의 최고점 대비 최대 하락
    cum = peak = mdd = 0.0
    for t in sorted(ts, key=lambda t: t["exits"][-1]["t"] if t.get("exits") else t["entry_time"]):
        cum += _krw(t, fx)
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    base = month_start_krw or 0
    my_pct = round(cum / base * 100, 2) if base else None
    idx = index_return(market, since)
    # 실전 체결률 (지정가가 실제로 체결됐나)
    live = [o for o in state_orders if o.get("mode") == "live" and o.get("status") not in (None, "rejected")]
    filled = [o for o in live if o.get("status") in ("filled", "partial_filled", "partially_filled") or float(o.get("counted") or 0) > 0]
    done = [o for o in live if o.get("status") in ("filled", "canceled", "cancelled", "partial_filled", "partially_filled")]
    fill_rate = round(len(filled) / len(done) * 100) if done else None
    # AI 판단 성적: AI 가 사자고 한 거래 vs 그 밖 거래
    ai = [t for t in all_ts[-200:] if (t.get("features") or {}).get("ai_decide") == "산다" or (t.get("features") or {}).get("ai_chart") == "찬성"]
    rest = [t for t in all_ts[-200:] if t not in ai]
    out = {"since": since, "n": len(ts), "realized_krw": round(cum), "my_pct": my_pct, "mdd_krw": round(mdd),
           "mdd_pct": round(mdd / base * 100, 2) if base else None, "index": INDEX_NAME[market], "index_pct": idx,
           "vs_index": round(my_pct - idx, 2) if my_pct is not None and idx is not None else None,
           "fill_rate": fill_rate, "fill_n": len(done), "ai": tl._stats(ai), "no_ai": tl._stats(rest), "tod": tod_stats(market)}
    if market == "US":   # 미장 양도세: 연 250만 원 공제 넘는 부분 22%
        year = datetime.now(KST).strftime("%Y")
        yt = [t for t in all_ts if t["day"][:4] == year and t["mode"] == "live"]
        gain = sum(_krw(t, fx) for t in yt)
        fx_eff = sum(float(t.get("qty_in") or 0) * float(t["exits"][-1]["px"]) * (float(t.get("fx_close") or fx) - float((t.get("features") or {}).get("fx") or fx))
                     for t in yt if t.get("exits") and (t.get("features") or {}).get("fx"))
        out.update(tax_gain_krw=round(gain), tax_free_krw=2_500_000, tax_est_krw=round(max(0.0, gain - 2_500_000) * 0.22),
                   fx_effect_krw=round(fx_eff))
    return out


def scorecard_text(market: str, sc: dict) -> str:
    flag = "🇺🇸 미장" if market == "US" else "🇰🇷 국장"
    L = [f"{flag} 이번 달 성적 ({sc['since'][5:]}~): 끝난 거래 {sc['n']}건 · 실현 {sc['realized_krw']:+,}원"
         + (f" ({sc['my_pct']:+.2f}%)" if sc.get("my_pct") is not None else "")
         + (f" · 최대 낙폭 -{sc['mdd_krw']:,}원" + (f" (-{sc['mdd_pct']:.2f}%)" if sc.get("mdd_pct") is not None else "") if sc["mdd_krw"] else "")]
    if sc.get("index_pct") is not None:
        L.append(f"  시장 대비: {sc['index']} {sc['index_pct']:+.2f}%" + (f" → 자동매매가 {sc['vs_index']:+.2f}%p" if sc.get("vs_index") is not None else ""))
    if sc.get("fill_rate") is not None:
        L.append(f"  실전 지정가 체결률 {sc['fill_rate']}% ({sc['fill_n']}건)")
    a, b = sc["ai"], sc["no_ai"]
    if a.get("n") or b.get("n"):
        L.append("  AI 판단: " + (f"AI 가 산 거래 {a['n']}건 승률 {a['win_rate']}% 평균 {a['avg']:+.2f}%" if a.get("n") else "AI 거래 없음")
                 + (f" / 그 밖 {b['n']}건 승률 {b['win_rate']}% 평균 {b['avg']:+.2f}%" if b.get("n") else ""))
    tod = {k: v for k, v in (sc.get("tod") or {}).items() if v.get("n")}
    if tod:
        L.append("  시간대: " + " · ".join(f"{k} {v['n']}건 {v['avg']:+.2f}%" for k, v in tod.items()))
    if "tax_gain_krw" in sc:
        L.append(f"  올해 미장 실현 이익 {sc['tax_gain_krw']:+,}원 / 공제 250만 원 → 예상 양도세 {sc['tax_est_krw']:,}원"
                 + (f" · 환율 효과 {sc['fx_effect_krw']:+,}원" if sc.get("fx_effect_krw") else ""))
    return "\n".join(L)


# ── 9. 장 마감 뒤: 내일 계획 · 주간 성적표 · 연말 세금 알림 ──
def after_review(market: str, mode: str, fx: float, s: dict, c: dict) -> list[str]:
    from . import notify
    msgs = []
    flag = "🇺🇸 미장" if market == "US" else "🇰🇷 국장"
    pos = s["bot_pos"] if mode == "live" else s["paper"]["positions"]
    carry = [f"{k} {float(v['qty']):g}주" for k, v in pos.items() if (s.get("style") or {}).get(k) == "swing" and float(v.get("qty") or 0) > 0]
    sw = [f"{p.get('name') or p['symbol']}" for p in ((s.get("swing") or {}).get("picks") or [])[:5]]
    notes = tl.lessons_text(market, 3)
    if c.get("tomorrow_plan", True):
        m = (f"🗓️ {flag} 내일 계획\n들고 가는 스윙: {', '.join(carry) or '없음'}\n내일 볼 스윙 후보: {', '.join(sw) or '장 전에 새로 골라요'}"
             + (f"\n오늘 교훈: {notes}" if notes else ""))
        notify.send(m); msgs.append(m)
    local = datetime.now(KST)
    if c.get("weekly_report", True) and local.weekday() == (5 if market == "US" else 4):   # 국장 금요일 · 미장 토요일 아침(한국)
        sc = scorecard(market, mode, fx, float((s.get("month") or {}).get("start") or 0), s.get("orders", []))
        m = "📊 주간 성적표\n" + scorecard_text(market, sc)
        notify.send(m); msgs.append(m)
    if market == "US" and local.month == 12 and local.day >= 10 and mode == "live":
        key = f"tax_notice_{local.year}"
        if not s.get(key):
            sc = scorecard(market, mode, fx, 0, [])
            if sc.get("tax_gain_krw", 0) > 2_500_000:
                losers = [k for k, v in pos.items() if v.get("avg")]
                m = (f"🧾 올해 미장 실현 이익 {sc['tax_gain_krw']:,}원 — 공제 250만 원을 넘어서 예상 양도세 {sc['tax_est_krw']:,}원\n"
                     f"손실 중인 종목을 연말 전에 정리하면 세금이 줄 수 있어요 (보유: {', '.join(losers[:6]) or '없음'}) · 판단은 사장님이")
                notify.send(m); msgs.append(m)
            s[key] = True
    return msgs


# ── 10. 자동 개선 효과 확인 (매매 코드가 바뀐 뒤 성적이 나빠지면 되돌림) ──
def improve_effect(log=print, restart=None) -> str:
    from . import improve as im
    rows = [r for r in im.history() if r.get("status") == "applied" and r.get("applied") and not r.get("effect")
            and any(f.get("trading") for f in (r.get("files") or []))]
    if not rows:
        return ""
    job = sorted(rows, key=lambda r: r["applied"])[-1]
    t0 = job["applied"].replace("T", " ")[:19]
    with tl._lock:
        ts = [t for t in tl._load()["trades"] if t["closed"] and t.get("exits")]
    before = [t for t in ts if t["exits"][-1]["t"] < t0][-20:]
    after = [t for t in ts if t["exits"][-1]["t"] >= t0]
    if len(after) < 10 or len(before) < 10:
        return ""
    ba, aa = st.mean(t["pnl_pct"] for t in before), st.mean(t["pnl_pct"] for t in after)
    ev = f"바뀌기 전 {len(before)}건 평균 {ba:+.2f}% → 바뀐 뒤 {len(after)}건 평균 {aa:+.2f}%"
    if aa < ba - 0.5 and aa < 0:
        r = im.rollback(job["id"])
        im._update(job["id"], effect=f"나빠짐 → 자동 되돌림 ({ev})")
        msg = f"🛟 자동 개선 '{job['request'][:50]}' 뒤로 성적이 나빠져서 되돌렸어요 — {ev}"
        try:
            from . import notify
            notify.send(msg)
        except Exception:
            pass
        log(msg)
        if r.get("ok") and restart:
            restart()
        return msg
    im._update(job["id"], effect=f"유지 ({ev})")
    return f"✅ 자동 개선 '{job['request'][:40]}' 효과 확인: {ev} → 유지"


# ── 11. 그림자 교체 기준: 실전 조건에 맞는 거래만 · 조합이 많을수록 더 큰 차이 요구 ──
def promote_edge(n_variants: int) -> float:
    """조합이 많으면 운으로 1등이 나오기 쉬워서, '현재'보다 이만큼(%p) 이상 나아야 교체"""
    return round(0.2 + 0.1 * math.log2(max(1.0, n_variants / 10)), 2)
