#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Investing.com 데일리 마켓 브리핑 → 텔레그램 발송
- 오전 07:30 KST: 미국 마감 브리핑 (전일 오후 브리핑 이후 기사)
- 오후 16:30 KST: 아시아 마감 + 미국 프리뷰 브리핑 (오전 브리핑 이후 기사)

사용법:
    python briefing.py --mode morning
    python briefing.py --mode afternoon

필요 환경변수:
    ANTHROPIC_API_KEY   : Anthropic API 키
    TELEGRAM_BOT_TOKEN  : 텔레그램 봇 토큰 (BotFather 발급)
    TELEGRAM_CHAT_ID    : 발송 대상 chat_id
"""

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import feedparser
import requests

KST = ZoneInfo("Asia/Seoul")

# 브리핑 기준 시각 (GitHub Actions cron 지연을 감안해 실제 cron은 10분 일찍 실행)
MORNING_ANCHOR = (7, 20)    # 오전 브리핑 시간 창의 끝 기준
AFTERNOON_ANCHOR = (16, 20)  # 오후 브리핑 시간 창의 끝 기준

# ---------------------------------------------------------------------------
# 1. RSS 피드 정의 (investing.com 공식 제공: /webmaster-tools/rss)
# ---------------------------------------------------------------------------
FEEDS_GLOBAL = {
    "속보":       "https://www.investing.com/rss/news_462.rss",
    "주식시장":   "https://www.investing.com/rss/news_25.rss",
    "경제":       "https://www.investing.com/rss/news_14.rss",
    "경제지표":   "https://www.investing.com/rss/news_95.rss",
    "외환":       "https://www.investing.com/rss/news_1.rss",
    "원자재":     "https://www.investing.com/rss/news_11.rss",
    "실적":       "https://www.investing.com/rss/news_1062.rss",
    "시장분석":   "https://www.investing.com/rss/market_overview.rss",
}

# 국내/아시아 시장용 (한국어판 미러 피드) — 오후 브리핑에서 주로 사용
FEEDS_KOREA = {
    "국내주식":   "https://kr.investing.com/rss/news_25.rss",
    "국내경제":   "https://kr.investing.com/rss/news_14.rss",
}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

MAX_ARTICLES_PER_FEED = 40   # 피드당 최대 수집 건수
MAX_ARTICLES_TO_LLM = 60     # LLM에 전달할 최대 기사 수
MAX_ARTICLES_TO_LLM_MONDAY = 90  # 월요일 오전(주말 포함 63시간 창)은 한도 확대

# 주말 기사 저장 파일 (RSS는 최근 기사만 유지하므로 주말 동안 미리 수집·보관)
WEEKEND_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "weekend_articles.json")


# ---------------------------------------------------------------------------
# 2. 시간 창 계산 (중복 제거의 핵심: 브리핑 간 시간 창이 겹치지 않음)
# ---------------------------------------------------------------------------
def _last_friday_afternoon(now: datetime) -> datetime:
    """현재 시각 기준 직전(또는 당일) 금요일 16:20 KST"""
    d = now.date()
    while d.weekday() != 4:  # 금요일(4)까지 거슬러 감
        d -= timedelta(days=1)
    return datetime(d.year, d.month, d.day, *AFTERNOON_ANCHOR, tzinfo=KST)


def get_time_window(mode: str, now: datetime):
    """
    morning   : 직전 영업일 16:20 KST ~ 현재 (월요일이면 금요일 16:20 → 주말 포함)
    afternoon : 당일 07:20 KST ~ 현재
    collect   : 직전 금요일 16:20 KST ~ 현재 (주말 수집 전용, 발송 없음)
    """
    if mode == "morning":
        d = now.date() - timedelta(days=1)
        while d.weekday() >= 5:  # 토(5), 일(6) 건너뜀 → 월요일 오전은 금요일 오후까지
            d -= timedelta(days=1)
        start = datetime(d.year, d.month, d.day, *AFTERNOON_ANCHOR, tzinfo=KST)
    elif mode == "collect":
        start = _last_friday_afternoon(now)
    else:
        start = now.replace(hour=MORNING_ANCHOR[0], minute=MORNING_ANCHOR[1],
                            second=0, microsecond=0)
    return start, now


# ---------------------------------------------------------------------------
# 3. RSS 수집 + 중복 제거
# ---------------------------------------------------------------------------
def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=ZoneInfo("UTC")).astimezone(KST)
    return None


def _normalize_title(title: str) -> str:
    t = re.sub(r"[^0-9a-zA-Z가-힣]+", " ", title.lower())
    return " ".join(t.split())


def collect_articles(feeds: dict, start: datetime, end: datetime) -> list[dict]:
    articles, seen_urls, seen_titles = [], set(), set()
    for category, url in feeds.items():
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as e:
            print(f"[WARN] 피드 수집 실패 ({category}): {e}", file=sys.stderr)
            continue

        for entry in parsed.entries[:MAX_ARTICLES_PER_FEED]:
            pub = _entry_time(entry)
            if pub is None or not (start <= pub <= end):
                continue

            link = entry.get("link", "").split("?")[0]
            title = (entry.get("title") or "").strip()
            norm = _normalize_title(title)
            if not title or not link:
                continue
            if link in seen_urls or norm in seen_titles:  # 피드 간 중복 제거
                continue
            seen_urls.add(link)
            seen_titles.add(norm)

            summary = re.sub(r"<[^>]+>", "", entry.get("summary", "") or "")
            articles.append({
                "category": category,
                "title": title,
                "summary": summary.strip()[:300],
                "link": link,
                "published_iso": pub.isoformat(),
                "published_kst": pub.strftime("%m/%d %H:%M"),
            })
        time.sleep(0.5)  # 피드 서버 배려

    articles.sort(key=lambda a: a["published_iso"], reverse=True)
    print(f"[INFO] 수집 기사: {len(articles)}건 ({start:%m/%d %H:%M} ~ {end:%m/%d %H:%M} KST)")
    return articles


# ---------------------------------------------------------------------------
# 3-1. 주말 기사 저장소 (RSS 롤오프 대비)
#      토·일 collect 모드가 기사를 JSON에 누적 → 월요일 오전 브리핑이 병합 사용
# ---------------------------------------------------------------------------
def load_weekend_store(start: datetime, end: datetime) -> list[dict]:
    if not os.path.exists(WEEKEND_STORE):
        return []
    try:
        with open(WEEKEND_STORE, encoding="utf-8") as f:
            stored = json.load(f)
    except Exception as e:
        print(f"[WARN] 주말 저장소 로드 실패: {e}", file=sys.stderr)
        return []
    result = []
    for a in stored:
        try:
            pub = datetime.fromisoformat(a["published_iso"])
            if start <= pub <= end:  # 지난 주말 기사 등 창 밖 데이터는 자동 배제
                result.append(a)
        except Exception:
            continue
    print(f"[INFO] 주말 저장소에서 {len(result)}건 로드")
    return result


def merge_dedup(*article_lists: list[dict]) -> list[dict]:
    merged, seen_urls, seen_titles = [], set(), set()
    for lst in article_lists:
        for a in lst:
            norm = _normalize_title(a["title"])
            if a["link"] in seen_urls or norm in seen_titles:
                continue
            seen_urls.add(a["link"])
            seen_titles.add(norm)
            merged.append(a)
    merged.sort(key=lambda a: a.get("published_iso", ""), reverse=True)
    return merged


def save_weekend_store(articles: list[dict]):
    os.makedirs(os.path.dirname(WEEKEND_STORE), exist_ok=True)
    with open(WEEKEND_STORE, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=1)
    print(f"[INFO] 주말 저장소 저장: {len(articles)}건 → {WEEKEND_STORE}")


# ---------------------------------------------------------------------------
# 4. 시세 스냅샷 (yfinance) — 지수/금리/달러/유가/금/선물은 정확한 숫자로 주입
# ---------------------------------------------------------------------------
MORNING_TICKERS = [
    ("다우존스", "^DJI", "pt"), ("S&P500", "^GSPC", "pt"), ("나스닥", "^IXIC", "pt"),
    ("필라델피아반도체", "^SOX", "pt"), ("VIX", "^VIX", "pt"),
    ("미국채 10년물", "^TNX", "%"), ("미국채 2년물(5Y대용 ^FVX)", "^FVX", "%"),
    ("달러인덱스", "DX-Y.NYB", "pt"), ("원/달러", "KRW=X", "원"),
    ("WTI", "CL=F", "$"), ("브렌트유", "BZ=F", "$"), ("금", "GC=F", "$"),
    ("비트코인", "BTC-USD", "$"),
]

AFTERNOON_TICKERS = [
    ("코스피", "^KS11", "pt"), ("코스닥", "^KQ11", "pt"),
    ("닛케이225", "^N225", "pt"), ("항셍", "^HSI", "pt"), ("상해종합", "000001.SS", "pt"),
    ("원/달러", "KRW=X", "원"), ("달러/엔", "JPY=X", "엔"),
    ("S&P500 선물", "ES=F", "pt"), ("나스닥 선물", "NQ=F", "pt"), ("다우 선물", "YM=F", "pt"),
    ("미국채 10년 선물", "ZN=F", "pt"), ("금 선물", "GC=F", "$"), ("WTI 선물", "CL=F", "$"),
]


def market_snapshot(mode: str) -> str:
    try:
        import yfinance as yf
    except ImportError:
        return "(시세 데이터 조회 불가 — 뉴스 기사 기반으로 작성)"

    tickers = MORNING_TICKERS if mode == "morning" else AFTERNOON_TICKERS
    lines = []
    for name, symbol, unit in tickers:
        try:
            hist = yf.Ticker(symbol).history(period="7d", interval="1d")
            if len(hist) < 2:
                continue
            last, prev = float(hist["Close"].iloc[-1]), float(hist["Close"].iloc[-2])
            chg, pct = last - prev, (last / prev - 1) * 100
            if symbol in ("^TNX", "^FVX"):  # yfinance 수익률 지수는 10배 스케일
                last, prev, chg = last / 10, prev / 10, chg / 10
                lines.append(f"{name}: {last:.3f}% (전일比 {chg:+.3f}%p)")
            else:
                lines.append(f"{name}: {last:,.2f}{unit if unit!='pt' else ''} "
                             f"({chg:+,.2f}, {pct:+.2f}%)")
        except Exception as e:
            print(f"[WARN] 시세 조회 실패 ({name}/{symbol}): {e}", file=sys.stderr)
    return "\n".join(lines) if lines else "(시세 데이터 조회 불가 — 뉴스 기사 기반으로 작성)"


# ---------------------------------------------------------------------------
# 5. Claude API — 분석·요약·브리핑 생성 (웹 검색 도구로 캘린더/프리마켓 보완)
# ---------------------------------------------------------------------------
MORNING_TEMPLATE = """\
📊 <b>모닝 브리핑 | {date} 07:30</b>

