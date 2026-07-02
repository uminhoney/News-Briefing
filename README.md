# 📊 Market Briefing Bot

investing.com 뉴스를 수집·분석하여 하루 2회(오전 07:30 / 오후 16:30 KST) 텔레그램으로 마켓 브리핑을 발송하는 자동화 시스템.

## 브리핑 구성

| | 🌅 오전 07:30 (모닝 브리핑) | 🌆 오후 16:30 (마감 브리핑) |
|---|---|---|
| 수집 범위 | 전 영업일 16:20 ~ 당일 07:20 기사 | 당일 07:20 ~ 16:20 기사 |
| 섹션 1 | 미국 지수 마감 + 등락 요인 상세 분석 | 아시아 세션 이슈 (일·중·한) |
| 섹션 2 | 금리·달러·유가·금 (전일比 등락폭) | 국내시장 주요 뉴스 1~2개 |
| 섹션 3 | 미국시장 주요 뉴스 3~5개 | 아시아·글로벌 주요 뉴스 3~4개 |
| 섹션 4 | 오늘 한국시장 체크포인트 | 밤사이 미국 지표·실적 일정 |
| 섹션 5 | 당일 주요 일정 | 미국 선물(주식·금리·금)·프리마켓 |

시간 창이 겹치지 않으므로 오전/오후 브리핑 간 중복 뉴스는 구조적으로 발생하지 않으며,
같은 창 안에서는 URL·제목 정규화 기반으로 중복을 제거합니다.

### 주말 처리 (월요일 오전 브리핑)

월요일 오전 브리핑은 **금요일 16:20 ~ 월요일 07:20 (약 63시간)** 을 커버합니다.
문제는 RSS 피드가 최근 기사 20~40건만 유지해서, 월요일 아침에 피드를 읽으면
금요일 밤~토요일 기사가 이미 밀려나 있다는 점입니다. 이를 해결하기 위해:

1. **주말 수집 잡(`collect` 모드)** 이 토·일 각 2회 실행되어 기사를
   `data/weekend_articles.json`에 누적 저장 (텔레그램 발송 없음, 저장소에 커밋)
2. **월요일 오전 브리핑**이 저장된 주말 기사 + 실시간 RSS를 병합·중복 제거
   (수집 한도도 60건 → 90건으로 확대)
3. 월요일 오전에는 **"주말 주요 이슈" 섹션이 추가**되어 금요일 오후 이후
   지정학·기업 발표·크립토 주말 급등락 등을 별도로 정리
4. 저장소의 지난 주말 기사는 시간 창 필터로 자동 배제되므로 별도 청소 불필요

### 전체 실행 스케줄 (KST 기준)

| 요일 | 시각 | 모드 | 동작 |
|---|---|---|---|
| 월~금 | 07:20 | morning | 브리핑 생성 + 발송 |
| 월~금 | 16:20 | afternoon | 브리핑 생성 + 발송 |
| 토 | 16:20 | collect | 금 16:20 이후 기사 저장 (발송 없음) |
| 일 | 04:20 | collect | 〃 (금요일 미국 마감 기사 확보) |
| 일 | 16:20 | collect | 〃 |
| 월 | 04:20 | collect | 〃 (일요일 밤 기사 확보 후 오전 브리핑으로 연결) |

## 데이터 소스

- **AI 분석**: Google Gemini(무료 티어) 또는 Anthropic Claude(유료) — Secret으로 선택
- **뉴스**: investing.com 공식 RSS (속보/주식/경제/지표/외환/원자재/실적/시장분석 + kr판 국내 피드)
- **시세**: yfinance (지수 마감, 금리, 달러인덱스, WTI/브렌트/금, 미국 선물, 아시아 지수)
- **보완**: Claude API 웹 검색 (경제 캘린더, 프리마켓 동향 등 RSS에 없는 정보)

---

## 🚀 설치 가이드

### 1단계. 텔레그램 봇 생성 (5분)

1. 텔레그램에서 **@BotFather** 검색 → 대화 시작
2. `/newbot` 입력
3. 봇 이름 입력 (예: `헌이 마켓브리핑`)
4. 봇 username 입력 — 반드시 `bot`으로 끝나야 함 (예: `honey_market_bot`)
5. BotFather가 주는 **토큰**을 복사해 안전하게 보관
   - 형식: `1234567890:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`
   - ⚠️ 이 토큰이 유출되면 누구나 봇을 조종할 수 있으니 절대 코드에 하드코딩하지 말 것

### 2단계. chat_id 확인

1. 방금 만든 봇을 검색해서 **대화 시작** 버튼을 누르고 아무 메시지나 하나 전송
   (이 단계를 건너뛰면 봇이 먼저 말을 걸 수 없어 발송이 실패함)
