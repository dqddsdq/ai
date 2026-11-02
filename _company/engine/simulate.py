"""목표 시뮬레이션 — "이걸 원해"를 수천 번 가상 실행해서 달성 확률을 계산합니다.

  python -m engine simulate "블로그로 월 1000만원 벌고 싶어"
  python -m engine simulate --stock 005930.KS --years 1 --target 20     # 1년에 +20% 가능?
  python -m engine simulate --stock AAPL --csv prices.csv --target 15   # 내 가격 데이터로

구조:
  1) AI가 목표를 '시나리오 모델(JSON)'로 바꿉니다 — 변수, 범위(분포), 계산식, 목표값.
  2) 이 파일의 계산기가 몬테카를로로 N번 실행합니다. AI가 쓴 코드는 실행하지 않고,
     계산식은 사칙연산과 허용된 함수만 되는 안전한 계산기로 계산합니다.
  3) 결과(확률, 범위, 가장 영향 큰 변수)를 AI가 쉬운 말로 설명합니다.

⚠️ 시뮬레이션은 예측이 아니라 '가정이 맞다면'의 결과입니다. 투자 조언이 아닙니다.
"""

from __future__ import annotations

import ast
import json
import operator
import re
from datetime import datetime
from pathlib import Path

import numpy as np

from . import providers as pv
from .agents import COMPANY_DIR, SHARED_DIR, _read

SIM_DIR = COMPANY_DIR / "simulations"
DEFAULT_RUNS = 10_000
TRADING_DAYS = 252


# ── 안전한 계산식 실행기 ──────────────────────────────────────────────────
_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod}
_CMPOPS = {ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Lt: operator.lt, ast.LtE: operator.le}
_FUNCS = {"min": np.minimum, "max": np.maximum, "abs": np.abs, "sqrt": np.sqrt,
          "exp": np.exp, "log": np.log, "round": np.round, "where": np.where, "floor": np.floor}
MAX_FORMULA_CHARS = 500


class FormulaError(ValueError):
    pass


def safe_eval(formula: str, names: dict):
    """'visitors * conv * price - cost' 같은 식을 numpy 배열로 계산. 그 외 문법은 거부."""
    if len(formula) > MAX_FORMULA_CHARS:
        raise FormulaError("계산식이 너무 길어요")
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"계산식 문법 오류: {formula}") from e

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in names:
                raise FormulaError(f"모르는 변수: {node.id}")
            return names[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            right = ev(node.right)
            if isinstance(node.op, ast.Pow) and np.max(np.abs(right)) > 1000:
                raise FormulaError("거듭제곱 지수가 너무 커요")
            return _BINOPS[type(node.op)](ev(node.left), right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = ev(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _CMPOPS:
            return _CMPOPS[type(node.ops[0])](ev(node.left), ev(node.comparators[0])).astype(float)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in _FUNCS and not node.keywords):
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        raise FormulaError(f"허용되지 않는 계산식: {ast.dump(node)[:80]}")

    with np.errstate(divide="ignore", invalid="ignore"):
        return ev(tree)


# ── 분포에서 값 뽑기 ──────────────────────────────────────────────────────
def sample(var: dict, n: int, rng: np.random.Generator) -> np.ndarray:
    d = var.get("dist", "fixed")
    f = lambda k, default=None: float(var.get(k, default))  # noqa: E731
    if d == "fixed":
        x = np.full(n, f("value"))
    elif d == "uniform":
        x = rng.uniform(f("low"), f("high"), n)
    elif d == "triangular":  # 최소/보통/최대 — 사람이 추정하기 가장 쉬운 형태
        lo, mode, hi = f("low"), f("mode"), f("high")
        lo, hi = min(lo, hi), max(lo, hi)
        x = rng.triangular(lo, min(max(mode, lo), hi), hi, n) if hi > lo else np.full(n, lo)
    elif d == "normal":
        x = rng.normal(f("mean"), abs(f("std")), n)
    elif d == "lognormal":  # 중앙값과 변동폭(배수)으로 지정: 조회수·매출처럼 한쪽으로 긴 값
        x = rng.lognormal(np.log(f("median")), abs(f("sigma", 0.5)), n)
    else:
        raise FormulaError(f"모르는 분포: {d}")
    if "min" in var:
        x = np.maximum(x, f("min"))
    if "max" in var:
        x = np.minimum(x, f("max"))
    return x


# ── 시나리오 모델 실행 ────────────────────────────────────────────────────
def run_model(model: dict, runs: int = DEFAULT_RUNS, seed: int | None = 42) -> dict:
    """model = {variables:{}, metric:"식", target:{op,value}, periods?:N} → 결과 dict."""
    rng = np.random.default_rng(seed)
    variables = model.get("variables") or {}
    if not variables or not model.get("metric"):
        raise FormulaError("시나리오에 variables 와 metric 이 필요해요")
    periods = int(model.get("periods") or 1)
    periods = max(1, min(periods, 120))

    draws = {name: sample(v, runs, rng) for name, v in variables.items()}
    names = {k: v[:, None] for k, v in draws.items()}  # (runs, 1) — 기간과 브로드캐스트
    names["t"] = np.arange(1, periods + 1)[None, :]    # (1, periods), 1부터
    values = np.broadcast_to(np.asarray(safe_eval(model["metric"], names), dtype=float), (runs, periods))
    values = np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)

    target = model.get("target") or {}
    op = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt}.get(target.get("op", ">="))
    tval = float(target.get("value", 0))
    hit = op(values, tval)
    final = values[:, -1]

    result = {
        "runs": runs,
        "periods": periods,
        "target": {"op": target.get("op", ">="), "value": tval},
        "p_hit_final": float(hit[:, -1].mean()),
        "percentiles_final": {f"p{q}": float(np.percentile(final, q)) for q in (5, 25, 50, 75, 95)},
        "mean_final": float(final.mean()),
        "sensitivity": _sensitivity(draws, final),
    }
    if periods > 1:
        result["p_hit_by_period"] = [float(x) for x in hit.mean(axis=0)]
        reached = hit.any(axis=1)
        first = np.where(reached, hit.argmax(axis=1) + 1, -1)
        result["p_ever_hit"] = float(reached.mean())
        result["median_first_hit_period"] = int(np.median(first[reached])) if reached.any() else None
    return result


