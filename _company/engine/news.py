"""📰 사기 전에 공시 확인 — 급등 이유를 알고 사기.

- 국장: DART 공시 (🧰 도구의 DART 키) — 오늘·어제 공시 제목
- 미장: SEC EDGAR 공시 (무료, 키 없음) — 최근 24시간 제출 서류. SEC 규칙상 연락용 이메일이 필요해서
  🧰 도구 → 'SEC 공시 (미장)' 에 사장님 이메일을 넣어야 켜져요 (SEC 에만 보내지고 다른 데는 안 감)

판정:
- bad   : 유상증자·전환사채·감자·상장폐지·거래정지·회생 / 미장 증자 서류(S-1·S-3·424B)·상장폐지(25·8-K 3.01)·파산(8-K 1.03) → 매수 제외
- items : 그 밖의 최근 공시 (= 급등 이유일 수 있는 것) → AI 판단에 같이 넣고 주문 이유에 기록
- ok and not items : 확인했는데 아무 공시 없음 = '이유 모르는 급등' → 비중 축소 (news_unknown_ratio)
- ok False : 확인 못 함 (키 없음·접속 실패) → 판단에 안 씀 (그대로)
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from .agents import COMPANY_DIR

TTL = 20 * 60
_mem: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()

KR_BAD = re.compile(r"유상증자|전환사채|신주인수권부사채|교환사채|전환청구권|감자|상장폐지|관리종목|매매거래정지|거래정지|"
                    r"불성실공시|횡령|배임|감사의견|회생절차|파산|부도|투자주의환기")
KR_OK_EXCEPT = re.compile(r"해제|철회|취소")     # '거래정지 해제', '유상증자 철회' 같은 건 악재 아님
KR_ROUTINE = re.compile(r"임원ㆍ주요주주특정증권등소유상황보고서|주식등의대량보유상황보고서|최대주주등소유주식변동신고서")
US_BAD_FORMS = {"S-1", "S-1/A", "S-3", "S-3/A", "F-1", "F-1/A", "F-3", "F-3/A", "424B1", "424B2", "424B3", "424B4", "424B5",
                "424B7", "25", "25-NSE", "15-12B", "15-12G", "NT 10-K", "NT 10-Q"}
US_BAD_ITEMS = {"1.03": "파산·회생", "3.01": "상장폐지 통보", "4.02": "재무제표 신뢰 불가"}
US_FORM_KO = {"8-K": "주요 사항", "6-K": "해외기업 수시공시", "10-Q": "분기보고서", "10-K": "연간보고서", "SC 13D": "지분 5% 이상(경영 참여)",
              "SC 13G": "지분 5% 이상", "4": "임원 매매", "S-1": "상장·증자 신고", "S-3": "증자 신고", "424B5": "증자 확정",
              "25-NSE": "상장폐지", "25": "상장폐지"}
US_ITEM_KO = {"1.01": "중요 계약", "2.02": "실적 발표", "7.01": "투자자 공시", "8.01": "기타 중요 사항", "5.02": "임원 변경",
              "2.01": "인수·매각 완료", "3.02": "주식 발행", "1.03": "파산", "3.01": "상장폐지 통보", "4.02": "재무제표 정정"}


def _cached(key: str, fn) -> dict:
    with _lock:
        hit = _mem.get(key)
        if hit and time.time() - hit[0] < TTL:
            return hit[1]
    try:
        out = fn()
    except Exception as e:
        out = {"ok": False, "why": f"확인 실패 ({type(e).__name__})", "bad": [], "items": []}
    with _lock:
        _mem[key] = (time.time(), out)
    return out


# ── 판정 (인터넷 없이 — 자체 점검에서도 씀) ──
def classify_kr(titles: list[str]) -> dict:
    bad, items = [], []
    for t in titles:
        if KR_ROUTINE.search(t):
            continue
        (bad if KR_BAD.search(t) and not KR_OK_EXCEPT.search(t) else items).append(t)
    return {"bad": bad, "items": items}


def classify_us(rows: list[dict]) -> dict:
    """rows: [{"form", "items", "desc"}]"""
    bad, items = [], []
    for r in rows:
        form, its = r.get("form", ""), [i.strip() for i in (r.get("items") or "").split(",") if i.strip()]
        if form == "4":      # 임원 매매는 매일 쏟아져서 이유로 안 봄
            continue
        label = f"{form} {US_FORM_KO.get(form, '')}".strip()
        if its:
            label += " (" + ", ".join(US_ITEM_KO.get(i, i) for i in its) + ")"
        if r.get("desc"):
            label += f" — {r['desc'][:60]}"
        if form in US_BAD_FORMS or any(i in US_BAD_ITEMS for i in its):
            bad.append(label)
        else:
            items.append(label)
    return {"bad": bad, "items": items}


# ── 국장: DART ──
def _kr(sym: str) -> dict:
    from . import dart
    if not dart.enabled():
        return {"ok": False, "why": "DART 키 없음", "bad": [], "items": []}
    hit = dart.lookup(sym)
    if not hit:
        return {"ok": False, "why": "DART 에 없는 종목", "bad": [], "items": []}
    end = datetime.now(timezone(timedelta(hours=9)))
    d = dart._get("list.json", corp_code=hit[0], bgn_de=(end - timedelta(days=1)).strftime("%Y%m%d"),
                  end_de=end.strftime("%Y%m%d"), page_count=50)
    titles = [re.sub(r"\s+", " ", x["report_nm"]).strip() for x in d.get("list", [])]
    return {"ok": True, "src": "DART", **classify_kr(titles)}


# ── 미장: SEC EDGAR ──
def _ua() -> str:
    mail = os.getenv("SEC_CONTACT_EMAIL", "").strip()
    return f"CompanyAI-personal {mail}" if mail else ""


def _sec(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _ua(), "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=12) as r:
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip
            body = gzip.decompress(body)
    return json.loads(body)


def _cik(sym: str) -> int | None:
    f = COMPANY_DIR / "_cache" / "sec_tickers.json"
    try:
        if f.exists() and time.time() - f.stat().st_mtime < 7 * 86400:
            m = json.loads(f.read_text(encoding="utf-8"))
        else:
            raw = _sec("https://www.sec.gov/files/company_tickers.json")
            m = {v["ticker"].upper(): int(v["cik_str"]) for v in raw.values()}
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(m), encoding="utf-8")
    except Exception:
        return None
    return m.get(sym.upper().replace(".", "-"))


def _us(sym: str) -> dict:
    if not _ua():
        return {"ok": False, "why": "SEC 연락 이메일 없음 (🧰 도구)", "bad": [], "items": []}
    cik = _cik(sym)
    if not cik:
        return {"ok": False, "why": "SEC 에 없는 종목", "bad": [], "items": []}
    rec = _sec(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")["filings"]["recent"]
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = []
    for i, acc in enumerate(rec.get("acceptanceDateTime", [])):
        try:
            t = datetime.fromisoformat(acc.replace("Z", "+00:00"))
        except ValueError:
            continue
        if t < since:
            break        # 최신순
        rows.append({"form": rec["form"][i], "items": (rec.get("items") or [""] * (i + 1))[i],
                     "desc": (rec.get("primaryDocDescription") or [""] * (i + 1))[i]})
    return {"ok": True, "src": "SEC", **classify_us(rows)}


def check(sym: str, market: str) -> dict:
    """{"ok", "src", "bad": [...], "items": [...], "why"} — 20분 보관"""
    return _cached(f"{market}:{sym}", lambda: _kr(sym) if market == "KR" else _us(sym))


def note(nw: dict | None) -> str:
    """주문 이유·AI 판단에 붙일 한 줄"""
    if not nw or not nw.get("ok"):
        return ""
    src = nw.get("src", "")
    if nw["bad"]:
        return f"📰 {src} 악재: " + " / ".join(nw["bad"][:2])
    if nw["items"]:
        return f"📰 {src}: " + " / ".join(nw["items"][:2])
    return f"📰 {src}: 최근 24시간 공시 없음 (급등 이유 모름)"


def enabled(market: str) -> bool:
    if market == "KR":
        from . import dart
        return dart.enabled()
    return bool(_ua())