<b>■ 미국 지수 마감</b>
(3대 지수 + 반도체지수 마감 수치와 등락률을 제시하고, 등락 요인을 최소 3~4줄로 상세히 분석.
어떤 섹터/종목이 지수를 움직였는지, 배경이 된 매크로/뉴스 이벤트는 무엇인지 구체적으로.)

<b>■ 금리 · 달러 · 유가 · 금</b>
(각 자산의 종가/수익률과 전일 대비 등락폭을 함께 제시하고 한 줄씩 배경 설명)

<b>■ 미국시장 주요 뉴스</b>
(주식시장·개별기업·실적·금리 등 market 관련 중요 뉴스 3~5개.
각 뉴스: 제목 한 줄 + 2~3문장 요약 + 기사 링크)
{weekend_section}
<b>■ 오늘 한국시장 체크포인트</b>
(위 미국 뉴스 중 오늘 한국 주식시장에 영향을 줄 이슈를 골라 2~4줄 브리핑.
반도체·2차전지·환율 등 국내 연관 섹터 관점에서)

<b>■ 오늘 주요 일정</b>
(당일 예정된 경제지표 발표, 연준 인사 발언, 주요 기업 실적 등을 한국시간 기준으로)
"""

AFTERNOON_TEMPLATE = """\
🌆 <b>마감 브리핑 | {date} 16:30</b>

