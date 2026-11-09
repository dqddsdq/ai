"""📊 차트 그리기 — 토스 데이터(1분봉·일봉)로 캔들 차트 PNG 를 만듦. AI 가 보는 그림 = 사장님이 보는 그림.

- 캔들(상승 빨강·하락 파랑) + 이동평균(일봉 5/20/60, 1분봉 5/20) + 1분봉은 VWAP(그날 평균 단가)
- 아래쪽 거래량 막대
- 선택: 산 가격·손절선·나눠 팔기선·익절선, 매수/매도 표시(▲▼)
그림은 _cache/uploads 에 저장 → 채팅 이미지와 같은 주소(/api/upload/파일명)로 보임.
"""

from __future__ import annotations

import os
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from . import toss
from .agents import COMPANY_DIR

OUT = COMPANY_DIR / "_cache" / "uploads"
W, H = 1100, 640
PAD_L, PAD_R, PAD_T = 14, 92, 46
PRICE_H, VOL_H, GAP = 420, 110, 18
UP, DOWN = (217, 48, 37), (30, 100, 210)
BG, GRID, TEXT, MUTED = (255, 255, 255), (232, 232, 232), (30, 30, 30), (120, 120, 120)
MA_COLORS = {5: (240, 160, 0), 20: (140, 70, 200), 60: (0, 150, 120)}
FONT_CANDIDATES = ["C:/Windows/Fonts/malgun.ttf", "C:/Windows/Fonts/malgunbd.ttf", "/System/Library/Fonts/AppleSDGothicNeo.ttc",
                   "/usr/share/fonts/truetype/nanum/NanumGothic.ttf", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]


KO = False   # 한글 글꼴이 있으면 True → 없으면 영어 라벨
EN = {"1분봉": "1m", "일봉": "1D", "거래량": "Volume", "오늘 시작 대비": "vs open", "토스증권 데이터": "Toss data",
      "산 가격": "Entry", "손절": "Stop", "나눠 팔기": "Partial TP", "익절": "TP", "최고가": "High", "고점": "High"}


def _t(s: str) -> str:
    if KO:
        return s
    for k, v in EN.items():
        s = s.replace(k, v)
    return s.encode("ascii", "ignore").decode() or "?"


def _font(size: int):
    global KO
    for f in FONT_CANDIDATES:
        if os.path.isfile(f):
            try:
                font = ImageFont.truetype(f, size)
                KO = "DejaVu" not in f
                return font
            except OSError:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def candles(sym: str, interval: str = "1d", count: int = 120) -> list[dict]:
    """오래된 것 → 최신. 1분봉은 200개씩 나눠 받음."""
    out, before = [], None
    while len(out) < count:
        r = toss._get("/api/v1/candles", symbol=toss.symbol(sym), interval=interval, count=min(200, count - len(out)), before=before) or {}
        cs = r.get("candles") or []
        out += cs
        before = r.get("nextBefore")
        if not cs or not before:
            break
    out.sort(key=lambda c: c["timestamp"])
    return [{"t": c["timestamp"], "o": float(c.get("openPrice") or c["closePrice"]), "h": float(c.get("highPrice") or c["closePrice"]),
             "l": float(c.get("lowPrice") or c["closePrice"]), "c": float(c["closePrice"]), "v": float(c.get("volume") or 0)} for c in out]


def _fmt(p: float) -> str:
    return f"{p:,.0f}" if p >= 1000 else (f"{p:,.2f}" if p >= 1 else f"{p:.4f}")


