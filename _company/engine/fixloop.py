"""🔧 손실 원인별로 규칙 하나씩 고쳐 보기 (주말 공부 때) — 무작정 조합을 많이 돌리는 대신, 왜 졌는지부터 보고 그 원인에 맞는 수정만 시험.

1. 최근 30일 고른 단타 종목(산 것·못 산 것)의 1분봉 경로로 '지금 실전 규칙'을 돌려 봄
2. 진 거래를 원인별로 묶음: 사자마자 떨어짐 · 이익을 돌려줌 · 늦은 시간에 삼 · 거래량 약함 · 손절 폭 큼
3. 손실이 가장 큰 원인에 맞는 수정 후보를 **하나씩만** 앞쪽 70% 날짜로 시험 → 가장 나아진 하나를 채택 → 다시 1 (최대 3바퀴)
4. 뒤쪽 30% 날짜(처음 보는 날)로 원래 규칙 vs 고친 규칙을 한 번 비교 → 나으면 그림자 매매에 넣음 (실전엔 바로 안 넣음 · 그림자에서 이기면 자동 교체)
5. 안 통한 수정은 _cache/failure_journal.jsonl 에 기록 → 30일 동안은 다시 시험 안 함
"""

from __future__ import annotations

import json
import statistics as st
from datetime import datetime, timedelta, timezone

from . import backtest as bt
from . import picker, toss
from . import tradelearn as tl
from .agents import COMPANY_DIR

KST = timezone(timedelta(hours=9))
JOURNAL = COMPANY_DIR / "_cache" / "failure_journal.jsonl"
KEYS = ("stop_loss_pct", "partial_tp_pct", "trail_pct", "surge_min_change_pct", "surge_volume_ratio", "pre_volume_ratio",
        "time_stop_min", "surge_min_change_regular", "surge_buy_cutoff_min", "take_profit_pct", "surge_max_change_pct")


def _load_paths(market: str, days: int, log=print) -> list[tuple[dict, list[dict]]]:
    since = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    j = tl._load()
    samples = []
    for key, w in j["watch"].items():
        mk, _, day = key.partition(":")
        if not day or mk != market or day < since:
            continue
        for sym, x in w.items():
            if x.get("pick_px") and x.get("t"):
                t = bt._parse_kst(x["t"])
                samples.append({"sym": sym, "day": day, "t": x["t"], "px": x["pick_px"], "change": x.get("change") or 0,
                                "pre": bool(x.get("pre")), "ratio": x.get("ratio"),
                                "minute": picker.session_minutes(market, t, extended=True)})
    samples = sorted(samples, key=lambda s: s["t"])[-150:]
    log(f"🔧 {'미장' if market == 'US' else '국장'} 원인별 수정 시험: 표본 {len(samples)}개 1분봉 준비")
    out = []
    for s in samples:
        try:
            p = bt._minute_path(s["sym"], s["day"], bt._parse_kst(s["t"]), market)
        except (toss.TossError, ValueError):
            p = []
        if p:
            out.append((s, p))
    return out


def _passes(s: dict, prm: dict) -> bool:
    """이 규칙의 진입 기준으로 이 종목을 샀을까"""
    if s["change"] < prm["surge_min_change_pct"] or s["change"] > prm.get("surge_max_change_pct", 1e9):
        return False
    need = prm["pre_volume_ratio"] if s["pre"] else prm["surge_volume_ratio"]
    if s.get("ratio") is not None and s["ratio"] < need:
        return False
    m = s.get("minute")
    if not s["pre"] and m is not None and m >= 0:
        if s["change"] < float(prm.get("surge_min_change_regular") or 0):
            return False
        if prm.get("surge_buy_cutoff_min") and m > float(prm["surge_buy_cutoff_min"]):
            return False
    return True


