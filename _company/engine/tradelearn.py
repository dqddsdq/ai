"""📚 자동매매 학습 — 장중 기록·장중 조심 규칙·장 마감 후 복기·하루마다 규칙 조정 제안.

장중 (실시간)
- 거래 일지: 산 종목마다 산 뒤 최대 상승(MFE)·최대 하락(MAE)을 계속 갱신, 매수 시점 조건(상승률·거래량 배수·프리장 여부·시간) 기록
- 놓친 종목: 골랐지만 못 산 종목이 그 뒤 얼마나 움직였는지 추적
- 조심 규칙 (자동, 더 조심하는 방향만): 연속 손절 N번 → 잠시 매수 쉬기 / 오늘 프리장 매수가 2번 다 손실 → 오늘 프리장 매수 중단

장 마감 후
- 복기: 승률·평균 수익/손실·조건별 성적·놓친 종목 → AI 가 쉬운 말로 요약
- 조정 제안: 최근 거래(최대 30건)를 근거로 숫자 규칙 변경을 제안 → 사장님이 [적용]해야 바뀜 · [되돌리기] 가능

기록: _cache/trade_journal.json (git 에 안 올라감). 자기 개선 기능은 이 파일을 못 고침.
"""

from __future__ import annotations

import json
import statistics as st
import threading
import uuid
from datetime import datetime, timedelta, timezone

from .agents import COMPANY_DIR

FILE = COMPANY_DIR / "_cache" / "trade_journal.json"
KST = timezone(timedelta(hours=9))
_lock = threading.RLock()
TUNABLE = {"partial_tp_pct": "나눠 팔기 수익률(%)", "stop_loss_pct": "손절(%)", "trail_pct": "고점 대비 하락 매도(%)",
           "pre_volume_ratio": "프리장 거래량 기준", "us_premarket": "프리장 매매", "surge_min_change_pct": "급등 최소 상승률(%)",
           "surge_max_change_pct": "급등 최대 상승률(%)", "surge_volume_ratio": "거래량 배수 기준"}
MIN_TRADES = 5          # 이보다 적으면 규칙 조정 제안 안 함 (우연일 수 있어서)


def _load() -> dict:
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    for k, v in (("trades", []), ("watch", {}), ("reviews", []), ("proposals", []), ("guard", {})):
        d.setdefault(k, v)
    return d


def _save(d: dict) -> None:
    d["trades"] = d["trades"][-500:]
    d["reviews"] = d["reviews"][-60:]
    d["proposals"] = d["proposals"][-100:]
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def _now() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def _pnl(t: dict) -> float:
    q = sum(x["qty"] for x in t["exits"])
    return sum(x["qty"] * (x["px"] / t["entry_px"] - 1) for x in t["exits"]) / q * 100 if q and t["entry_px"] else 0.0


# ── 장중 기록 ──
def open_trade(sym: str, qty: float, px: float, features: dict | None, mode: str, day: str) -> None:
    with _lock:
        d = _load()
        t = next((x for x in d["trades"] if x["sym"] == sym and not x["closed"] and x["mode"] == mode), None)
        if t:   # 같은 종목 추가 매수 → 평단 합치기
            tot = t["qty_in"] + qty
            t["entry_px"] = (t["entry_px"] * t["qty_in"] + px * qty) / tot
            t["qty_in"] = tot
        else:
            d["trades"].append({"id": uuid.uuid4().hex[:10], "day": day, "sym": sym, "mode": mode, "entry_time": _now(),
                                "entry_px": px, "qty_in": qty, "features": features or {}, "mfe": 0.0, "mae": 0.0,
                                "exits": [], "closed": False})
        d["watch"].get(day, {}).get(sym, {}).update(bought=True)
        _save(d)


