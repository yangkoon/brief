# 새 브리프 발행 시 카카오톡 '나에게 보내기' 자동 발송 설정 가이드

`main` 브랜치에 새로운 브리프 파일(`AEC_Brief_XX.html`, `AI+_Brief_XX.html`)이 추가되어 발행될 때마다 카카오톡 **나와의 채팅방**으로 표지 지표·1면 톱 헤드라인·바로가기 버튼이 자동 발송됩니다.

---

## 1단계: Kakao Developers 앱 생성 및 설정 (최초 1회, 약 3분)

1. [Kakao Developers 콘솔](https://developers.kakao.com/console/app)에 접속해 **[앱 생성]** 클릭 (이름 예: `Yangkoon Brief`)
2. 생성된 앱 선택 후 좌측 **`[앱] > [플랫폼 키]`** (구 UI: `앱 설정 > 앱 키`) 이동:
   - **`REST API 키`** 항목의 32자리 영문+숫자 키(예: `a1b2c3d4e5f6...`)를 복사합니다.
     *(주의: 상단의 6~7자리 숫자 `앱 ID`나 `네이티브 앱 키`, `JavaScript 키`, `어드민 키`가 아닙니다.)*
   - **`REST API 키` 상세 설정**(또는 `[카카오 로그인] > [리다이렉트 URI]`)에서 **리다이렉트 URI**에 아래 주소를 등록합니다:
     ```text
     https://localhost:5000/oauth
     ```
3. **웹 도메인 등록** (`[앱] > [제품 링크 관리] > [웹 도메인]`):
   - 아래 두 주소를 모두 등록합니다 (등록된 도메인으로만 카톡 버튼 링크가 열립니다):
     ```text
     https://brief.yangkoon.com
     https://yangkoon.github.io
     ```
4. **카카오 로그인 활성화** (`[카카오 로그인] > [설정]` 또는 `[카카오 로그인] > [사용 설정]`):
   - **활성화 설정**: `ON`
5. **동의항목 설정** (`[카카오 로그인] > [동의항목]`):
   - **`카카오톡 메시지 전송 (talk_message)`** → **[설정]** 클릭 → **`이용 중 동의`** 선택 후 저장

---

## 2단계: 최초 1회 Refresh Token 발급

### 방법 A: 내장 스크립트 사용 (권장)
```bash
# 1) 로그인 동의 URL 출력 (꺽쇠 < > 없이 32자리 REST API 키만 입력)
python3 .github/scripts/send_kakao_brief.py --print-auth-url --client-id 여기에_32자리_REST_API_키_입력
```
출력된 URL을 브라우저로 열어 카카오 로그인 및 **카카오톡 메시지 전송** 체크 후 동의합니다.
이동된 브라우저 주소창(`https://localhost:5000/oauth?code=XXXXXX`, 페이지 연결 실패 화면이 떠도 정상)에서 `code=` 뒤 문자열을 복사한 뒤:

```bash
# 2) Refresh Token 발급
python3 .github/scripts/send_kakao_brief.py --client-id 여기에_32자리_REST_API_키_입력 --exchange-code 복사한_CODE_입력
```

### 방법 B: 스크립트 없이 curl 한 줄로 발급
1. 브라우저에서 아래 주소 접속 (**주의: `<` `>` 꺽쇠까지 모두 지우고** 32자리 REST API 키만 넣어야 합니다):
   ```text
   https://kauth.kakao.com/oauth/authorize?client_id=여기에32자리REST_API키만입력&redirect_uri=https://localhost:5000/oauth&response_type=code&scope=talk_message
   ```
   - 올바른 예: `...authorize?client_id=3f8a9c12b4d5e6f7a8b9c0d1e2f3a4b5&redirect_uri=...`
   - 잘못된 예(`KOE101` 발생): `...authorize?client_id=<3f8a9c12...>&redirect_uri=...` 또는 숫자 `앱 ID` 입력
2. 주소창의 `code=` 뒷부분만 복사 후 터미널에서 실행:
   ```bash
   curl -s -X POST "https://kauth.kakao.com/oauth/token" \
     -d "grant_type=authorization_code" \
     -d "client_id=여기에32자리REST_API키만입력" \
     -d "redirect_uri=https://localhost:5000/oauth" \
     -d "code=복사한CODE만입력"
   ```
   응답 JSON의 `"refresh_token"` 값을 복사합니다.
   *(만약 `KOE010 Bad client credentials` 오류가 뜨면 `[앱] > [플랫폼 키] > [REST API 키]`에서 클라이언트 시크릿이 켜져 있는 것이므로 `-d "client_secret=시크릿코드"`를 한 줄 추가하세요.)*

---

## 3단계: GitHub Secrets 등록

GitHub 저장소의 **`Settings` → `Secrets and variables` → `Actions` → `New repository secret`** 에서 등록합니다:

| Secret 이름 | 필수 여부 | 값 설명 |
| :--- | :---: | :--- |
| `KAKAO_REST_API_KEY` | **필수** | 1단계에서 복사한 카카오 앱 **REST API 키** |
| `KAKAO_REFRESH_TOKEN` | **필수** | 2단계에서 발급받은 **`refresh_token`** |
| `KAKAO_CLIENT_SECRET` | 선택 | 카카오 콘솔 `[카카오 로그인] > [보안]`에서 Client Secret을 **사용함**으로 켠 경우에만 등록 |
| `GH_SECRET_PAT` | 선택(권장) | 카카오 Refresh Token(유효기간 60일)을 만료 전 GitHub Secret에 **영구 자동 갱신**하기 위한 Fine-grained Personal Access Token (`yangkoon/brief` 저장소에 대해 `Secrets: Read and write` 권한만 부여) |

---

## 4단계: 즉시 테스트 및 동작 규칙

### 수동 테스트 발송
1. GitHub 저장소 상단 **`Actions`** 탭 → 좌측 **`KakaoTalk Brief Notify`** 선택
2. 우측 **`Run workflow`** 클릭 (`target_file`: `latest` 또는 `AI+_Brief_02.html`)
3. 내 카카오톡 **나와의 채팅방**에 카드 메시지가 도착하는지 확인!

### 자동 발송 규칙
- **신규 호 발행**: `main` 브랜치에 새 `*_Brief_*.html` 파일이 **신규 추가(`Add`)** 되면 배포 완료(HTTP 200)를 확인한 직후 자동으로 카카오톡 발송
- **오타 수정 시 중복 방지**: 이미 발행된 호를 단순 수정(`Modify`)할 때는 알림이 중복 발송되지 않음
- **수정본 재발송 강제**: 기존 호를 대폭 개정한 뒤 다시 카톡으로 받아보고 싶다면 커밋 메시지에 `[발송]` 또는 `[kakao]`를 포함해 푸시하면 재발송됨
