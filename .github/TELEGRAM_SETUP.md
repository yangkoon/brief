# 새 브리프 발행 시 텔레그램 채널 자동 배포 가이드

`main` 브랜치에 새로운 브리프(`AEC_Brief_XX.html`, `AI+_Brief_XX.html`)가 추가되면 에디션별 전용 텔레그램 채널로 **1면 캡처 이미지 + 톱 헤드라인 + 이번 주 3줄 요약 + 바로가기 버튼**이 자동 배포됩니다.

---

## 1단계: AEC Brief 채널 연결 (`@aec_brief_bot`)

> **중요 체크포인트:** `@aec_brief_bot`은 메시지를 대신 올려주는 **봇(Bot)** 입니다. 구독자들이 받아보는 공간은 별도의 **텔레그램 채널(Channel)** 이어야 하며, `TELEGRAM_CHAT_ID`에는 봇 아이디(`@aec_brief_bot`)가 아니라 **채널 아이디**(예: `@aec_brief` 또는 `-100...`)를 넣어야 합니다.

1. 텔레그램에서 **새 채널(New Channel)** 생성 (예: 이름 `AEC Brief`)
   - **공개 채널**로 설정하면 고유 주소(예: `t.me/aec_brief` → 채널 ID: `@aec_brief`)를 바로 쓸 수 있어 가장 간편합니다.
2. 생성한 채널의 **`채널 정보` → `관리자(Administrators)` → `관리자 추가`** 클릭
3. **`@aec_brief_bot`** 을 검색해 선택하고 **`메시지 게시(Post messages)`** 권한을 켠 뒤 저장
4. **채널 Chat ID 확인**:
   - **공개 채널**: `@채널주소` 그대로 사용 (예: `@aec_brief`, `https://t.me/aec_brief`로 넣어도 자동 변환됨)
   - **비공개 채널**: 채널에 아무 테스트 글이나 1개 올린 뒤 로컬에서 아래 명령을 실행하면 `-100xxxxxxxxxx` 형태의 숫자 ID가 출력됩니다:
     ```bash
     TELEGRAM_BOT_TOKEN="봇파더_토큰" python3 .github/scripts/send_telegram_brief.py --discover-chats
     ```

---

## 2단계: GitHub Secrets 구성 (2개 채널 분리 지원)

저장소의 **`Settings` → `Secrets and variables` → `Actions`** 기준입니다.

### 현재 (AEC 채널 우선 운영)
이미 등록하신 기본 시크릿 2개를 그대로 인식해 `AEC_Brief_*.html` 발송에 사용합니다:

| Secret 이름 | 대상 에디션 | 값 설명 |
| :--- | :---: | :--- |
| `TELEGRAM_BOT_TOKEN` | **AEC Brief** (기본) | `@BotFather`에서 발급받은 `@aec_brief_bot` 토큰 |
| `TELEGRAM_CHAT_ID` | **AEC Brief** (기본) | `@aec_brief_bot`이 관리자로 들어가 있는 **AEC 채널 ID** (`@채널명` 또는 `-100...`) |

*(안전장치: `AI+_Brief_*.html`이 푸시되더라도 `AI_TELEGRAM_CHAT_ID`가 설정되기 전까지는 AEC 채널로 섞여 발송되지 않고 자동 스킵됩니다.)*

### 추후 AI+ Brief 채널 추가 시
워크플로 코드 수정 없이 아래 시크릿만 추가하면 즉시 두 채널이 각각 독립 가동됩니다:

| Secret 이름 | 필수 여부 | 값 설명 |
| :--- | :---: | :--- |
| `AI_TELEGRAM_CHAT_ID` | **필수** (AI+ 개설 시) | AI+ Brief 전용 텔레그램 **채널 ID** (`@채널명` 또는 `-100...`) |
| `AI_TELEGRAM_BOT_TOKEN` | 선택 | 별도 봇(예: `@ai_brief_bot`)을 만든 경우 등록. 비워두면 `TELEGRAM_BOT_TOKEN`(`@aec_brief_bot`)을 AI+ 채널에도 관리자로 초대해 공용으로 사용 가능 |
| `AEC_TELEGRAM_BOT_TOKEN` / `AEC_TELEGRAM_CHAT_ID` | 선택 | 추후 AEC 전용 시크릿 이름을 명시적으로 분리하고 싶을 때 사용 (설정 시 `TELEGRAM_*`보다 우선 적용) |

---

## 3단계: 테스트 발송 및 운영 규칙

### GitHub에서 즉시 수동 테스트 발송
1. 저장소 상단 **`Actions`** 탭 → 좌측 **`Telegram Brief Notify`** 선택
2. 우측 **`Run workflow`** 클릭 (`target_file` 기본값: `latest-aec` → 최신 호인 `AEC_Brief_02.html` 선택됨)
3. AEC 텔레그램 채널에 **1면 이미지 + 요약문 + [전문 읽기] [아카이브] 버튼**이 올라오는지 확인합니다.

### 로컬 미리보기 테스트 (API 호출 없음)
```bash
python3 .github/scripts/send_telegram_brief.py --dry-run AEC_Brief_02.html
```

### 자동 발송 규칙
- **신규 호 발행**: `main` 브랜치에 새 `AEC_Brief_XX.html` 파일이 **신규 추가(`Add`)** 되면 웹 페이지 배포 완료(HTTP 200)를 확인한 직후 자동 발송됩니다.
- **오타 수정 시 중복 방지**: 이미 발행된 HTML을 수정(`Modify`)해 푸시할 때는 채널에 중복 발송되지 않습니다.
- **수정본 강제 재발송**: 커밋 메시지에 `[발송]` 또는 `[telegram]`을 포함해 푸시하면 수정된 호도 다시 발송됩니다.