<b>■ 아시아 세션 이슈</b>
(일본·중국·한국 관련 글로벌 뉴스와 아시아 증시 흐름. 코스피/닛케이/항셍/상해 등락 포함, 3~4줄)

<b>■ 국내시장 주요 뉴스</b>
(한국 시장 관련 중요 뉴스 1~2개. 각 뉴스: 제목 + 2문장 요약 + 링크)

<b>■ 아시아 · 글로벌 주요 뉴스</b>
(아시아 및 글로벌 시장 중요 뉴스 3~4개. 각 뉴스: 제목 + 2문장 요약 + 링크)

<b>■ 밤사이 미국 일정</b>
(오늘 밤~내일 새벽 예정된 미국 주요 경제지표, 연준 이벤트, 개별기업 실적 발표를 한국시간 기준으로)

<b>■ 미국 선물 · 프리마켓</b>
(주식/금리/금 선물 현재가와 등락, 주요 종목 프리마켓 특이 동향)
"""

SYSTEM_PROMPT = """\
당신은 한국 증권사 S&T 데스크를 위한 마켓 브리핑 작성 전문가입니다.

작성 원칙:
1. 한국어로 작성. 간결하되 중요 내용(수치·기업명·배경)은 반드시 포함.
2. 제공된 [시세 스냅샷]의 숫자를 그대로 사용 (임의로 숫자를 만들지 말 것).
3. 뉴스 요약은 제공된 [수집 기사] 목록에 기반하되, 기사 원문을 번역·전재하지 말고
   자신의 문장으로 재구성. 각 뉴스에는 원문 링크를 <a href="URL">링크</a> 형태로 첨부.