def draw(sym: str, interval: str = "1d", count: int | None = None, lines: dict | None = None,
         marks: list[dict] | None = None, title: str = "", data: list[dict] | None = None, name: str = "") -> str:
    """차트 PNG 저장 후 파일명 반환. lines={'산 가격': 12.3, ...}, marks=[{'t': ISO, 'side': 'BUY'|'SELL', 'px': 12.3}]"""
    count = count or (400 if interval == "1m" else 120)
    cs = data if data is not None else candles(sym, interval, count)
    if len(cs) < 2:
        raise ValueError("차트 데이터가 부족해요")
    lines = {k: v for k, v in (lines or {}).items() if v}
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_s, f_l = _font(13), _font(19)
    closes = [c["c"] for c in cs]
    hi = max([c["h"] for c in cs] + list(lines.values()))
    lo = min([c["l"] for c in cs] + list(lines.values()))
    span = (hi - lo) or hi * 0.01 or 1
    hi, lo = hi + span * 0.04, lo - span * 0.04
    n = len(cs)
    plot_w = W - PAD_L - PAD_R
    step = plot_w / n
    body = max(1.0, step * 0.68)
    y0_vol = PAD_T + PRICE_H + GAP

    def x(i): return PAD_L + step * (i + 0.5)
    def y(p): return PAD_T + (hi - p) / (hi - lo) * PRICE_H

    # 격자·가격 눈금
    for k in range(6):
        p = lo + (hi - lo) * k / 5
        yy = y(p)
        d.line([(PAD_L, yy), (W - PAD_R, yy)], fill=GRID)
        d.text((W - PAD_R + 6, yy - 8), _fmt(p), fill=MUTED, font=f_s)
    # 시간 눈금 (6개)
    for k in range(6):
        i = min(n - 1, int(k * (n - 1) / 5))
        t = cs[i]["t"]
        lab = t[5:10] if interval == "1d" else t[11:16]
        d.line([(x(i), PAD_T), (x(i), y0_vol + VOL_H)], fill=GRID)
        d.text((x(i) - 18, y0_vol + VOL_H + 4), lab, fill=MUTED, font=f_s)
    # 거래량
    vmax = max(c["v"] for c in cs) or 1
    for i, c in enumerate(cs):
        col = UP if c["c"] >= c["o"] else DOWN
        vh = c["v"] / vmax * VOL_H
        d.rectangle([x(i) - body / 2, y0_vol + VOL_H - vh, x(i) + body / 2, y0_vol + VOL_H], fill=col + (0,) if False else tuple(min(255, v + 90) for v in col))
    # 이동평균
    for nma in ((5, 20, 60) if interval == "1d" else (5, 20)):
        if n > nma:
            pts = [(x(i), y(sum(closes[i - nma + 1:i + 1]) / nma)) for i in range(nma - 1, n)]
            d.line(pts, fill=MA_COLORS[nma], width=2)
    # VWAP (1분봉: 마지막 날짜 기준)
    legend = [(f"MA{k}", MA_COLORS[k]) for k in ((5, 20, 60) if interval == "1d" else (5, 20))]
    if interval == "1m":
        day = cs[-1]["t"][:10]
        pv_sum = v_sum = 0.0
        pts = []
        for i, c in enumerate(cs):
            if c["t"][:10] != day:
                continue
            tp = (c["h"] + c["l"] + c["c"]) / 3
            pv_sum += tp * c["v"]; v_sum += c["v"]
            if v_sum:
                pts.append((x(i), y(pv_sum / v_sum)))
        if len(pts) > 1:
            d.line(pts, fill=(0, 0, 0), width=1)
            legend.append(("VWAP", (0, 0, 0)))
    # 캔들
    for i, c in enumerate(cs):
        col = UP if c["c"] >= c["o"] else DOWN
        d.line([(x(i), y(c["h"])), (x(i), y(c["l"]))], fill=col, width=1)
        top, bot = y(max(c["o"], c["c"])), y(min(c["o"], c["c"]))
        d.rectangle([x(i) - body / 2, top, x(i) + body / 2, max(bot, top + 1)], fill=col)
    # 기준선 (산 가격·손절 등)
    line_colors = [(0, 0, 0), (217, 48, 37), (0, 150, 120), (240, 140, 0), (120, 120, 120)]
    for k, (lname, p) in enumerate(lines.items()):
        yy = y(p)
        col = line_colors[k % len(line_colors)]
        for xx in range(PAD_L, W - PAD_R, 12):
            d.line([(xx, yy), (min(xx + 6, W - PAD_R), yy)], fill=col, width=2)
        d.rectangle([W - PAD_R + 2, yy - 9, W - 2, yy + 9], fill=col)
        d.text((W - PAD_R + 6, yy - 8), _fmt(p), fill=(255, 255, 255), font=f_s)
        d.text((PAD_L + 6, yy - 18), _t(lname), fill=col, font=f_s)
    # 매수/매도 표시
    for m in marks or []:
        i = min(range(n), key=lambda j: abs(_ts(cs[j]["t"]) - _ts(m["t"])))
        xx, yy = x(i), y(m["px"])
        if m["side"] == "BUY":
            d.polygon([(xx, yy + 4), (xx - 8, yy + 18), (xx + 8, yy + 18)], fill=UP)
        else:
            d.polygon([(xx, yy - 4), (xx - 8, yy - 18), (xx + 8, yy - 18)], fill=DOWN)
    # 제목·범례·현재가
    last, prev = cs[-1]["c"], cs[-2]["c"]
    chg = (last / cs[0]["o"] - 1) * 100 if interval == "1m" else (last / prev - 1) * 100
    head = f"{title or toss.symbol(sym)}  ·  {'1분봉' if interval == '1m' else '일봉'}  ·  {_fmt(last)} ({chg:+.2f}%{' 오늘 시작 대비' if interval == '1m' else ''})"
    d.text((PAD_L + 4, 12), _t(head), fill=TEXT, font=f_l)
    lx = W - PAD_R - 10
    for lg, col in reversed(legend):
        w = d.textlength(lg, font=f_s) if hasattr(d, "textlength") else 40
        lx -= w + 26
        d.line([(lx, 22), (lx + 16, 22)], fill=col, width=3)
        d.text((lx + 20, 14), lg, fill=MUTED, font=f_s)
    d.text((PAD_L + 4, y0_vol - 2), _t("거래량"), fill=MUTED, font=f_s)
    d.text((W - 230, H - 18), _t(f"토스증권 데이터 · {datetime.now():%m-%d %H:%M}"), fill=MUTED, font=f_s)
    OUT.mkdir(parents=True, exist_ok=True)
    name = name or f"chart-{toss.symbol(sym)}-{interval}-{datetime.now():%Y%m%d-%H%M%S}-{os.urandom(2).hex()}.png"
    img.save(OUT / name, "PNG", optimize=True)
    _cleanup()
    return name


_last_cleanup = [0.0]


def _cleanup(days: int = 7) -> None:
    """오래된 차트 그림 정리 (하루 한 번): 7일 지난 chart-* · 하루 지난 live-* (실시간 차트)"""
    import time
    if time.time() - _last_cleanup[0] < 86400:
        return
    _last_cleanup[0] = time.time()
    now = time.time()
    for f in OUT.glob("chart-*.png"):
        if now - f.stat().st_mtime > days * 86400:
            f.unlink(missing_ok=True)
    for f in OUT.glob("live-*.png"):
        if now - f.stat().st_mtime > 86400:
            f.unlink(missing_ok=True)


def _ts(t: str) -> float:
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def path(name: str):
    return OUT / name
