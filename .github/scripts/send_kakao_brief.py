#!/usr/bin/env python3
"""새 브리프(AEC Brief / AI+ Brief) 발행 시 카카오톡 '나에게 보내기' 자동 발송 스크립트.

외부 패키지 의존성 없이 Python 표준 라이브러리만 사용합니다.

주요 기능:
  1) 최초 1회 토큰 발급 도우미:
     python3 .github/scripts/send_kakao_brief.py --print-auth-url --client-id <REST_API_KEY>
     python3 .github/scripts/send_kakao_brief.py --exchange-code <AUTH_CODE> --client-id <REST_API_KEY>

  2) 브리프 HTML 파싱 및 나에게 카카오톡 발송:
     KAKAO_REST_API_KEY=... KAKAO_REFRESH_TOKEN=... \
       python3 .github/scripts/send_kakao_brief.py AEC_Brief_02.html

  3) 미리보기 테스트 (API 호출 없음):
     python3 .github/scripts/send_kakao_brief.py --dry-run AEC_Brief_02.html AI+_Brief_02.html
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

KAKAO_AUTH_URL = "https://kauth.kakao.com/oauth/authorize"
KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
KAKAO_SEND_ME_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"

DEFAULT_REDIRECT_URI = "https://localhost:5000/oauth"
HUB_URL = "https://brief.yangkoon.com"
GH_PAGES_BASE = "https://yangkoon.github.io/brief"

# 카카오톡 기본 텍스트 템플릿(object_type="text")의 최대 글자 수 제한
KAKAO_TEXT_MAX_CHARS = 200


@dataclass
class BriefMeta:
    filepath: Path
    brand: str
    edition_date: str
    cover_label: str
    cover_value: str
    lead_title: str
    page_url: str
    hub_url: str = HUB_URL


def _strip_tags(raw: str) -> str:
    cleaned = re.sub(r"<[^>]+>", "", raw)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _detect_brand(filename: str, title_text: str) -> str:
    if filename.startswith("AI+_Brief") or "AI+ Brief" in title_text:
        return "AI+ Brief"
    if filename.startswith("AEC_Brief") or "AEC Brief" in title_text:
        return "AEC Brief"
    return "Weekly Brief"


def resolve_brief_url(
    filepath: Path,
    base_url: str | None = None,
    wait_for_live: bool = False,
    timeout_sec: int = 75,
) -> str:
    """브리프 공개 URL을 결정합니다.

    1) base_url 지정 시 해당 도메인 사용
    2) brief.yangkoon.com/<파일명(확장자 제외)> 가 이미 HTTP 200이면 우선 사용
    3) 그렇지 않으면 GitHub Pages URL(yangkoon.github.io/brief/<파일명>) 사용
       (wait_for_live=True 인 경우 배포 완료까지 최대 timeout_sec 대기)
    """
    fname = filepath.name
    stem = filepath.stem
    encoded_fname = urllib.parse.quote(fname, safe="_.-")
    encoded_stem = urllib.parse.quote(stem, safe="_.-")

    if base_url:
        base = base_url.rstrip("/")
        if "brief.yangkoon.com" in base:
            return f"{base}/{encoded_stem}"
        return f"{base}/{encoded_fname}"

    hub_candidate = f"{HUB_URL}/{encoded_stem}"
    gh_candidate = f"{GH_PAGES_BASE}/{encoded_fname}"

    if not wait_for_live:
        if _url_is_live(hub_candidate):
            return hub_candidate
        return gh_candidate

    deadline = time.monotonic() + timeout_sec
    attempt = 0
    while True:
        attempt += 1
        if _url_is_live(hub_candidate):
            print(f"[url] brief.yangkoon.com 확인 완료: {hub_candidate}")
            return hub_candidate
        if _url_is_live(gh_candidate):
            print(f"[url] GitHub Pages 배포 확인 완료: {gh_candidate}")
            return gh_candidate

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(
                f"[url] 배포 대기 시간({timeout_sec}s) 초과 — 기본 GitHub Pages 링크를 사용합니다: {gh_candidate}"
            )
            return gh_candidate

        sleep_sec = min(10, max(3, int(remaining)))
        print(f"[url] 페이지 배포 대기 중... ({attempt}회차, {sleep_sec}s 후 재시도)")
        time.sleep(sleep_sec)


def _url_is_live(url: str) -> bool:
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "BriefKakaoNotifier/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


def parse_brief_html(
    filepath: Path,
    base_url: str | None = None,
    wait_for_live: bool = False,
) -> BriefMeta:
    content = filepath.read_text(encoding="utf-8")

    title_m = re.search(r"<title>(.*?)</title>", content, re.S | re.I)
    title_text = _strip_tags(title_m.group(1)) if title_m else filepath.stem

    brand = _detect_brand(filepath.name, title_text)

    ed_m = re.search(
        r'class="edition-bar".*?<strong>(.*?)</strong>', content, re.S | re.I
    )
    edition_date = _strip_tags(ed_m.group(1)) if ed_m else ""

    kicker_m = re.search(
        r'class="cover-kicker"[^>]*>(.*?)</div>', content, re.S | re.I
    )
    cover_label = ""
    if kicker_m:
        raw_kicker = _strip_tags(kicker_m.group(1))
        # "이번 주를 상징하는 숫자 · 빅테크 4사 2026년 AI 설비투자" -> 뒷부분 핵심 라벨 추출
        parts = [p.strip() for p in raw_kicker.split("·") if p.strip()]
        cover_label = parts[-1] if parts else raw_kicker

    cover_m = re.search(
        r'class="cover-big[^"]*"[^>]*>(.*?)</div>', content, re.S | re.I
    )
    cover_value = _strip_tags(cover_m.group(1)) if cover_m else ""

    lead_m = re.search(
        r'class="lead-title"[^>]*>(.*?)</h[1-6]>', content, re.S | re.I
    )
    if lead_m:
        lead_title = _strip_tags(lead_m.group(1))
    else:
        desc_m = re.search(
            r'<meta\s+name="description"\s+content="(.*?)"', content, re.S | re.I
        )
        lead_title = _strip_tags(desc_m.group(1)) if desc_m else title_text

    page_url = resolve_brief_url(
        filepath, base_url=base_url, wait_for_live=wait_for_live
    )

    return BriefMeta(
        filepath=filepath,
        brand=brand,
        edition_date=edition_date,
        cover_label=cover_label,
        cover_value=cover_value,
        lead_title=lead_title,
        page_url=page_url,
    )


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return text[:max_len]
    return text[: max_len - 1].rstrip() + "…"


def build_kakao_template(
    meta: BriefMeta, token_notice: str | None = None
) -> dict:
    """카카오톡 기본 텍스트 템플릿(최대 200자 + 버튼 2개)을 생성합니다."""
    edition_part = meta.edition_date.split("·")[0].strip() if meta.edition_date else ""
    date_part = (
        meta.edition_date.split("·", 1)[1].strip()
        if "·" in meta.edition_date
        else ""
    )

    header = f"📰 [{meta.brand} {edition_part} 발행]".replace("  ", " ").strip()
    lines = [header]
    if date_part:
        lines.append(f"📅 {date_part}")
    if meta.cover_value:
        if meta.cover_label:
            lines.append(f"📊 {meta.cover_label}: {meta.cover_value}")
        else:
            lines.append(f"📊 주요 지표: {meta.cover_value}")

    prefix = "\n".join(lines) + "\n\n■ 1면 톱\n"
    suffix = f"\n\n{token_notice}" if token_notice else ""

    available_for_lead = KAKAO_TEXT_MAX_CHARS - len(prefix) - len(suffix)
    lead_text = _truncate(meta.lead_title, max(20, available_for_lead))
    full_text = _truncate(prefix + lead_text + suffix, KAKAO_TEXT_MAX_CHARS)

    return {
        "object_type": "text",
        "text": full_text,
        "link": {
            "web_url": meta.page_url,
            "mobile_web_url": meta.page_url,
        },
        "buttons": [
            {
                "title": "이번 호 전문 읽기",
                "link": {
                    "web_url": meta.page_url,
                    "mobile_web_url": meta.page_url,
                },
            },
            {
                "title": "전체 아카이브",
                "link": {
                    "web_url": meta.hub_url,
                    "mobile_web_url": meta.hub_url,
                },
            },
        ],
    }


def _post_form(url: str, data: dict[str, str], headers: dict[str, str] | None = None) -> dict:
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    req_headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
    }
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=encoded, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Kakao API HTTP {e.code} 오류: {err_body}") from e


def exchange_auth_code(
    client_id: str,
    auth_code: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    client_secret: str | None = None,
) -> dict:
    payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code": auth_code,
    }
    if client_secret:
        payload["client_secret"] = client_secret
    return _post_form(KAKAO_TOKEN_URL, payload)


def refresh_access_token(
    client_id: str,
    refresh_token: str,
    client_secret: str | None = None,
) -> dict:
    payload = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }
    if client_secret:
        payload["client_secret"] = client_secret
    return _post_form(KAKAO_TOKEN_URL, payload)


def send_kakao_memo(access_token: str, template_obj: dict) -> dict:
    payload = {
        "template_object": json.dumps(template_obj, ensure_ascii=False),
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    return _post_form(KAKAO_SEND_ME_URL, payload, headers=headers)


def _write_github_output(key: str, value: str) -> None:
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if not gh_out:
        return
    with open(gh_out, "a", encoding="utf-8") as f:
        f.write(f"{key}={value}\n")


def find_latest_briefs(repo_root: Path) -> list[Path]:
    """가장 최신 호수의 AEC Brief 및 AI+ Brief 파일을 반환합니다."""
    results: list[Path] = []
    for pattern in ("AEC_Brief_*.html", "AI+_Brief_*.html"):
        matches = sorted(repo_root.glob(pattern))
        if matches:
            results.append(matches[-1])
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="새 브리프 발행 시 카카오톡 '나에게 보내기' 자동 발송 스크립트"
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="발송할 브리프 HTML 파일 경로 (또는 'latest' 지정 시 최신 호 자동 선택)",
    )
    parser.add_argument(
        "--client-id",
        default=os.environ.get("KAKAO_REST_API_KEY", "").strip(),
        help="카카오 앱 REST API 키 (기본값: 환경변수 KAKAO_REST_API_KEY)",
    )
    parser.add_argument(
        "--client-secret",
        default=os.environ.get("KAKAO_CLIENT_SECRET", "").strip(),
        help="카카오 Client Secret (설정한 경우에만 사용, 환경변수 KAKAO_CLIENT_SECRET)",
    )
    parser.add_argument(
        "--refresh-token",
        default=os.environ.get("KAKAO_REFRESH_TOKEN", "").strip(),
        help="카카오 OAuth Refresh Token (기본값: 환경변수 KAKAO_REFRESH_TOKEN)",
    )
    parser.add_argument(
        "--redirect-uri",
        default=DEFAULT_REDIRECT_URI,
        help=f"카카오 로그인 Redirect URI (기본값: {DEFAULT_REDIRECT_URI})",
    )
    parser.add_argument(
        "--print-auth-url",
        action="store_true",
        help="최초 1회 인가 코드 발급용 브라우저 접속 URL 출력",
    )
    parser.add_argument(
        "--exchange-code",
        metavar="AUTH_CODE",
        help="인가 코드(code)를 입력받아 최초 Refresh Token 발급",
    )
    parser.add_argument(
        "--refresh-only",
        action="store_true",
        help="메시지 발송 없이 Refresh Token 갱신 여부만 점검",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("BRIEF_BASE_URL", "").strip() or None,
        help="브리프 링크 기본 URL 강제 지정 (예: https://brief.yangkoon.com)",
    )
    parser.add_argument(
        "--wait-for-live",
        action="store_true",
        help="GitHub Pages / 웹 배포가 HTTP 200으로 열릴 때까지 대기 후 발송",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="카카오 API를 호출하지 않고 파싱 결과 및 메시지 전문만 출력",
    )

    args = parser.parse_args()

    # 1. 인가 URL 안내 모드
    if args.print_auth_url:
        if not args.client_id:
            print("오류: --client-id <REST_API_KEY> 또는 KAKAO_REST_API_KEY 환경변수가 필요합니다.", file=sys.stderr)
            return 1
        params = urllib.parse.urlencode(
            {
                "client_id": args.client_id,
                "redirect_uri": args.redirect_uri,
                "response_type": "code",
                "scope": "talk_message",
            }
        )
        print("\n아래 URL을 브라우저에 붙여넣어 카카오 로그인 및 '카카오톡 메시지 전송' 동의를 진행하세요:\n")
        print(f"{KAKAO_AUTH_URL}?{params}\n")
        print("로그인 후 리다이렉트된 주소창(연결 실패 화면이 떠도 정상)에서")
        print("?code= 뒤의 문자열만 복사해 아래 명령으로 토큰을 발급받으세요:\n")
        print(
            f"python3 .github/scripts/send_kakao_brief.py --client-id {args.client_id} --exchange-code <복사한_CODE>\n"
        )
        return 0

    # 2. 인가 코드 -> Refresh Token 교환 모드
    if args.exchange_code:
        if not args.client_id:
            print("오류: --client-id <REST_API_KEY> 가 필요합니다.", file=sys.stderr)
            return 1
        token_data = exchange_auth_code(
            client_id=args.client_id,
            auth_code=args.exchange_code.strip(),
            redirect_uri=args.redirect_uri,
            client_secret=args.client_secret or None,
        )
        refresh_tok = token_data.get("refresh_token", "")
        expires_days = int(token_data.get("refresh_token_expires_in", 0)) // 86400
        print("\n✅ 카카오 토큰 발급 완료!")
        print("GitHub 저장소 Settings -> Secrets and variables -> Actions 에 아래 두 시크릿을 등록하세요:\n")
        print(f"  KAKAO_REST_API_KEY  = {args.client_id}")
        print(f"  KAKAO_REFRESH_TOKEN = {refresh_tok}")
        if args.client_secret:
            print(f"  KAKAO_CLIENT_SECRET = {args.client_secret}")
        print(f"\n(Refresh Token 유효기간: 약 {expires_days}일 / 만료 30일 전부터 자동 갱신 응답 제공)\n")
        return 0

    repo_root = Path(__file__).resolve().parents[2]

    # 대상 파일 확인
    target_paths: list[Path] = []
    if not args.refresh_only:
        raw_files = args.files
        if not raw_files or raw_files == ["latest"]:
            target_paths = find_latest_briefs(repo_root)
        else:
            for item in raw_files:
                if item == "latest":
                    target_paths.extend(find_latest_briefs(repo_root))
                else:
                    matched = sorted(glob.glob(item))
                    if matched:
                        target_paths.extend(Path(p) for p in matched)
                    else:
                        p = Path(item)
                        if p.exists():
                            target_paths.append(p)
                        else:
                            print(f"[경고] 파일을 찾을 수 없어 건너뜁니다: {item}", file=sys.stderr)

        if not target_paths:
            print("발송할 브리프 HTML 파일이 없습니다.")
            return 0

    # 3. Dry-run 미리보기 모드
    if args.dry_run:
        for path in target_paths:
            meta = parse_brief_html(path, base_url=args.base_url, wait_for_live=False)
            tpl = build_kakao_template(meta)
            print("=" * 64)
            print(f"파일명 : {path.name}")
            print(f"링크   : {meta.page_url}")
            print(f"글자수 : {len(tpl['text'])} / {KAKAO_TEXT_MAX_CHARS}자")
            print("-" * 64)
            print(tpl["text"])
            print("-" * 64)
            print("버튼   :", [b["title"] + " -> " + b["link"]["web_url"] for b in tpl["buttons"]])
        print("=" * 64)
        return 0

    # 4. 실제 API 호출: Access Token 재발급
    if not args.client_id or not args.refresh_token:
        print(
            "오류: KAKAO_REST_API_KEY 및 KAKAO_REFRESH_TOKEN 시크릿(또는 인자)이 설정되지 않았습니다.\n"
            "설정 방법은 .github/KAKAO_SETUP.md 를 참고하세요.",
            file=sys.stderr,
        )
        return 1

    token_resp = refresh_access_token(
        client_id=args.client_id,
        refresh_token=args.refresh_token,
        client_secret=args.client_secret or None,
    )
    access_token = token_resp["access_token"]
    new_refresh_token = token_resp.get("refresh_token")
    refresh_expires_in = token_resp.get("refresh_token_expires_in")

    token_notice: str | None = None
    if new_refresh_token:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::add-mask::{new_refresh_token}")
        print("[token] 카카오에서 갱신된 Refresh Token이 반환되었습니다 (만료 30일 이내 진입).")
        _write_github_output("new_refresh_token", new_refresh_token)
        _write_github_output("token_rotated", "true")
        if not os.environ.get("GH_SECRET_PAT"):
            days_left = (int(refresh_expires_in) // 86400) if refresh_expires_in else 29
            if days_left <= 14:
                token_notice = f"⚠️ 카톡 토큰 만료 {days_left}일 전 (시크릿 갱신 권장)"
    else:
        _write_github_output("token_rotated", "false")

    if args.refresh_only:
        print("[token] Refresh Token 점검 완료.")
        return 0

    # 5. 브리프별 메시지 전송
    for idx, path in enumerate(target_paths):
        meta = parse_brief_html(
            path,
            base_url=args.base_url,
            wait_for_live=args.wait_for_live,
        )
        tpl = build_kakao_template(meta, token_notice=token_notice if idx == 0 else None)
        res = send_kakao_memo(access_token, tpl)
        result_code = res.get("result_code")
        if result_code == 0:
            print(f"✅ 카카오톡 발송 성공: {path.name} -> {meta.page_url}")
        else:
            print(f"⚠️ 카카오톡 응답 확인 필요 ({path.name}): {res}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