4. 중복 뉴스(같은 사건을 다룬 여러 기사)는 하나로 통합.
5. 수집 기사에 없는 정보(당일 경제 캘린더, 프리마켓 동향 등)는 웹 검색으로 보완.
   검색해도 확인 안 되는 내용은 추측하지 말고 생략.
6. 출력 형식: 텔레그램 HTML. 허용 태그는 <b>, <i>, <a href="">만 사용.
   마크다운(**,##,-) 절대 금지. 특수문자 <, >, &는 태그 외 사용 시 &lt; &gt; &amp;로 이스케이프.
7. 전체 분량 2,500~3,500자 (링크 URL 제외 기준). 불릿은 · 기호 사용.
8. 템플릿의 섹션 구조와 제목을 정확히 유지하고, 괄호 안 지시문은 실제 내용으로 대체.
9. 응답에는 브리핑 본문만 출력 (서론·후기·코드블록 금지).
"""


WEEKEND_SECTION = """
<b>■ 주말 주요 이슈</b>
(금요일 오후 브리핑 이후 주말 동안 발생한 이슈를 정리: 지정학·정치 이벤트,
기업 발표/M&A, 암호화폐 주말 급등락, 원자재 관련 소식 등. 2~4개 항목, 각 링크 첨부)
"""


def generate_briefing(mode: str, articles: list[dict], snapshot: str,
                      now: datetime) -> str:
    is_monday_morning = (mode == "morning" and now.weekday() == 0)

    if mode == "morning":
        template = MORNING_TEMPLATE.replace(
            "{weekend_section}", WEEKEND_SECTION if is_monday_morning else "")
    else:
        template = AFTERNOON_TEMPLATE

    date_str = now.strftime("%m/%d(%a)").replace("Mon", "월").replace("Tue", "화") \
        .replace("Wed", "수").replace("Thu", "목").replace("Fri", "금") \
        .replace("Sat", "토").replace("Sun", "일")

    cap = MAX_ARTICLES_TO_LLM_MONDAY if is_monday_morning else MAX_ARTICLES_TO_LLM
    articles = articles[:cap]

    article_lines = [
        f"- [{a['category']}] ({a['published_kst']}) {a['title']}\n"
        f"  요약: {a['summary']}\n  링크: {a['link']}"
        for a in articles
    ]
    articles_block = "\n".join(article_lines) if article_lines else \
        "(수집된 기사 없음 — 웹 검색으로 주요 뉴스를 직접 조사해 작성할 것)"

    monday_note = ("\n[참고] 오늘은 월요일 오전 브리핑입니다. '미국 지수 마감' 섹션은 "
                   "금요일(현지) 정규장 마감 기준으로 작성하고, 금요일 오후 이후 "
                   "주말 동안의 이슈를 '주말 주요 이슈' 섹션에서 반드시 다루세요.\n"
                   if is_monday_morning else "")

    user_prompt = f"""현재 시각: {now.strftime('%Y-%m-%d %H:%M')} KST
{monday_note}
[시세 스냅샷 — 이 숫자를 그대로 사용]
{snapshot}

[수집 기사 — investing.com RSS, 직전 브리핑 이후 발행분만]
{articles_block}

[출력 템플릿]
{template.format(date=date_str)}

위 템플릿 구조대로 브리핑을 작성하세요."""

    # ── 프로바이더 선택: GEMINI_API_KEY가 있으면 Gemini(무료), 없으면 Anthropic ──
    if os.environ.get("GEMINI_API_KEY"):
        return _call_gemini(user_prompt)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _call_anthropic(user_prompt)
    raise RuntimeError("GEMINI_API_KEY 또는 ANTHROPIC_API_KEY 중 하나를 등록하세요")


def _call_gemini(user_prompt: str) -> str:
    """Google Gemini API (무료 티어, aistudio.google.com에서 카드 등록 없이 발급).
    Google Search grounding으로 경제 캘린더·프리마켓 등 최신 정보 보완."""
    api_key = os.environ["GEMINI_API_KEY"]
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {
            "maxOutputTokens": 16384,   # 본문 잘림 방지 (한글 3,500자 + 링크 여유분)
            "temperature": 0.4,
            # Gemini 2.5는 내부 '생각(thinking)'이 출력 한도를 소모해 본문이
            # 중간에 잘릴 수 있음 → 생각 분량에 상한 설정
            "thinkingConfig": {"thinkingBudget": 1024},
        },
    }
    for attempt in range(3):
        try:
            resp = requests.post(url, params={"key": api_key},
                                 json=payload, timeout=300)
            resp.raise_for_status()
            data = resp.json()
            cand = (data.get("candidates") or [{}])[0]
            parts = cand.get("content", {}).get("parts", [])
            text = "\n".join(p.get("text", "") for p in parts).strip()
            if cand.get("finishReason") == "MAX_TOKENS":
                raise ValueError("출력이 토큰 한도로 잘림 — 재시도")
            if text:
                return text
            raise ValueError(f"빈 응답: {json.dumps(data)[:300]}")
        except Exception as e:
            print(f"[WARN] Gemini API 호출 실패 (시도 {attempt+1}/3): {e}",
                  file=sys.stderr)
            time.sleep(20)  # 무료 티어 분당 요청 제한 대비 대기
    raise RuntimeError("Gemini API 호출 3회 실패")


def _call_anthropic(user_prompt: str) -> str:
    """Anthropic Claude API (유료, 웹 검색 도구 포함)"""
    api_key = os.environ["ANTHROPIC_API_KEY"]
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
        "tools": [{"type": "web_search_20250305", "name": "web_search",
                   "max_uses": 6}],
    }
    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json=payload, timeout=300)
            resp.raise_for_status()
            data = resp.json()
            text = "\n".join(b.get("text", "") for b in data.get("content", [])
                             if b.get("type") == "text").strip()
            if text:
                return text
            raise ValueError("빈 응답")
        except Exception as e:
            print(f"[WARN] Claude API 호출 실패 (시도 {attempt+1}/3): {e}",
                  file=sys.stderr)
            time.sleep(15)
    raise RuntimeError("Claude API 호출 3회 실패")


# ---------------------------------------------------------------------------
# 6. 텔레그램 발송 (4,096자 제한 → 문단 단위 분할)
# ---------------------------------------------------------------------------
def _split_message(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for para in text.split("\n\n"):
        # 문단 하나가 한도를 넘는 극단적인 경우: 줄 단위 → 글자 단위로 강제 분할
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            piece, para = para[:cut], para[cut:].lstrip("\n")
            if current:
                chunks.append(current.strip())
                current = ""
            chunks.append(piece.strip())
        if len(current) + len(para) + 2 > limit:
            if current:
                chunks.append(current.strip())
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current.strip():
        chunks.append(current.strip())
    return chunks


def send_telegram(text: str):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    for chunk in _split_message(text):
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        resp = requests.post(url, json=payload, timeout=30)
        if resp.status_code == 400:  # HTML 파싱 오류 시 태그 제거 후 재시도
            print(f"[WARN] HTML 파싱 실패, 평문 재발송: {resp.text}",
                  file=sys.stderr)
            plain = re.sub(r"<[^>]+>", "", chunk)
            resp = requests.post(url, json={
                "chat_id": chat_id, "text": html.unescape(plain),
                "disable_web_page_preview": True}, timeout=30)
        resp.raise_for_status()
        time.sleep(1)
    print("[INFO] 텔레그램 발송 완료")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["morning", "afternoon", "collect"],
                        required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="텔레그램 발송 없이 콘솔 출력만")
    args = parser.parse_args()

    now = datetime.now(KST)
    start, end = get_time_window(args.mode, now)

    # ── collect 모드: 주말 기사 수집·저장만 하고 종료 (발송 없음) ──
    if args.mode == "collect":
        fresh = collect_articles(dict(FEEDS_GLOBAL) | dict(FEEDS_KOREA),
                                 start, end)
        stored = load_weekend_store(start, end)
        # 기존 저장분과 병합·중복 제거 후 덮어쓰기 → 여러 번 실행해도 안전(멱등)
        save_weekend_store(merge_dedup(stored, fresh))
        return

    feeds = dict(FEEDS_GLOBAL)
    if args.mode == "afternoon":
        feeds.update(FEEDS_KOREA)  # 오후 브리핑에만 국내 피드 추가

    articles = collect_articles(feeds, start, end)

    # ── 월요일 오전: 주말 저장소 병합 (RSS에서 이미 밀려난 금~일 기사 복원) ──
    if args.mode == "morning" and now.weekday() == 0:
        weekend = load_weekend_store(start, end)
        articles = merge_dedup(articles, weekend)
        print(f"[INFO] 주말 병합 후 총 {len(articles)}건")

    snapshot = market_snapshot(args.mode)
    print(f"[INFO] 시세 스냅샷:\n{snapshot}")

    briefing = generate_briefing(args.mode, articles, snapshot, now)

    if args.dry_run:
        print("\n" + "=" * 60 + "\n" + briefing)
    else:
        send_telegram(briefing)


if __name__ == "__main__":
    main()