def _rank(x: np.ndarray) -> np.ndarray:
    return np.argsort(np.argsort(x)).astype(float)


def _sensitivity(draws: dict, final: np.ndarray) -> list[tuple[str, float]]:
    """각 변수와 결과의 순위 상관 — 절댓값이 클수록 결과를 크게 좌우."""
    out = []
    rf = _rank(final)
    for name, x in draws.items():
        if np.std(x) == 0 or np.std(final) == 0:
            continue
        out.append((name, float(np.corrcoef(_rank(x), rf)[0, 1])))
    return sorted(out, key=lambda kv: abs(kv[1]), reverse=True)


# ── 주식 시뮬레이션 (실제 과거 수익률을 섞어 미래 경로 생성) ───────────────
def load_prices(ticker: str | None, csv: str | None, years_history: int = 10) -> np.ndarray:
    if csv:
        import csv as _csv

        with open(csv, encoding="utf-8-sig") as f:
            rows = list(_csv.DictReader(f))
        col = next((c for c in ("Adj Close", "Close", "close", "종가", "price") if rows and c in rows[0]), None)
        if not col:
            raise pv.ProviderError("CSV 에 Close(또는 종가) 열이 필요해요")
        prices = np.array([float(str(r[col]).replace(",", "")) for r in rows if r[col]])
    else:
        try:
            import yfinance as yf
        except ImportError as e:
            raise pv.ProviderError("주가 데이터 패키지가 없어요: pip install yfinance") from e
        data = yf.Ticker(ticker).history(period=f"{years_history}y", auto_adjust=True)
        if data.empty:
            raise pv.ProviderError(f"주가 데이터를 못 가져왔어요: {ticker} (한국 주식은 005930.KS 형식)")
        prices = data["Close"].to_numpy(dtype=float)
    prices = prices[prices > 0]
    if len(prices) < 60:
        raise pv.ProviderError("가격 데이터가 너무 적어요 (최소 60일)")
    return prices


