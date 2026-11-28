#!/usr/bin/env python3
"""텔레그램 브리프 배포 시 Make.com Custom Webhook으로 브리프 메타데이터를 전송하는 스크립트.

Make.com 시나리오(Custom Webhook -> Facebook Pages / LinkedIn)에서 바로 매핑해 사용할 수 있도록
제목, 요약, 링크, 1면 썸네일 URL 및 SNS별 완성 본문(facebook_message, linkedin_message)을 JSON으로 전송합니다.

환경변수:
  - MAKE_WEBHOOK_URL      : 공용 Make.com Webhook URL (에디션 구분 없이 1개 시나리오 사용 시)
  - MAKE_AEC_WEBHOOK_URL  : AEC Brief 전용 Webhook URL (설정 시 MAKE_WEBHOOK_URL보다 우선)
  - MAKE_AI_WEBHOOK_URL   : AI+ Brief 전용 Webhook URL (설정 시 MAKE_WEBHOOK_URL보다 우선)

실행 예시:
  1) 페이로드 미리보기 (실제 전송 없음):
     python3 .github/scripts/send_make_webhook.py --dry-run AEC_Brief_03.html

  2) 실제 Webhook 전송:
     MAKE_WEBHOOK_URL="https://hook.eu2.make.com/..." \
       python3 .github/scripts/send_make_webhook.py --wait-for-live AEC_Brief_03.html
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import send_telegram_brief as tg  # noqa: E402

RAW_GH_BASE = "https://raw.githubusercontent.com/yangkoon/brief/main"

DEFAULT_HASHTAGS = {
    "aec": "#AECBrief #스마트건설 #BIM #건축 #엔지니어링 #건설트렌드",
    "ai": "#AIPlusBrief #AI트렌드 #인공지능 #테크브리프 #AX",
    "other": "#WeeklyBrief #뉴스레터",
}


def resolve_webhook_url(edition_key: str, cli_url: str = "") -> str:
    if cli_url.strip():
        return cli_url.strip()
    env = os.environ
    if edition_key == "aec":
        return (
            env.get("MAKE_AEC_WEBHOOK_URL", "").strip()
            or env.get("MAKE_WEBHOOK_URL", "").strip()
        )
    if edition_key == "ai":
        return (
            env.get("MAKE_AI_WEBHOOK_URL", "").strip()
            or env.get("MAKE_WEBHOOK_URL", "").strip()
        )
    return env.get("MAKE_WEBHOOK_URL", "").strip()


def resolve_thumb_url(filepath: Path, repo_root: Path) -> str:
    thumb_name = tg.thumb_filename(filepath)
    local_thumb = repo_root / tg.THUMBS_DIR_NAME / thumb_name
    if local_thumb.exists() and local_thumb.stat().st_size > 0:
        encoded = urllib.parse.quote(thumb_name, safe="_.-")
        return f"{RAW_GH_BASE}/{tg.THUMBS_DIR_NAME}/{encoded}"
    return ""


def _format_header(meta: tg.BriefMeta) -> str:
    edition_part = meta.edition_date.split("·")[0].strip() if meta.edition_date else ""
    date_part = (
        meta.edition_date.split("·", 1)[1].strip()
        if "·" in meta.edition_date
        else ""
    )
    if edition_part and date_part:
        return f"{meta.brand} · {edition_part} ({date_part})"
    if meta.edition_date:
        return f"{meta.brand} ({meta.edition_date})"
    return f"{meta.brand}"


def build_sns_messages(meta: tg.BriefMeta) -> tuple[str, str, str]:
    header = _format_header(meta)
    hashtags = DEFAULT_HASHTAGS.get(meta.edition_key, DEFAULT_HASHTAGS["other"])

    lines: list[str] = [header, meta.lead_title]

    if meta.cover_value:
        if meta.cover_label:
            lines.append(f"■ 이번 호 숫자 ({meta.cover_label}): {meta.cover_value}")
        else:
            lines.append(f"■ 이번 호 숫자: {meta.cover_value}")

    if meta.three_lines:
        summary_block = ["▶ 핵심 3줄 요약"]
        for idx, item in enumerate(meta.three_lines[:3], start=1):
            summary_block.append(f"{idx}. {item}")
        lines.append("\n".join(summary_block))

    lines.append(f"전문 읽기: {meta.page_url}\n아카이브: {meta.hub_url}")
    lines.append(hashtags)

    fb_msg = "\n\n".join(lines)
    li_msg = "\n\n".join(lines)
    return fb_msg, li_msg, hashtags


def build_webhook_payload(
    meta: tg.BriefMeta,
    repo_root: Path,
    thumb_url: str,
) -> dict:
    fb_msg, li_msg, hashtags = build_sns_messages(meta)
    three_lines_text = "\n".join(
        f"{idx}. {item}" for idx, item in enumerate(meta.three_lines[:3], start=1)
    )
    return {
        "event": "brief_published",
        "filename": meta.filepath.name,
        "edition_key": meta.edition_key,
        "brand": meta.brand,
        "edition_date": meta.edition_date,
        "cover_label": meta.cover_label,
        "cover_value": meta.cover_value,
        "lead_title": meta.lead_title,
        "three_lines": meta.three_lines[:3],
        "three_lines_text": three_lines_text,
        "page_url": meta.page_url,
        "hub_url": meta.hub_url,
        "thumb_url": thumb_url,
        "hashtags": hashtags,
        "facebook_message": fb_msg,
        "linkedin_message": li_msg,
        "published_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def post_webhook(webhook_url: str, payload: dict) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "BriefMakeWebhook/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Make.com Webhook HTTP {e.code} 오류: {err_body}") from e


def main() -> int:
    parser = argparse.ArgumentParser(
        description="브리프 발행 정보를 Make.com Custom Webhook으로 전송"
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="전송할 브리프 HTML 경로 ('latest-aec', 'latest-ai', 'latest' 지원)",
    )
    parser.add_argument(
        "--webhook-url",
        default="",
        help="Make.com Webhook URL 직접 지정 (미지정 시 환경변수 사용)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("BRIEF_BASE_URL", "").strip() or None,
        help="브리프 링크 기본 URL 강제 지정 (예: https://brief.yangkoon.com)",
    )
    parser.add_argument(
        "--wait-for-live",
        action="store_true",
        help="GitHub Pages / brief.yangkoon.com 배포가 HTTP 200으로 열릴 때까지 대기",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Webhook을 호출하지 않고 전송 예정 JSON 페이로드만 출력",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    target_paths = tg.resolve_input_files(args.files, repo_root)
    if not target_paths:
        print("전송할 브리프 HTML 파일이 없습니다.")
        return 0

    sent_count = 0
    for path in target_paths:
        meta = tg.parse_brief_html(
            path,
            repo_root=repo_root,
            base_url=args.base_url,
            wait_for_live=args.wait_for_live and not args.dry_run,
            dry_run=args.dry_run,
            no_photo=True,
        )
        thumb_url = resolve_thumb_url(path, repo_root)
        payload = build_webhook_payload(meta, repo_root=repo_root, thumb_url=thumb_url)

        if args.dry_run:
            print("=" * 68)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            continue

        webhook_url = resolve_webhook_url(meta.edition_key, cli_url=args.webhook_url)
        if not webhook_url:
            print(
                f"[skip] {path.name}: MAKE_WEBHOOK_URL(또는 MAKE_{meta.edition_key.upper()}_WEBHOOK_URL) 시크릿이 미설정 상태여서 Make.com 전송을 건너뜁니다."
            )
            continue

        status, resp_body = post_webhook(webhook_url, payload)
        print(
            f"✅ Make.com Webhook 전송 완료: {path.name} (HTTP {status}, 응답: {resp_body.strip() or 'ok'})"
        )
        sent_count += 1

    if args.dry_run:
        print("=" * 68)
    elif sent_count == 0:
        print(
            "💡 Make.com 연동을 활성화하려면 GitHub Secrets에 MAKE_WEBHOOK_URL을 등록하세요 (.github/MAKE_SNS_SETUP.md 참고)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