2. 브라우저에서 아래 URL 접속 (`<토큰>` 부분 교체):
   ```
   https://api.telegram.org/bot<토큰>/getUpdates
   ```
3. 응답 JSON에서 `"chat":{"id":123456789,...}` 의 숫자가 **chat_id**

> 💡 채널로 받고 싶다면: 채널 생성 → 봇을 관리자로 추가 → 채널에 메시지 하나 작성 후
> getUpdates 확인. 채널 chat_id는 `-100`으로 시작하는 음수.

### 3단계. AI API 키 발급 — 두 가지 중 택 1

**옵션 A. Google Gemini (무료, 권장 시작점)**
1. [aistudio.google.com](https://aistudio.google.com) 접속 → 구글 계정 로그인
2. 좌측 또는 우측 상단의 **Get API key → Create API key** 클릭
3. `AIza...`로 시작하는 키 복사 — **카드 등록·결제 불필요**
4. 무료 한도(분당/일일 요청 제한)가 있지만 하루 2회 브리핑에는 충분
   ⚠️ 무료 티어는 입력 데이터가 구글 모델 개선에 활용될 수 있음.
   이 봇은 공개 뉴스와 시세만 다루므로 문제없지만, 민감 정보 용도로는 부적합.

**옵션 B. Anthropic Claude (유료, 요약 품질 우선 시)**
1. [console.anthropic.com](https://console.anthropic.com) → API Keys에서 키 생성
2. Billing에서 크레딧 충전 필요. 브리핑 1회당 약 $0.05~0.15 → 월 $3~7 내외

> 스크립트는 `GEMINI_API_KEY`가 등록되어 있으면 Gemini를, 없으면
> `ANTHROPIC_API_KEY`를 사용합니다. 둘 다 등록 시 Gemini 우선.

### 4단계. GitHub 저장소 설정

1. 이 폴더 구조 그대로 **private 저장소** 생성 후 push:
   ```
   market-briefing/
   ├── briefing.py
   ├── requirements.txt
   └── .github/workflows/briefing.yml
   ```
2. 저장소 → **Settings → Secrets and variables → Actions → New repository secret**으로
   아래 3개 등록:

   | Secret 이름 | 값 | 비고 |
   |---|---|---|
   | `GEMINI_API_KEY` | Google AI Studio 키 (`AIza...`) | 무료 — 옵션 A 선택 시 |
   | `ANTHROPIC_API_KEY` | Anthropic API 키 (`sk-ant-...`) | 유료 — 옵션 B 선택 시 |
   | `TELEGRAM_BOT_TOKEN` | BotFather 토큰 | 필수 |
   | `TELEGRAM_CHAT_ID` | chat_id 숫자 | 필수 |

   → AI 키는 둘 중 **하나만** 등록하면 됩니다.

### 5단계. 테스트

1. 저장소 → **Actions** 탭 → `Daily Market Briefing` 워크플로우 선택
2. **Run workflow** 클릭 → mode 선택(morning/afternoon) → 실행
3. 텔레그램으로 브리핑이 도착하면 완료 🎉
   (발송 없이 로그로만 확인하려면 dry_run 체크)

로컬 테스트:
```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
python briefing.py --mode morning --dry-run
```

---

## ⚙️ 운영 참고사항

- **스케줄 지연**: GitHub Actions cron은 3~15분 지연될 수 있어 07:20/16:20(KST)으로
  10분 일찍 설정되어 있음. 정시성이 중요해지면 Cloud Run Jobs / AWS Lambda로 이전 권장.
- **요일**: 브리핑은 월~금만 발송. 주말에는 수집 잡만 조용히 돌며, 월요일 오전
  브리핑이 금요일 미국 마감 + 주말 이슈를 한 번에 정리해서 발송.
- **주말 저장소 커밋**: collect 잡이 `data/weekend_articles.json`을 저장소에
  커밋하므로 워크플로우에 `permissions: contents: write`가 필요 (이미 설정됨).
  브랜치 보호 규칙을 쓴다면 bot 커밋 허용 필요.
- **RSS 차단 시**: GitHub Actions IP에서 investing.com RSS가 간헐적으로 막힐 경우,
  스크립트는 실패한 피드를 건너뛰고 Claude 웹 검색으로 보완하도록 설계됨.
  전 피드가 지속적으로 막히면 self-hosted runner 또는 프록시 검토.
- **저작권**: 기사 원문을 전재하지 않고 자체 문장으로 요약 + 원문 링크 첨부 구조.
  개인용 브리핑 기준이며, 외부 공개 채널로 확장 시 별도 검토 필요.
- **휴장일**: 한국/미국 공휴일에도 실행되지만 "휴장" 취지의 브리핑이 감. 필요 시
  휴장일 스킵 로직(exchange_calendars 라이브러리) 추가 가능.
