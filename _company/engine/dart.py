"""DART(금융감독원 전자공시) 연결 — 국내 상장사 공시·실적 원문 숫자.

.env 의 DART_API_KEY 가 있으면 동작합니다 (🧰 도구 화면에서 입력).
- 회사 목록(종목코드 ↔ 회사 고유번호)은 _cache/dart_corp.json 에 7일 보관
- 최근 공시 목록, 최근 정기보고서(분기·반기·사업보고서)의 매출·영업이익·순이익
- 종목코드가 맞는지 공식 회사명으로 확인 (AI 가 코드를 잘못 짚는 것 방지)
"""

from __future__ import annotations

import io
import json
import os
import re
import time
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from .agents import COMPANY_DIR

BASE = "https://opendart.fss.or.kr/api/"
CORP_CACHE = COMPANY_DIR / "_cache" / "dart_corp.json"
CORP_TTL = 7 * 86400
REPORTS = {"11013": "1분기보고서", "11012": "반기보고서", "11014": "3분기보고서", "11011": "사업보고서"}
STATUS = {"010": "등록되지 않은 키", "011": "사용할 수 없는 키", "012": "접근할 수 없는 IP", "020": "오늘 요청 한도 초과",
          "100": "잘못된 값", "800": "DART 점검 중", "900": "DART 오류"}
ACCOUNTS = [("매출액", ("매출액", "수익(매출액)", "영업수익", "매출")), ("영업이익", ("영업이익", "영업이익(손실)")),
            ("당기순이익", ("당기순이익", "당기순이익(손실)", "분기순이익", "반기순이익"))]

_corp: dict | None = None
_mem: dict[str, tuple[float, object]] = {}


class DartError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(os.environ.get("DART_API_KEY", "").strip())


def _get(endpoint: str, raw: bool = False, **params):
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key:
        raise DartError("DART 키가 없어요 (🧰 도구 화면에서 입력)")
    url = BASE + endpoint + "?" + urllib.parse.urlencode({"crtfc_key": key, **params})
    try:
        with urllib.request.urlopen(url, timeout=60 if raw else 20) as r:
            body = r.read()
    except Exception as e:  # 네트워크 오류 메시지에 키가 섞이지 않게 주소는 빼고 알림
        raise DartError(f"DART 접속 실패: {type(e).__name__}") from None
    if raw:
        return body
    data = json.loads(body)
    st = data.get("status")
    if st not in ("000", "013"):
        raise DartError(f"DART: {STATUS.get(st, data.get('message', st))}")
    return data


def _cached(key: str, ttl: float, fn):
    hit = _mem.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _mem[key] = (time.time(), val)
    return val


# ── 회사 목록 ──
def corps() -> dict:
    """{'by_stock': {종목코드: [고유번호, 회사명]}, 'by_name': {회사명: 종목코드}} — 상장사만."""
    global _corp
    if _corp is not None:
        return _corp
    if CORP_CACHE.is_file() and time.time() - CORP_CACHE.stat().st_mtime < CORP_TTL:
        _corp = json.loads(CORP_CACHE.read_text(encoding="utf-8"))
        return _corp
    body = _get("corpCode.xml", raw=True)
    if not body.startswith(b"PK"):  # 오류면 zip 대신 JSON/XML 이 옴
        m = re.search(rb"<status>(\d+)</status>", body) or re.search(rb'"status"\s*:\s*"(\d+)"', body)
        raise DartError(f"DART: {STATUS.get(m.group(1).decode(), '회사 목록 못 받음') if m else '회사 목록 못 받음'}")
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        root = ET.fromstring(z.read(z.namelist()[0]))
    by_stock, by_name = {}, {}
    for el in root.iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        if not stock:
            continue
        name = (el.findtext("corp_name") or "").strip()
        by_stock[stock] = [el.findtext("corp_code").strip(), name]
        by_name[name] = stock
    _corp = {"by_stock": by_stock, "by_name": by_name}
    CORP_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CORP_CACHE.write_text(json.dumps(_corp, ensure_ascii=False), encoding="utf-8")
    return _corp


def lookup(stock_code: str) -> tuple[str, str] | None:
    """종목코드 6자리 → (고유번호, 공식 회사명)"""
    v = corps()["by_stock"].get(stock_code)
    return (v[0], v[1]) if v else None


def find_in_text(text: str, limit: int = 3) -> list[str]:
    """글 속에 나온 상장사 이름 → 종목코드 (3글자 이상 이름만, 긴 이름 우선, 겹치면 긴 쪽만)."""
    names = corps()["by_name"]
    found: list[tuple[int, str, str]] = []
    for name, code in names.items():
        if len(name) >= 3 and name in text:
            found.append((text.index(name), name, code))
    found.sort(key=lambda x: -len(x[1]))
    keep: list[tuple[int, str, str]] = []
    for f in found:
        if not any(f[1] in k[1] for k in keep):
            keep.append(f)
    keep.sort()
    return [c for _, _, c in keep[:limit]]