def mark(prices: dict[str, float], mode: str) -> None:
    """보유 중인 거래의 최대 상승·하락 갱신 (10초마다)"""
    with _lock:
        d = _load()
        changed = False
        for t in d["trades"]:
            if t["closed"] or t["mode"] != mode or t["sym"] not in prices or not t["entry_px"]:
                continue
            r = (prices[t["sym"]] / t["entry_px"] - 1) * 100
            if r > t["mfe"]:
                t["mfe"] = round(r, 2); changed = True
            if r < t["mae"]:
                t["mae"] = round(r, 2); changed = True
        if changed:
            _save(d)


def on_sell(sym: str, qty: float, px: float, reason: str, mode: str, closed_all: bool, cfg: dict) -> None:
    with _lock:
        d = _load()
        t = next((x for x in d["trades"] if x["sym"] == sym and not x["closed"] and x["mode"] == mode), None)
        if not t:
            return
        t["exits"].append({"t": _now(), "qty": qty, "px": px, "reason": reason})
        r = (px / t["entry_px"] - 1) * 100 if t["entry_px"] else 0
        t["mfe"], t["mae"] = max(t["mfe"], round(r, 2)), min(t["mae"], round(r, 2))
        if closed_all:
            t["closed"] = True
            t["pnl_pct"] = round(_pnl(t), 2)
            t["exit_kind"] = _kind(t["exits"][-1]["reason"])
            try:
                t["hold_min"] = round((datetime.strptime(t["exits"][-1]["t"], "%Y-%m-%d %H:%M:%S") -
                                       datetime.strptime(t["entry_time"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60)
            except ValueError:
                t["hold_min"] = None
            _update_guard(d, t, cfg)
        _save(d)


def _kind(reason: str) -> str:
    for key, name in (("손절", "손절"), ("본전", "본전 정리"), ("익절", "익절"), ("고점 대비", "수익 지키기"), ("마감", "마감 정리")):
        if key in reason:
            return name
    return "기타"


def _update_guard(d: dict, t: dict, cfg: dict) -> None:
    """장중 조심 규칙 (자동): 연속 손실 → 잠시 쉬기 / 오늘 프리장 매수 2번 다 손실 → 오늘 프리장 매수 중단"""
    g = d["guard"]
    if g.get("day") != t["day"]:
        g.clear(); g["day"] = t["day"]
    if t["pnl_pct"] < 0:
        g["streak"] = g.get("streak", 0) + 1
    else:
        g["streak"] = 0
    if g["streak"] >= int(cfg.get("loss_streak_pause", 3)):
        until = datetime.now(KST) + timedelta(minutes=int(cfg.get("pause_min", 30)))
        g["pause_until"] = until.strftime("%Y-%m-%d %H:%M:%S")
        g["pause_why"] = f"{g['streak']}번 연속 손실 → {cfg.get('pause_min', 30)}분 매수 쉬기"
        g["streak"] = 0
    if t.get("features", {}).get("pre"):
        pre = [x for x in d["trades"] if x["day"] == t["day"] and x["closed"] and x.get("features", {}).get("pre")]
        if len(pre) >= 2 and all(x["pnl_pct"] < 0 for x in pre):
            g["no_premarket"] = f"오늘 프리장 매수 {len(pre)}번 모두 손실 → 오늘은 프리장 매수 중단"


def guard(day: str, premarket_now: bool) -> str:
    """지금 매수를 쉬어야 하면 이유, 아니면 ''"""
    with _lock:
        g = _load()["guard"]
    if g.get("day") != day:
        return ""
    if g.get("pause_until") and _now() < g["pause_until"]:
        return g.get("pause_why", "잠시 쉬기") + f" (~{g['pause_until'][11:16]})"
    if premarket_now and g.get("no_premarket"):
        return g["no_premarket"]
    return ""


def watch(day: str, picks: list[dict], prices: dict[str, float]) -> None:
    """고른 종목(산 것·못 산 것 모두)의 이후 움직임 기록"""
    with _lock:
        d = _load()
        w = d["watch"].setdefault(day, {})
        for p in picks:
            w.setdefault(p["symbol"], {"pick_px": p["price"], "t": _now(), "max": p["price"], "min": p["price"], "bought": False,
                                      "change": p.get("change"), "pre": p.get("pre")})
        for sym, x in w.items():
            if sym in prices:
                x["max"], x["min"] = max(x["max"], prices[sym]), min(x["min"], prices[sym])
        for k in sorted(d["watch"])[:-5]:   # 최근 5일만 보관
            d["watch"].pop(k)
        _save(d)


def watch_symbols(day: str) -> list[str]:
    with _lock:
        return list(_load()["watch"].get(day, {}))[:30]


# ── 통계 ──
def _stats(ts: list[dict]) -> dict:
    if not ts:
        return {"n": 0}
    pn = [t["pnl_pct"] for t in ts]
    wins = [p for p in pn if p > 0]
    loss = [p for p in pn if p <= 0]
    return {"n": len(ts), "win_rate": round(len(wins) / len(ts) * 100), "avg": round(st.mean(pn), 2),
            "avg_win": round(st.mean(wins), 2) if wins else 0, "avg_loss": round(st.mean(loss), 2) if loss else 0,
            "sum": round(sum(pn), 2), "mfe_med": round(st.median([t["mfe"] for t in ts]), 2),
            "mae_med": round(st.median([t["mae"] for t in ts]), 2)}


def _bucket(ts: list[dict], key, edges: list[float], unit: str) -> dict:
    out = {}
    for lo, hi in zip(edges[:-1], edges[1:]):
        g = [t for t in ts if t.get("features", {}).get(key) is not None and lo <= t["features"][key] < hi]
        if g:
            out[f"{lo:g}~{hi:g}{unit}" if hi < 1e9 else f"{lo:g}{unit}+"] = _stats(g)
    return out


def today_stats(day: str, mode: str) -> dict:
    with _lock:
        d = _load()
    ts = [t for t in d["trades"] if t["day"] == day and t["closed"] and t["mode"] == mode]
    open_ = [t for t in d["trades"] if t["day"] == day and not t["closed"] and t["mode"] == mode]
    s = _stats(ts)
    s["open"] = [{"sym": t["sym"], "mfe": t["mfe"], "mae": t["mae"]} for t in open_]
    s["guard"] = d["guard"] if d["guard"].get("day") == day else {}
    return s


# ── 복기·제안 ──
def _propose(d: dict, cfg: dict, param: str, to, why: str, evidence: str) -> dict | None:
    cur = cfg.get(param)
    if cur == to or param not in TUNABLE:
        return None
    for p in d["proposals"]:   # 같은 규칙의 예전 대기 제안은 새 제안으로 교체
        if p["param"] == param and p["status"] == "pending":
            p["status"] = "replaced"
    p = {"id": uuid.uuid4().hex[:8], "param": param, "label": TUNABLE[param], "from": cur, "to": to, "why": why,
         "evidence": evidence, "created": _now(), "status": "pending"}
    d["proposals"].append(p)
    return p


def _r(x: float, step: float = 0.5) -> float:
    return round(round(x / step) * step, 2)


def daily_review(day: str, cfg: dict, mode: str, use_ai: bool = True) -> dict:
    """장 마감 후 복기 + 규칙 조정 제안. 같은 날 다시 부르면 덮어씀."""
    with _lock:
        d = _load()
    today = [t for t in d["trades"] if t["day"] == day and t["closed"] and t["mode"] == mode]
    recent = [t for t in d["trades"] if t["closed"] and t["mode"] == mode][-30:]
    w = d["watch"].get(day, {})
    missed = [(s, (x["max"] / x["pick_px"] - 1) * 100) for s, x in w.items() if not x.get("bought") and x.get("pick_px")]
    rep = {"day": day, "mode": mode, "created": _now(), "today": _stats(today), "recent": _stats(recent),
           "by_exit": {k: _stats([t for t in today if t.get("exit_kind") == k]) for k in {t.get("exit_kind") for t in today}},
           "by_change": _bucket(recent, "change", [0, 8, 12, 20, 1e9], "%"),
           "by_ratio": _bucket(recent, "ratio", [0, 3, 5, 10, 1e9], "배"),
           "by_session": {k: v for k, v in (("프리장", _stats([t for t in recent if t.get("features", {}).get("pre")])),
                                            ("정규장", _stats([t for t in recent if not t.get("features", {}).get("pre")]))) if v["n"]},
           "missed": {"n": len(missed), "avg_max_up": round(st.mean([m for _, m in missed]), 2) if missed else None,
                      "top": sorted(missed, key=lambda x: -x[1])[:3]},
           "best": max(today, key=lambda t: t["pnl_pct"], default=None), "worst": min(today, key=lambda t: t["pnl_pct"], default=None)}
    for k in ("best", "worst"):
        if rep[k]:
            t = rep[k]; rep[k] = {"sym": t["sym"], "pnl": t["pnl_pct"], "mfe": t["mfe"], "mae": t["mae"], "exit": t.get("exit_kind")}

    proposals = []
    with _lock:
        d = _load()
        if len(recent) >= MIN_TRADES:
            n = len(recent)
            mfes = sorted(t["mfe"] for t in recent)
            # 1) 나눠 팔기 수익률: 거의 안 닿으면 낮추고, 대부분 닿으면 높임
            if cfg.get("partial_tp_pct"):
                reach = sum(1 for m in mfes if m >= cfg["partial_tp_pct"]) / n
                if reach < 0.25 and st.median(mfes) >= 2:
                    to = max(3.0, min(cfg["partial_tp_pct"] - 0.5, _r(mfes[int(n * 0.6)])))
                    proposals.append(_propose(d, cfg, "partial_tp_pct", to, "나눠 팔기 가격에 닿는 거래가 너무 적어요",
                                              f"최근 {n}건 중 +{cfg['partial_tp_pct']}% 도달 {reach * 100:.0f}% · 산 뒤 최대 상승 중간값 {st.median(mfes):+.1f}%"))
                elif reach > 0.7:
                    proposals.append(_propose(d, cfg, "partial_tp_pct", min(cfg["take_profit_pct"] - 1, cfg["partial_tp_pct"] + 1),
                                              "대부분 나눠 팔기 가격을 넘어 더 올라요 — 조금 더 들고 가기",
                                              f"최근 {n}건 중 +{cfg['partial_tp_pct']}% 도달 {reach * 100:.0f}%"))
            # 2) 손절: 결국 수익 난 거래도 손절선 근처까지 자주 빠졌다면 → 손절이 너무 빡빡
            winners = [t for t in recent if t["pnl_pct"] > 0]
            if len(winners) >= 3:
                near = sum(1 for t in winners if t["mae"] <= -cfg["stop_loss_pct"] * 0.8) / len(winners)
                if near >= 0.3 and cfg["stop_loss_pct"] < 8:
                    proposals.append(_propose(d, cfg, "stop_loss_pct", _r(cfg["stop_loss_pct"] + 0.5), "수익 난 거래도 손절선 근처까지 자주 빠졌어요 — 조금 여유",
                                              f"수익 거래 {len(winners)}건 중 {near * 100:.0f}%가 -{cfg['stop_loss_pct'] * 0.8:.1f}% 이하까지 하락 후 반등"))
            # 3) 고점 대비 하락 매도: 최고점에서 너무 많이 돌려주면 → 더 빨리 팔기
            trail = [t for t in recent if t.get("exit_kind") == "수익 지키기"]
            if len(trail) >= 3:
                give = st.mean(t["mfe"] - t["pnl_pct"] for t in trail)
                if give > cfg["trail_pct"] * 1.2 and cfg["trail_pct"] > 2:
                    proposals.append(_propose(d, cfg, "trail_pct", _r(cfg["trail_pct"] - 0.5), "최고점에서 수익을 많이 돌려주고 팔고 있어요",
                                              f"'수익 지키기' 매도 {len(trail)}건 평균: 최고 +{st.mean(t['mfe'] for t in trail):.1f}% → 판 가격 {st.mean(t['pnl_pct'] for t in trail):+.1f}%"))
            # 4) 프리장 성적
            pre = [t for t in recent if t.get("features", {}).get("pre")]
            if len(pre) >= 3:
                s_ = _stats(pre)
                if s_["win_rate"] < 20 and len(pre) >= 5 and cfg.get("us_premarket"):
                    proposals.append(_propose(d, cfg, "us_premarket", False, "프리장 매수 성적이 계속 나빠요 — 정규장부터만",
                                              f"프리장 {len(pre)}건 승률 {s_['win_rate']}% · 평균 {s_['avg']:+.2f}%"))
                elif s_["win_rate"] < 35 and s_["avg"] < 0:
                    proposals.append(_propose(d, cfg, "pre_volume_ratio", round(min(5, cfg.get("pre_volume_ratio", 0.2) * 1.5), 2),
                                              "프리장은 거래가 더 많이 몰린 종목만", f"프리장 {len(pre)}건 승률 {s_['win_rate']}% · 평균 {s_['avg']:+.2f}%"))
            # 5) 매수 시점 상승률: 덜 오른 걸 산 게 나쁘면 기준 올리고, 너무 오른 걸 산 게 나쁘면 상한 내림
            ch = [t for t in recent if t.get("features", {}).get("change") is not None]
            if len(ch) >= 6:
                med = st.median(t["features"]["change"] for t in ch)
                low = [t["pnl_pct"] for t in ch if t["features"]["change"] < med]
                high = [t["pnl_pct"] for t in ch if t["features"]["change"] >= med]
                if low and high and st.mean(low) < 0 < st.mean(high) and st.mean(high) - st.mean(low) > 2:
                    proposals.append(_propose(d, cfg, "surge_min_change_pct", _r(max(cfg["surge_min_change_pct"] + 0.5, med)),
                                              "덜 오른 종목을 산 게 성적이 나빠요", f"상승률 {med:.1f}% 미만에서 산 거래 평균 {st.mean(low):+.2f}% vs 이상 {st.mean(high):+.2f}%"))
                elif low and high and st.mean(high) < 0 < st.mean(low) and st.mean(low) - st.mean(high) > 2:
                    proposals.append(_propose(d, cfg, "surge_max_change_pct", _r(max(cfg["surge_min_change_pct"] + 3, med), 1),
                                              "이미 너무 오른 종목을 산 게 성적이 나빠요", f"상승률 {med:.1f}% 이상에서 산 거래 평균 {st.mean(high):+.2f}% vs 미만 {st.mean(low):+.2f}%"))
            # 6) 거래량 배수
            rt = [t for t in recent if t.get("features", {}).get("ratio") is not None and not t["features"].get("pre")]
            if len(rt) >= 6:
                med = st.median(t["features"]["ratio"] for t in rt)
                low = [t["pnl_pct"] for t in rt if t["features"]["ratio"] < med]
                high = [t["pnl_pct"] for t in rt if t["features"]["ratio"] >= med]
                if low and high and st.mean(low) < 0 < st.mean(high) and st.mean(high) - st.mean(low) > 2:
                    proposals.append(_propose(d, cfg, "surge_volume_ratio", _r(max(cfg["surge_volume_ratio"] + 0.5, med)),
                                              "거래량이 덜 붙은 종목을 산 게 성적이 나빠요", f"거래량 {med:.1f}배 미만 평균 {st.mean(low):+.2f}% vs 이상 {st.mean(high):+.2f}%"))
        rep["proposals"] = [p["id"] for p in proposals if p]
        rep["note"] = "" if len(recent) >= MIN_TRADES else f"거래가 {len(recent)}건뿐이라 규칙 조정 제안은 {MIN_TRADES}건 이상 쌓이면 시작해요"
        d["reviews"] = [r for r in d["reviews"] if not (r["day"] == day and r["mode"] == mode)] + [rep]
        _save(d)

    if use_ai and (today or missed):
        rep["text"] = _ai_text(rep, [p for p in proposals if p])
        with _lock:
            d = _load()
            for r in d["reviews"]:
                if r["day"] == day and r["mode"] == mode:
                    r["text"] = rep["text"]
            _save(d)
    return rep


def _ai_text(rep: dict, proposals: list[dict]) -> str:
    from . import providers as pv
    from . import team as tm
    data = {k: rep[k] for k in ("today", "recent", "by_exit", "by_change", "by_ratio", "by_session", "missed", "best", "worst")}
    prompt = ("자동매매 하루 기록이에요. 사장님께 5~7줄로 쉬운 말 복기를 써 주세요: 오늘 결과, 잘된 점, 아쉬운 점, 내일 주의할 점. "
              "숫자는 아래 값만 쓰고 지어내지 마세요. 투자 권유 문구는 빼고요.\n"
              f"기록: {json.dumps(data, ensure_ascii=False)}\n제안된 규칙 조정: {json.dumps([{k: p[k] for k in ('label', 'from', 'to', 'why')} for p in proposals], ensure_ascii=False)}")
    try:
        return pv.chat(tm.fast_spec(), "너는 트레이딩 복기 담당이다. 짧고 정확하게.", [{"role": "user", "content": prompt}],
                       effort="low", timeout=120).strip()
    except Exception as e:
        return f"(AI 요약 실패: {type(e).__name__} — 숫자 표는 그대로 보세요)"


def decide(pid: str, action: str) -> dict:
    """제안 적용/거절/되돌리기"""
    from . import autotrade as at
    with _lock:
        d = _load()
        p = next((x for x in d["proposals"] if x["id"] == pid), None)
        if not p:
            return {"ok": False, "message": "없는 제안이에요"}
        if action == "apply" and p["status"] == "pending":
            prev = at.config().get(p["param"])
            r = at.save_config({p["param"]: p["to"]})
            if not r.get("ok"):
                return r
            p.update(status="applied", prev=prev, applied=_now())
        elif action == "reject" and p["status"] == "pending":
            p.update(status="rejected")
        elif action == "revert" and p["status"] == "applied":
            r = at.save_config({p["param"]: p["prev"]})
            if not r.get("ok"):
                return r
            p.update(status="reverted")
        else:
            return {"ok": False, "message": "지금 상태에서는 할 수 없어요"}
        _save(d)
    at.event("학습", f"제안 {dict(apply='적용', reject='거절', revert='되돌림')[action]}: {p['label']} {p['from']} → {p['to']}")
    return {"ok": True, "proposal": p}


def overview(day: str, mode: str) -> dict:
    with _lock:
        d = _load()
    revs = [r for r in d["reviews"] if r["mode"] == mode]
    return {"today": today_stats(day, mode), "review": revs[-1] if revs else None,
            "proposals": [p for p in d["proposals"] if p["status"] in ("pending", "applied")][-20:][::-1],
            "trades": [t for t in d["trades"] if t["mode"] == mode][-30:][::-1]}


def last_review_text(mode: str) -> str:
    with _lock:
        revs = [r for r in _load()["reviews"] if r["mode"] == mode]
    if not revs:
        return ""
    r = revs[-1]
    t = r["today"]
    head = f"최근 복기({r['day']}): 거래 {t.get('n', 0)}건" + (f" · 승률 {t['win_rate']}% · 평균 {t['avg']:+.2f}%" if t.get("n") else "")
    return head + ("\n" + r["text"] if r.get("text") else "")