def run_stock(prices: np.ndarray, years: float, target_pct: float,
              runs: int = DEFAULT_RUNS, seed: int | None = 42, block: int = 20) -> dict:
    """블록 부트스트랩: 과거 실제 수익률을 20일 묶음으로 섞어 미래 경로를 만듭니다.
    (급락·급등이 몰려 오는 실제 시장의 특성을 어느 정도 유지)"""
    rng = np.random.default_rng(seed)
    rets = np.diff(np.log(prices))
    horizon = max(1, int(round(years * TRADING_DAYS)))
    n_blocks = -(-horizon // block)
    starts = rng.integers(0, len(rets) - block, size=(runs, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(runs, -1)[:, :horizon]
    paths = np.cumsum(rets[idx], axis=1)
    final = np.expm1(paths[:, -1]) * 100
    peak = np.maximum.accumulate(np.concatenate([np.zeros((runs, 1)), paths], axis=1), axis=1)[:, 1:]
    mdd = (np.expm1(paths - peak).min(axis=1)) * 100

    hist_years = len(rets) / TRADING_DAYS
    return {
        "runs": runs,
        "horizon_years": years,
        "history_years": round(hist_years, 1),
        "history_cagr_pct": float((np.exp(rets.sum() / hist_years) - 1) * 100),
        "history_vol_pct": float(rets.std() * np.sqrt(TRADING_DAYS) * 100),
        "target_pct": target_pct,
        "p_hit_final": float((final >= target_pct).mean()),
        "p_ever_hit": float((np.expm1(paths.max(axis=1)) * 100 >= target_pct).mean()),
        "p_loss": float((final < 0).mean()),
        "percentiles_return_pct": {f"p{q}": float(np.percentile(final, q)) for q in (5, 25, 50, 75, 95)},
        "median_max_drawdown_pct": float(np.median(mdd)),
        "p5_max_drawdown_pct": float(np.percentile(mdd, 5)),
    }


# ── AI: 목표 → 시나리오 모델 ─────────────────────────────────────────────
BUILD_PROMPT = """당신은 1인 기업의 시뮬레이션 설계자입니다. 사용자의 목표를 몬테카를로 시나리오 모델(JSON)로 바꾸세요.

회사 정보:
{context}

규칙:
- 불확실한 값은 분포로: triangular(low, mode, high) 를 기본으로, 조회수·매출처럼 한쪽으로 긴 값은 lognormal(median, sigma).
  확실한 값은 fixed(value). 그 외 uniform(low, high), normal(mean, std). 필요하면 min/max 로 범위 제한.
- 각 변수에 "desc"(한국어 설명, 단위 포함)를 붙이고, 숫자는 현실적인 업계 수준으로 보수적으로 잡으세요.
- metric 은 변수 이름과 + - * / ** ( ) 숫자, min max abs sqrt exp log round floor where 만 쓴 계산식 한 줄.
- 시간에 따라 변하면 "periods"(개월 수, 최대 120)를 정하고 계산식에서 t(1부터 시작하는 월)를 쓰세요.
  예: 월 성장률 g 로 커지는 방문자 → "visitors0 * (1 + g) ** (t - 1)"
- target 은 metric 이 넘어야 할 값. 예: {{"op": ">=", "value": 10000000}}
- assumptions 에 핵심 가정을 짧게 나열하세요.

JSON만 답하세요. 형식:
{{"title": "...", "metric_name": "월 순수익(원)", "metric": "...", "periods": 12,
  "target": {{"op": ">=", "value": 10000000}},
  "variables": {{"name": {{"dist": "triangular", "low": 1, "mode": 2, "high": 3, "desc": "..."}}}},
  "assumptions": ["..."]}}"""

EXPLAIN_PROMPT = """당신은 1인 기업의 분석가입니다. 시뮬레이션 결과를 사장님께 쉬운 한국어로 설명하세요.
- 달성 확률을 먼저, 그다음 '보통의 경우'(중앙값)와 '나쁜 경우/좋은 경우'(하위·상위 5%) 범위.
- sensitivity 에서 영향이 큰 변수 1~3개를 짚고, 그걸 올리려면 무엇을 해야 하는지 구체적 행동 제안.
- 이 결과는 가정이 맞을 때의 이야기이며 예측이 아니라는 점을 한 줄로 밝히세요. 주식이면 투자 조언이 아님을 밝히세요.
- 숫자는 원 단위면 '만원'으로 읽기 쉽게. 10줄 이내."""


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise FormulaError("AI가 시나리오 JSON을 만들지 못했어요. 목표를 더 구체적으로 적어 보세요.")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise FormulaError(f"시나리오 JSON 형식 오류: {e}") from e


def build_model(goal: str, spec: pv.ModelSpec) -> dict:
    context = "\n".join(_read(SHARED_DIR / n)[:1200] for n in ("identity.md", "goals.md"))
    reply = pv.chat(spec, BUILD_PROMPT.format(context=context), [{"role": "user", "content": goal}], effort="high")
    model = _extract_json(reply)
    run_model(model, runs=200)  # 계산식이 실제로 돌아가는지 미리 검사
    return model


def explain(spec: pv.ModelSpec, goal: str, model: dict | None, result: dict, on_text=None) -> str:
    payload = json.dumps({"goal": goal, "model": model, "result": result}, ensure_ascii=False, default=str)
    return pv.chat(spec, EXPLAIN_PROMPT, [{"role": "user", "content": payload}], on_text=on_text, effort="medium")


def save_report(goal: str, model: dict | None, result: dict, explanation: str) -> Path:
    SIM_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M")
    slug = re.sub(r"[^\w가-힣]+", "_", goal).strip("_")[:40] or "sim"
    path = SIM_DIR / f"{stamp}_{slug}.md"
    body = [f"# 시뮬레이션: {goal}", f"_{stamp} · {result['runs']:,}회 실행 · 예측이 아닌 가정 기반 결과_", "",
            "## 해석", explanation.strip(), ""]
    if model:
        body += ["## 시나리오 모델", "```json", json.dumps(model, ensure_ascii=False, indent=2), "```", ""]
    body += ["## 원시 결과", "```json", json.dumps(result, ensure_ascii=False, indent=2), "```"]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


# ── 터미널 출력 ───────────────────────────────────────────────────────────
def _fmt(x: float) -> str:
    if abs(x) >= 10_000:
        return f"{x / 10_000:,.0f}만"
    return f"{x:,.2f}" if abs(x) < 100 else f"{x:,.0f}"


def _bar(p: float, width: int = 30) -> str:
    n = int(round(p * width))
    return "█" * n + "░" * (width - n)


def print_model_result(model: dict, r: dict) -> None:
    print(f"\n📐 {model.get('title', '시나리오')}  —  {model.get('metric_name', 'metric')} = {model['metric']}")
    for name, v in model["variables"].items():
        params = {k: v[k] for k in ("value", "low", "mode", "high", "mean", "std", "median", "sigma") if k in v}
        print(f"   · {name:14} {v.get('dist', 'fixed'):10} {params}  {v.get('desc', '')}")
    t = r["target"]
    print(f"\n🎯 목표: {model.get('metric_name', 'metric')} {t['op']} {_fmt(t['value'])}")
    label = "마지막 기간" if r["periods"] > 1 else "달성"
    print(f"   {label} 달성 확률  {_bar(r['p_hit_final'])} {r['p_hit_final']:.0%}")
    if r["periods"] > 1:
        print(f"   기간 중 한 번이라도  {_bar(r['p_ever_hit'])} {r['p_ever_hit']:.0%}"
              + (f"  (보통 {r['median_first_hit_period']}개월째)" if r.get("median_first_hit_period") else ""))
    p = r["percentiles_final"]
    print(f"   결과 범위: 나쁜 경우 {_fmt(p['p5'])} · 보통 {_fmt(p['p50'])} · 좋은 경우 {_fmt(p['p95'])}")
    if r["sensitivity"]:
        print("   영향 큰 변수: " + ", ".join(f"{k}({v:+.2f})" for k, v in r["sensitivity"][:3]))


def print_stock_result(ticker: str, r: dict) -> None:
    p = r["percentiles_return_pct"]
    print(f"\n📈 {ticker} — 과거 {r['history_years']}년 데이터 (연 {r['history_cagr_pct']:+.1f}%, 변동성 {r['history_vol_pct']:.0f}%)")
    print(f"🎯 {r['horizon_years']}년 후 +{r['target_pct']}% 이상")
    print(f"   만기 달성 확률     {_bar(r['p_hit_final'])} {r['p_hit_final']:.0%}")
    print(f"   중간에 한 번이라도 {_bar(r['p_ever_hit'])} {r['p_ever_hit']:.0%}")
    print(f"   손실 확률          {_bar(r['p_loss'])} {r['p_loss']:.0%}")
    print(f"   수익률 범위: 나쁜 경우 {p['p5']:+.1f}% · 보통 {p['p50']:+.1f}% · 좋은 경우 {p['p95']:+.1f}%")
    print(f"   최대 낙폭: 보통 {r['median_max_drawdown_pct']:.1f}% · 나쁜 경우 {r['p5_max_drawdown_pct']:.1f}%")