def market_suffix(corp_code: str) -> str:
    """야후 티커 접미사: 코스피 .KS / 코스닥 .KQ"""
    d = _cached("co:" + corp_code, 86400, lambda: _get("company.json", corp_code=corp_code))
    return {"Y": ".KS", "K": ".KQ"}.get(d.get("corp_cls"), ".KS")


# ── 공시·실적 ──
ROUTINE = re.compile(r"임원ㆍ주요주주특정증권등소유상황보고서|주식등의대량보유상황보고서|최대주주등소유주식변동신고서")


def disclosures(corp_code: str, days: int = 60, n: int = 8) -> list[dict]:
    """최근 공시. 임원·대주주 지분 보고처럼 매일 쏟아지는 건 한 줄로 묶음."""
    end = datetime.now()
    def fetch():
        d = _get("list.json", corp_code=corp_code, bgn_de=(end - timedelta(days=days)).strftime("%Y%m%d"),
                 end_de=end.strftime("%Y%m%d"), page_count=100)
        rows = [{"date": f"{x['rcept_dt'][:4]}-{x['rcept_dt'][4:6]}-{x['rcept_dt'][6:]}", "title": re.sub(r"\s+", " ", x["report_nm"]).strip(),
                 "by": x.get("flr_nm", ""), "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + x["rcept_no"]}
                for x in d.get("list", [])]
        main = [r for r in rows if not ROUTINE.search(r["title"])][:n]
        pre = next((r for r in rows if PRELIM.search(r["title"])), None)
        if pre and pre not in main:  # 잠정실적은 밀려나도 꼭 포함
            main.append(pre)
        routine = [r for r in rows if ROUTINE.search(r["title"])]
        if routine:
            main.append({"date": routine[0]["date"], "title": f"임원·대주주 지분 변동 보고 {len(routine)}건 (최근 {days}일)",
                         "by": "", "url": routine[0]["url"], "routine": True})
        return main
    return _cached(f"ls:{corp_code}:{days}:{n}", 600, fetch)


def filing_text(rcept_no: str, limit: int = 1800) -> str:
    """공시 원문(표 포함)을 글자로. 스타일·태그를 걷어내고 앞부분만."""
    def fetch():
        body = _get("document.xml", raw=True, rcept_no=rcept_no)
        if not body.startswith(b"PK"):
            return ""
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            x = z.read(z.namelist()[0]).decode("utf-8", "ignore")
        x = re.sub(r"(?is)<style.*?</style>", " ", x)
        t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", x)).strip()
        head = t[:t.find("실적기간")].strip()[:120] if "실적기간" in t else ""
        if "실적기간" in t:  # 정정공시는 앞에 정정 사유가 길게 붙음 → 마지막 실적 표부터
            t = head + " … " + t[t.rfind("실적기간"):]
        t = re.split(r"\d\. 정보제공내역", t)[0]  # 연락처 앞까지가 숫자
        return t[:limit]
    return _cached("doc:" + rcept_no, 86400, fetch)


PRELIM = re.compile(r"\(잠정\)실적|잠정실적|영업실적등에대한전망")


def _num(s) -> int | None:
    s = str(s or "").replace(",", "").strip()
    return int(s) if re.fullmatch(r"-?\d+", s) else None


def financials(corp_code: str) -> dict:
    """가장 최근 정기보고서의 핵심 숫자. 연결재무제표 우선."""
    def fetch():
        y = datetime.now().year
        tries = [(y, "11014"), (y, "11012"), (y, "11013"), (y - 1, "11011"), (y - 1, "11014")]
        for year, code in tries:
            d = _get("fnlttSinglAcnt.json", corp_code=corp_code, bsns_year=str(year), reprt_code=code)
            rows = d.get("list") or []
            if not rows:
                continue
            fs = "CFS" if any(r.get("fs_div") == "CFS" for r in rows) else "OFS"
            rows = [r for r in rows if r.get("fs_div") == fs and r.get("sj_div") == "IS"]
            out = {"report": f"{year}년 {REPORTS[code]}", "annual": code == "11011", "fs": "연결" if fs == "CFS" else "별도",
                   "period": rows[0].get("thstrm_nm", "") if rows else "", "prev_period": rows[0].get("frmtrm_nm", "") if rows else "",
                   "items": {}, "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rows[0]["rcept_no"] if rows else ""}
            for label, keys in ACCOUNTS:
                r = next((r for r in rows if r.get("account_nm", "").strip() in keys), None)
                if r:
                    cur, prev = _num(r.get("thstrm_amount")), _num(r.get("frmtrm_amount"))
                    out["items"][label] = {"now": cur, "prev": prev, "ytd": _num(r.get("thstrm_add_amount")),
                                           "yoy_pct": round((cur - prev) / abs(prev) * 100, 1) if cur is not None and prev else None}
            if out["items"]:
                return out
        return {}
    return _cached("fin:" + corp_code, 3600, fetch)


def _eok(v: int | None) -> str:
    if v is None:
        return "-"
    e = v / 1e8
    return f"{e / 1e4:,.2f}조" if abs(e) >= 1e4 else f"{e:,.0f}억"


def report(stock_code: str) -> dict:
    """종목 하나의 DART 요약. 실패해도 error 칸에 이유를 담아 돌려줌."""
    out = {"code": stock_code, "name": "", "disclosures": [], "financials": {}, "error": ""}
    try:
        hit = lookup(stock_code)
        if not hit:
            out["error"] = "DART 상장사 목록에 없는 종목코드"
            return out
        corp_code, out["name"] = hit
        out["disclosures"] = disclosures(corp_code)
        out["financials"] = financials(corp_code)
        pre = next((d for d in out["disclosures"] if PRELIM.search(d["title"])), None)
        if pre:  # 정기보고서보다 새 숫자 — 원문 표를 붙임
            out["prelim"] = {"date": pre["date"], "title": pre["title"], "url": pre["url"],
                             "text": filing_text(pre["url"].rsplit("=", 1)[1])}
    except DartError as e:
        out["error"] = str(e)
    except Exception as e:  # 예상 못 한 응답 모양
        out["error"] = f"DART 처리 오류: {type(e).__name__}"
    return out


def brief(r: dict) -> dict:
    """작업 현황 창에 보낼 짧은 요약"""
    f = r.get("financials") or {}
    op = (f.get("items") or {}).get("영업이익") or {}
    return {"code": r["code"], "name": r["name"], "error": r["error"], "report": f.get("report", ""),
            "op": _eok(op.get("now")) if op else "", "prelim": (r.get("prelim") or {}).get("date", ""), "op_yoy": op.get("yoy_pct"), "n_disc": len(r.get("disclosures") or []),
            "text": format_one(r)}


def format_one(r: dict) -> str:
    if r["error"]:
        return f"**{r['code']}** — DART 못 가져옴: {r['error']}"
    lines = [f"**{r['name']}** ({r['code']}) — DART 공식 자료"]
    f = r.get("financials") or {}
    if f:
        span = "연간" if f.get("annual") else "해당 분기 3개월"
        lines.append(f"\n{f['report']} · {f['fs']} 기준 · 숫자는 {span} ({f['period']} vs {f['prev_period']})")
        for label, v in f["items"].items():
            yoy = f" · 전년 같은 기간 대비 {v['yoy_pct']:+.1f}%" if v.get("yoy_pct") is not None else ""
            ytd = f" · 올해 누적 {_eok(v['ytd'])}" if v.get("ytd") is not None and not f.get("annual") else ""
            lines.append(f"- {label}: {_eok(v['now'])} (전년 같은 기간 {_eok(v['prev'])}){yoy}{ytd}")
        if f.get("url"):
            lines.append(f"- 원문: {f['url']}")
    pre = r.get("prelim")
    if pre and pre.get("text"):
        lines.append(f"\n⭐ 더 최신: {pre['date']} {pre['title']} (잠정치, 단위는 원문 표기 따름) — {pre['url']}\n{pre['text']}")
    if r["disclosures"]:
        lines.append("\n최근 공시")
        lines += [f"- {d['date']} {d['title']}" + (f" ({d['by']})" if d["by"] and d["by"] != r["name"] else "") + f" — {d['url']}"
                  for d in r["disclosures"]]
    else:
        lines.append("\n최근 60일 공시 없음")
    return "\n".join(lines)


def format_for_prompt(reports: list[dict]) -> str:
    if not reports:
        return ""
    body = "\n\n".join(format_one(r) for r in reports)
    return ("<dart_data>\n금융감독원 DART 공식 공시 자료예요. 정기보고서보다 최근인 '잠정실적' 공시가 목록에 있으면 그게 더 새 숫자라는 점을 알리세요. 실적 숫자는 여기 값을 우선 쓰고, 인용할 땐 '(DART, 보고서명)'처럼 출처를 밝히세요. "
            "회사명이 사장님이 말한 종목과 다르면 종목코드를 잘못 짚은 것이니 그 사실을 먼저 알리세요.\n\n" + body + "\n</dart_data>")
