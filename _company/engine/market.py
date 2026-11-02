"""주식 시세 + 차트 지표.

시세 출처 (자동 선택):
  1) 한국투자증권 KIS Open API — .env 에 KIS_APP_KEY, KIS_APP_SECRET 가 있으면 국내 주식 '현재가'를 실시간으로.
     모의투자 계좌 키면 KIS_MOCK=1 도 넣으세요.
  2) 야후 파이낸스 (yfinance) — 키 없이. 국내 주식은 약 15~20분 지연. 과거 차트(지표 계산)는 항상 여기서 먼저.

지표: 이동평균(5/20/60/120), RSI(14), MACD(12,26,9), 볼린저밴드(20,2), 거래량 비율, 52주 고저, 기간 수익률, 변동성.

⚠️ KIS 연결 부분은 실제 키 없이 작성되어 아직 실계좌로 검증되지 않았습니다. 첫 실행에서 오류가 나면 메시지를 알려주세요.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from .agents import COMPANY_DIR

KIS_REAL = "https://openapi.koreainvestment.com:9443"
KIS_MOCK = "https://openapivts.koreainvestment.com:29443"
TOKEN_CACHE = COMPANY_DIR / "_cache" / "kis_token.json"  # _cache/ 는 git 에 안 올라감


@dataclass
class Snapshot:
    ticker: str
    name: str = ""
    price: float | None = None
    change_pct: float | None = None
    source: str = ""
    asof: str = ""
    quote: dict = field(default_factory=dict)       # 현재가 관련 추가 정보 (거래량, PER 등)
    indicators: dict = field(default_factory=dict)
    error: str = ""

    def brief(self) -> dict:
        return {"ticker": self.ticker, "name": self.name, "price": self.price, "change_pct": self.change_pct,
                "source": self.source, "asof": self.asof, "error": self.error}


def kis_enabled() -> bool:
    return bool(os.getenv("KIS_APP_KEY") and os.getenv("KIS_APP_SECRET"))


def _kis_base() -> str:
    return KIS_MOCK if os.getenv("KIS_MOCK", "").strip() in ("1", "true", "yes") else KIS_REAL


def _kis_token() -> str:
    """접근 토큰 (24시간 유효, 발급은 1분에 1번 제한이라 파일에 저장해 재사용)."""
    import requests

    try:
        cached = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
        if cached.get("base") == _kis_base() and cached.get("expires", 0) > time.time() + 300:
            return cached["token"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    r = requests.post(f"{_kis_base()}/oauth2/tokenP", timeout=15, json={
        "grant_type": "client_credentials",
        "appkey": os.environ["KIS_APP_KEY"], "appsecret": os.environ["KIS_APP_SECRET"],
    })
    r.raise_for_status()
    data = r.json()
    token = data["access_token"]
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(json.dumps({"token": token, "base": _kis_base(),
                                       "expires": time.time() + int(data.get("expires_in", 86400))}),
                           encoding="utf-8")
    return token


def _kis_get(path: str, tr_id: str, params: dict) -> dict:
    import requests

    r = requests.get(f"{_kis_base()}{path}", params=params, timeout=15, headers={
        "authorization": f"Bearer {_kis_token()}",
        "appkey": os.environ["KIS_APP_KEY"], "appsecret": os.environ["KIS_APP_SECRET"],
        "tr_id": tr_id, "custtype": "P",
    })
    r.raise_for_status()
    data = r.json()
    if str(data.get("rt_cd", "0")) != "0":
        raise RuntimeError(f"KIS 오류: {data.get('msg1', data)}")
    return data


def _kr_code(ticker: str) -> str | None:
    """'005930.KS' / '035720.KQ' / '005930' → '005930'"""
    code = ticker.split(".")[0]
    return code if code.isdigit() and len(code) == 6 else None


def kis_quote(code: str) -> dict:
    out = _kis_get("/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
                   {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})["output"]
    num = lambda k: float(out[k]) if out.get(k) not in (None, "") else None  # noqa: E731
    return {
        "price": num("stck_prpr"), "change": num("prdy_vrss"), "change_pct": num("prdy_ctrt"),
        "open": num("stck_oprc"), "high": num("stck_hgpr"), "low": num("stck_lwpr"),
        "volume": num("acml_vol"), "per": num("per"), "pbr": num("pbr"), "eps": num("eps"),
        "market_cap_eok": num("hts_avls"), "w52_high": num("w52_hgpr"), "w52_low": num("w52_lwpr"),
    }


def kis_daily(code: str, days: int = 150) -> tuple[np.ndarray, np.ndarray]:
    """최근 일봉 (최대 100개씩) → (종가, 거래량), 오래된 것부터."""
    end = datetime.now()
    start = end - timedelta(days=int(days * 1.6))
    out = _kis_get("/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "FHKST03010100", {
        "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": start.strftime("%Y%m%d"), "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
        "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
    }).get("output2", [])
    rows = [r for r in out if r.get("stck_clpr")][::-1]
    return (np.array([float(r["stck_clpr"]) for r in rows]), np.array([float(r.get("acml_vol") or 0) for r in rows]))


def yf_history(ticker: str) -> tuple[np.ndarray, np.ndarray, str, str]:
    import yfinance as yf

    t = yf.Ticker(ticker)
    h = t.history(period="1y", auto_adjust=True)
    if h.empty:
        raise RuntimeError(f"데이터 없음: {ticker} (한국 주식은 005930.KS, 코스닥은 .KQ)")
    name = ""
    try:
        info = t.get_info()
        name = info.get("shortName") or info.get("longName") or ""
    except Exception:
        pass
    asof = h.index[-1].strftime("%Y-%m-%d %H:%M")
    return h["Close"].to_numpy(float), h["Volume"].to_numpy(float), name, asof


# ── 지표 계산 ─────────────────────────────────────────────────────────────
def _ema(x: np.ndarray, n: int) -> np.ndarray:
    a = 2 / (n + 1)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def _rsi(close: np.ndarray, n: int = 14) -> float | None:
    if len(close) <= n:
        return None
    d = np.diff(close)
    up, down = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = up[:n].mean(), down[:n].mean()
    for u, dn in zip(up[n:], down[n:]):  # Wilder 평활
        au, ad = (au * (n - 1) + u) / n, (ad * (n - 1) + dn) / n
    return 100.0 if ad == 0 else float(100 - 100 / (1 + au / ad))


def indicators(close: np.ndarray, volume: np.ndarray, last_price: float | None = None) -> dict:
    close = np.asarray(close, float)
    if last_price:  # 실시간 현재가가 있으면 마지막 값을 갱신해서 계산
        close = close.copy()
        close[-1] = last_price
    p = float(close[-1])
    ind: dict = {"price": p}
    for n in (5, 20, 60, 120):
        if len(close) >= n:
            ma = float(close[-n:].mean())
            ind[f"ma{n}"] = round(ma, 2)
            ind[f"vs_ma{n}_pct"] = round((p / ma - 1) * 100, 2)
    ind["rsi14"] = None if (r := _rsi(close)) is None else round(r, 1)
    if len(close) >= 35:
        macd = _ema(close, 12) - _ema(close, 26)
        sig = _ema(macd, 9)
        ind.update(macd=round(float(macd[-1]), 3), macd_signal=round(float(sig[-1]), 3),
                   macd_hist=round(float(macd[-1] - sig[-1]), 3),
                   macd_cross=("골든크로스" if macd[-2] <= sig[-2] and macd[-1] > sig[-1]
                               else "데드크로스" if macd[-2] >= sig[-2] and macd[-1] < sig[-1] else "없음"))
    if len(close) >= 20:
        m, sd = close[-20:].mean(), close[-20:].std()
        ind.update(bb_upper=round(float(m + 2 * sd), 2), bb_lower=round(float(m - 2 * sd), 2),
                   bb_pos_pct=round(float((p - (m - 2 * sd)) / (4 * sd) * 100), 1) if sd else None)
    if len(volume) >= 21 and volume[-21:-1].mean() > 0:
        ind["vol_ratio_20d"] = round(float(volume[-1] / volume[-21:-1].mean()), 2)
    w = close[-252:]
    ind.update(high_52w=round(float(w.max()), 2), low_52w=round(float(w.min()), 2),
               from_high_pct=round(float(p / w.max() - 1) * 100, 2))
    for label, n in (("1d", 1), ("1w", 5), ("1m", 21), ("3m", 63), ("6m", 126), ("1y", 251)):
        if len(close) > n:
            ind[f"ret_{label}_pct"] = round(float(p / close[-1 - n] - 1) * 100, 2)
    if len(close) > 21:
        rets = np.diff(np.log(close[-61:]))
        ind["vol_annual_pct"] = round(float(rets.std() * np.sqrt(252) * 100), 1)
    return ind


def snapshot(ticker: str) -> Snapshot:
    ticker = ticker.strip().upper()
    snap = Snapshot(ticker)
    close = volume = None
    code = _kr_code(ticker)
    if code and "." not in ticker:
        ticker = snap.ticker = f"{code}.KS"
    errors = []
    try:
        close, volume, snap.name, snap.asof = yf_history(ticker)
        snap.source = "야후 파이낸스 (국내 약 15~20분 지연)" if code else "야후 파이낸스 (지연 가능)"
    except Exception as e:
        errors.append(f"야후: {e}")
        if code and ticker.endswith(".KS"):  # 코스피에 없으면 코스닥으로 재시도
            try:
                close, volume, snap.name, snap.asof = yf_history(f"{code}.KQ")
                ticker = snap.ticker = f"{code}.KQ"
                snap.source = "야후 파이낸스 (국내 약 15~20분 지연)"
                errors.clear()
            except Exception as e2:
                errors.append(f"야후(KQ): {e2}")
    if code and kis_enabled():
        try:
            snap.quote = kis_quote(code)
            snap.source = "한국투자증권 실시간" + (" (모의)" if _kis_base() == KIS_MOCK else "")
            snap.asof = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if close is None:
                close, volume = kis_daily(code)
            errors.clear()
        except Exception as e:
            errors.append(f"KIS: {e}")
    if close is None or len(close) < 2:
        snap.error = " / ".join(errors) or "데이터 없음"
        return snap
    last = snap.quote.get("price")
    snap.indicators = indicators(close, volume, last)
    snap.price = snap.indicators["price"]
    snap.change_pct = snap.quote.get("change_pct", snap.indicators.get("ret_1d_pct"))
    if errors:
        snap.error = " / ".join(errors)
    return snap


def format_for_prompt(snaps: list[Snapshot]) -> str:
    parts = ["<market_data>",
             "아래 숫자는 시스템이 방금 가져온 데이터입니다. 분석 근거로 쓰고, 출처와 시각(지연 여부)을 답에 밝히세요."]
    for s in snaps:
        if not s.indicators:
            parts.append(f"- {s.ticker}: 데이터를 못 가져왔어요 ({s.error}). 이 종목은 숫자를 지어내지 말고 못 가져왔다고 말하세요.")
            continue
        parts.append(f"## {s.name or s.ticker} ({s.ticker}) — 출처: {s.source}, 기준: {s.asof}")
        if s.quote:
            parts.append("현재가 정보: " + json.dumps({k: v for k, v in s.quote.items() if v is not None},
                                                   ensure_ascii=False))
        parts.append("차트 지표: " + json.dumps(s.indicators, ensure_ascii=False))
    parts.append("</market_data>")
    return "\n".join(parts)


TICKER_PROMPT = """사용자 메시지(와 대화 흐름)에서 분석 대상 주식/ETF를 찾아 야후 파이낸스 티커로 바꾸세요.
- 한국 코스피: 6자리코드.KS (예: 삼성전자 005930.KS, SK하이닉스 000660.KS), 코스닥: 6자리코드.KQ (예: 에코프로 086520.KQ)
- 미국: 티커 그대로 (예: AAPL, NVDA, TSLA, SPY, QQQ)
- 최대 5개. 종목이 없으면 빈 배열. 확실하지 않은 코드는 넣지 마세요.
JSON 배열로만 답하세요. 예: ["005930.KS", "NVDA"]"""
