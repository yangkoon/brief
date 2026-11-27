# 텔레그램 브리프 배포 시 Make.com을 통한 페이스북·링크드인 자동 등록 가이드

`Telegram Brief Notify` 워크플로(`brief.yangkoon.com/admin`의 **텔레그램 발송** 버튼 또는 GitHub Actions 수동 실행)로 새 브리프가 배포되면, 후속 작업(`notify-make-sns`)이 **Make.com Custom Webhook**으로 브리프 데이터(제목·요약·링크·1면 썸네일 URL·SNS 완성 문구)를 자동 전송합니다.

> **안전장치:**
> - `edit_message_id`를 입력해 기존 텔레그램 메시지만 수정할 때는 SNS에 중복 등록되지 않습니다.
> - `MAKE_WEBHOOK_URL` 시크릿이 아직 등록되지 않은 상태에서는 Make.com 단계만 조용히 건너뛰며, 기존 텔레그램 발송에는 영향을 주지 않습니다.

---

## 1단계: Make.com에서 Custom Webhook 생성

1. [Make.com](https://www.make.com/) 로그인 후 **Scenarios** → **Create a new scenario** 클릭
2. 첫 번째 트리거 모듈로 **Webhooks** → **Custom webhook** 선택
3. **Add** 버튼 클릭 후 이름(예: `yangkoon-brief-sns`) 입력 → **Save**
4. 생성된 **Webhook URL**(`https://hook.eu2.make.com/...` 또는 `https://hook.us1.make.com/...`)을 복사해 둡니다.
5. **Re-determine data structure** (또는 **Listen for new data**) 대기 상태로 둡니다. *(2단계 등록 후 GitHub에서 테스트를 1회 실행하면 데이터 구조가 자동 인식됩니다.)*

---

## 2단계: GitHub Secrets 등록

저장소(`yangkoon/brief`)의 **Settings → Secrets and variables → Actions → New repository secret**에서 아래 시크릿을 등록합니다.

### 기본 구성 (시나리오 1개로 AEC / AI+ 모두 처리 시 — 권장)

| Secret 이름 | 필수 여부 | 값 설명 |
| :--- | :---: | :--- |
| `MAKE_WEBHOOK_URL` | **필수** | Make.com Custom Webhook URL |

### 선택 구성 (AEC Brief / AI+ Brief를 서로 다른 페이스북 페이지·링크드인으로 분리 시)

| Secret 이름 | 설명 |
| :--- | :--- |
| `MAKE_AEC_WEBHOOK_URL` | AEC Brief 전용 Webhook URL (설정 시 `MAKE_WEBHOOK_URL`보다 우선) |
| `MAKE_AI_WEBHOOK_URL` | AI+ Brief 전용 Webhook URL (설정 시 `MAKE_WEBHOOK_URL`보다 우선) |

*(참고: 하나의 `MAKE_WEBHOOK_URL`만 쓰고, Make.com 내부에서 **Router** 필터로 `edition_key == "aec"` / `edition_key == "ai"` 분기 처리하는 방식이 가장 관리하기 쉽습니다.)*

---

## 3단계: Make.com 데이터 구조 인식 (테스트 전송 1회)

1. Make.com 시나리오의 Webhook 모듈에서 **Determine data structure**가 대기 중인지 확인
2. GitHub 저장소의 **Actions** → **Make.com SNS Notify (Facebook & LinkedIn)** → **Run workflow** 클릭 (`target_file`: `latest-aec`, `dry_run`: `false`)
3. Make.com에 **Successfully determined** 메시지가 뜨면 데이터 구조 인식 완료!

### 전달되는 JSON 필드 목록

| 필드명 | 설명 | 예시 |
| :--- | :--- | :--- |
| `edition_key` | 에디션 구분자 | `"aec"` 또는 `"ai"` |
| `brand` | 브랜드명 | `"AEC Brief"` / `"AI+ Brief"` |
| `edition_date` | 호수 및 발행일 | `"제3호 · 2026.11.30(월)"` |
| `lead_title` | 톱 헤드라인 | `"“AI가 그려도 서명 책임은 건축사”…"` |
| `cover_label` / `cover_value` | 이번 호 숫자 라벨 / 수치 | `"5.6만호"` |
| `three_lines_text` | 핵심 3줄 요약 (줄바꿈 포함) | `"1. ...\n2. ...\n3. ..."` |
| `page_url` | 브리프 웹페이지 URL | `"https://brief.yangkoon.com/AEC_Brief_03"` |
| `hub_url` | 아카이브 메인 URL | `"https://brief.yangkoon.com"` |
| `thumb_url` | 1면 캡처 PNG 공개 URL | `"https://raw.githubusercontent.com/yangkoon/brief/main/thumbs/AEC_Brief_03.png"` |
| `hashtags` | 에디션별 기본 해시태그 | `"#AECBrief #스마트건설 ..."` |
| `facebook_message` | 페이스북용 완성 본문 (제목+숫자+3줄요약+링크+해시태그) | 바로 매핑 가능 |
| `linkedin_message` | 링크드인용 완성 본문 (제목+숫자+3줄요약+링크+해시태그) | 바로 매핑 가능 |

---

## 4단계: Make.com 시나리오 연결 (Facebook & LinkedIn)

Webhook 모듈 뒤에 **Router**를 연결해 Facebook과 LinkedIn 브랜치로 나눕니다.

### A. Facebook Pages 연결
1. **Facebook Pages** → **Create a Post** (또는 이미지 강조 시 **Create a Photo Post**) 모듈 추가
   - **참고**: 페이스북은 **개인 프로필** 자동 게시를 API에서 막고 있으므로, 반드시 **Facebook 페이지(Page)** 를 연결해야 합니다.
2. **Connection**: Facebook 계정 로그인 및 게시할 페이지 권한 허용
3. 필드 매핑:
   - **Create a Post (링크 카드형 — 클릭 유입 추천)**:
     - **Page**: 게시할 Facebook 페이지 선택
     - **Message**: `{{1.facebook_message}}`
     - **Link**: `{{1.page_url}}`
   - **Create a Photo Post (1면 썸네일 이미지형)**:
     - **Page**: 게시할 Facebook 페이지 선택
     - **Photo URL**: `{{1.thumb_url}}`
     - **Caption**: `{{1.facebook_message}}`

### B. LinkedIn 연결
1. **LinkedIn** 모듈 추가:
   - **개인 프로필** 게시: **Create a Text Post** 또는 **Create a Share (Article/URL)**
   - **회사 페이지(Organization)** 게시: **Create an Organization Text Post** / **Create an Organization Image Post**
2. **Connection**: LinkedIn 계정 로그인 및 게시 권한 허용
   - *(참고: LinkedIn OAuth 토큰은 60일마다 만료되므로 Make.com Connections에서 주기적으로 Reauthorize 안내가 올 수 있습니다.)*
3. 필드 매핑 (링크 공유형 기준):
   - **Content / Commentary**: `{{1.linkedin_message}}`
   - **Title** (Article/Link 항목이 있는 경우): `{{1.brand}} · {{1.lead_title}}`
   - **Original URL / Content URL**: `{{1.page_url}}`
   - *(이미지로 올리고 싶다면 LinkedIn 모듈 앞에 **HTTP → Get a file** 모듈을 넣어 `{{1.thumb_url}}` 파일을 받아 **Create an Image Post**에 `Data`를 매핑하세요.)*

### C. 시나리오 활성화
1. Make.com 좌측 하단 **Scheduling** 스위치를 **ON** (`Immediately as data arrives`)으로 켭니다.
2. 시나리오를 **Save** 합니다.

---

## 5단계: 운영 및 테스트 방법

- **평소 운영**: `brief.yangkoon.com/admin`에서 **텔레그램 발송** 버튼을 누르거나 GitHub Actions에서 **Telegram Brief Notify**를 실행하면 텔레그램 발송 완료 직후 Make.com을 통해 Facebook과 LinkedIn에 자동 등록됩니다.
- **SNS만 건너뛰고 텔레그램만 발송**: GitHub Actions → **Telegram Brief Notify** → `skip_sns` 체크 후 실행
- **텔레그램 없이 SNS만 테스트/재발송**: GitHub Actions → **Make.com SNS Notify (Facebook & LinkedIn)** 실행