def _trades(paths, prm: dict, cfg: dict, signal_surge, market: str) -> list[dict]:
    cost = tl.rt_cost(cfg, market)
    out = []
    for s, path in paths:
        if not _passes(s, prm):
            continue
        info: dict = {}
        r = bt._sim_surge(path, s["px"], prm, cfg, signal_surge, market, info=info)
        if r is not None:
            out.append({**s, "ret": r - cost, "mfe": info.get("mfe", 0.0), "why": str(info.get("why") or ""), "hold": info.get("hold", 0.0)})
    return out


def _score(ts: list[dict]) -> dict:
    if not ts:
        return {"n": 0, "total": 0.0, "avg": 0.0, "win": 0}
    rs = [t["ret"] for t in ts]
    return {"n": len(rs), "total": round(sum(rs), 2), "avg": round(st.fmean(rs), 2), "win": round(sum(r > 0 for r in rs) / len(rs) * 100)}


CAUSES = {   # 원인 → (판단, 수정 후보 만들기)
    "사자마자 떨어짐": (lambda t, p: t["mfe"] < 0.5,
                   lambda p: [("time_stop_min", 10), ("time_stop_min", 5), ("surge_min_change_regular", p.get("surge_min_change_regular", 12) + 4)]),
    "이익을 돌려줌": (lambda t, p: t["mfe"] >= 3,
                  lambda p: [("partial_tp_pct", 3), ("trail_pct", max(2, p["trail_pct"] - 1)), ("take_profit_pct", max(5, p.get("take_profit_pct", 10) - 3))]),
    "늦은 시간에 삼": (lambda t, p: (t.get("minute") or 0) > 120,
                   lambda p: [("surge_buy_cutoff_min", 120), ("surge_buy_cutoff_min", 60)]),
    "거래량 약함": (lambda t, p: not t["pre"] and (t.get("ratio") or 0) < 5,
                 lambda p: [("surge_volume_ratio", p["surge_volume_ratio"] + 1), ("surge_volume_ratio", p["surge_volume_ratio"] + 2)]),
    "손절 폭 큼": (lambda t, p: "손절" in t["why"] and t["ret"] < -(p["stop_loss_pct"] + 1),
                lambda p: [("stop_loss_pct", max(1.5, p["stop_loss_pct"] - 0.5)), ("stop_loss_pct", max(1.5, p["stop_loss_pct"] - 1))]),
}


def causes(ts: list[dict], prm: dict) -> list[dict]:
    losers = [t for t in ts if t["ret"] < 0]
    total = -sum(t["ret"] for t in losers) or 1
    out = []
    for name, (is_c, _) in CAUSES.items():
        g = [t for t in losers if is_c(t, prm)]
        if g:
            out.append({"cause": name, "n": len(g), "loss_share": round(-sum(t["ret"] for t in g) / total * 100)})
    return sorted(out, key=lambda x: -x["loss_share"])


def _journal() -> list[dict]:
    try:
        return [json.loads(x) for x in JOURNAL.read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        return []


def _failed_recently(market: str, k: str, v) -> bool:
    since = (datetime.now(KST) - timedelta(days=30)).isoformat()
    return any(x.get("market") == market and x.get("param") == k and float(x.get("value", -1e9)) == float(v) and x.get("t", "") >= since
               for x in _journal())


def _log_fail(market: str, cause: str, k: str, v, why: str) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": datetime.now(KST).isoformat(timespec="seconds"), "market": market, "cause": cause,
                            "param": k, "value": v, "why": why}, ensure_ascii=False) + "\n")


def run(cfg: dict, market: str, signal_surge, log=print, days: int = 30, rounds: int = 3) -> dict:
    paths = _load_paths(market, days, log)
    if len(paths) < 15:
        return {"note": f"표본이 {len(paths)}개뿐이라 원인별 수정 시험은 15개↑부터 해요", "seed": None}
    days_ = sorted({s["day"] for s, _ in paths})
    cut = days_[int(len(days_) * 0.7)] if len(days_) >= 4 else days_[-1]
    train = [x for x in paths if x[0]["day"] < cut]
    test = [x for x in paths if x[0]["day"] >= cut]
    orig = {k: cfg.get(k) for k in KEYS}
    cur = dict(orig)
    rows, n_fail, n_skip = [], 0, 0
    base = _score(_trades(train, cur, cfg, signal_surge, market))
    for rd in range(1, rounds + 1):
        ts = _trades(train, cur, cfg, signal_surge, market)
        cs = [c for c in causes(ts, cur) if c["loss_share"] >= 15]
        if not cs:
            break
        top = cs[0]
        before = _score(ts)
        best = None
        for k, v in CAUSES[top["cause"]][1](cur):
            if cur.get(k) == v:
                continue
            if _failed_recently(market, k, v):
                n_skip += 1
                continue
            sc = _score(_trades(train, {**cur, k: v}, cfg, signal_surge, market))
            if sc["n"] >= 5 and sc["total"] > before["total"] + 0.5 and (best is None or sc["total"] > best[2]["total"]):
                best = (k, v, sc)
            elif sc["n"] >= 1:
                _log_fail(market, top["cause"], k, v, f"앞쪽 기간 총 {sc['total']:+.1f}% (전 {before['total']:+.1f}%) · {sc['n']}건")
                n_fail += 1
        if not best:
            rows.append({"round": rd, "cause": top, "fix": None, "before": before})
            break
        k, v, sc = best
        rows.append({"round": rd, "cause": top, "fix": f"{k} {cur.get(k)} → {v}", "before": before, "after": sc})
        cur[k] = v
    changed = {k: v for k, v in cur.items() if v != orig.get(k)}
    t0, t1 = _score(_trades(test, orig, cfg, signal_surge, market)), _score(_trades(test, cur, cfg, signal_surge, market))
    ok = bool(changed) and t1["n"] >= 3 and t1["total"] > t0["total"]
    if changed and not ok:
        for k, v in changed.items():
            _log_fail(market, "처음 보는 날 검증", k, v, f"검증 기간 총 {t1['total']:+.1f}% vs 원래 {t0['total']:+.1f}%")
            n_fail += 1
    verdict = ("고친 규칙이 처음 보는 날에서도 나았어요 → 그림자 매매에 넣어 실전과 계속 비교해요" if ok else
               ("고친 규칙이 처음 보는 날에선 원래보다 못해서 버렸어요 (실패 기록에 남김)" if changed else "고칠 만한 수정을 못 찾았어요"))
    out = {"t": datetime.now(KST).isoformat(timespec="seconds"), "market": market, "n": len(paths), "train_first": base, "rounds": rows,
           "changed": changed, "test_orig": t0, "test_new": t1, "verdict": verdict, "failed_logged": n_fail, "skipped_known_fail": n_skip,
           "seed": changed if ok else None}
    (COMPANY_DIR / "_cache" / f"fixloop_{market}.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def text(r: dict) -> str:
    if not r.get("rounds") and not r.get("changed"):
        return "🔧 원인별 수정: " + (r.get("note") or r.get("verdict", ""))
    L = [f"🔧 원인별 수정 ({r['n']}개 표본): 처음 {r['train_first']['total']:+.1f}% ({r['train_first']['n']}건 · 승률 {r['train_first']['win']}%)"]
    for x in r["rounds"]:
        c = x["cause"]
        L.append(f"  {x['round']}바퀴: 가장 큰 손실 원인 '{c['cause']}'({c['loss_share']}%) → "
                 + (f"{x['fix']} → 총 {x['after']['total']:+.1f}%" if x.get("fix") else "나아지는 수정 없음"))
    L.append(f"  처음 보는 날: 원래 {r['test_orig']['total']:+.1f}% vs 고친 것 {r['test_new']['total']:+.1f}% → {r['verdict']}")
    L.append(f"  실패 기록 {r['failed_logged']}건 추가 · 이미 실패해서 건너뛴 것 {r['skipped_known_fail']}건")
    return "\n".join(L)
