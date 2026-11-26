#!/usr/bin/env python3
"""새 브리프(AEC Brief / AI+ Brief) 발행 시 텔레그램 채널 자동 배포 스크립트.

외부 패키지 의존성 없이 Python 표준 라이브러리만 사용합니다.

채널 분리 규칙:
  - AEC Brief (AEC_Brief_*.html):
      토큰: AEC_TELEGRAM_BOT_TOKEN -> 없으면 TELEGRAM_BOT_TOKEN 사용 (@aec_brief_bot)
      채널: AEC_TELEGRAM_CHAT_ID   -> 없으면 TELEGRAM_CHAT_ID 사용
  - AI+ Brief (AI+_Brief_*.html):
      토큰: AI_TELEGRAM_BOT_TOKEN  -> 없으면 TELEGRAM_BOT_TOKEN 사용
      채널: AI_TELEGRAM_CHAT_ID    (미설정 시 AEC 채널로 섞이지 않도록 안전하게 건너뜀)

주요 실행 예시:
  1) 미리보기 테스트 (API 호출 없음):
     python3 .github/scripts/send_telegram_brief.py --dry-run AEC_Brief_02.html

  2) 봇에 연결된 채널/채팅방 ID 조회 도우미:
     TELEGRAM_BOT_TOKEN=... python3 .github/scripts/send_telegram_brief.py --discover-chats

  3) 실제 발송:
     TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=@채널아이디 \
       python3 .github/scripts/send_telegram_brief.py --wait-for-live AEC_Brief_02.html
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

HUB_URL = "https://brief.yangkoon.com"
GH_PAGES_BASE = "https://yangkoon.github.io/brief"
TELEGRAM_API_BASE = "https://api.telegram.org"

THUMBS_DIR_NAME = "thumbs"
THUMB_VIEWPORT_W = 1080
THUMB_VIEWPORT_H = 1080

# 텔레그램 API 글자 수 제한 (UTF-16 코드 유닛 기준)
TELEGRAM_CAPTION_MAX = 1024
TELEGRAM_MESSAGE_MAX = 4096


@dataclass
class BriefMeta:
    filepath: Path
    edition_key: str  # "aec" | "ai" | "other"
    brand: str
    edition_date: str
    cover_label: str
    cover_value: str
    lead_title: str
    three_lines: list[str] = field(default_factory=list)
    page_url: str = ""
    thumb_path: Path | None = None
    hub_url: str = HUB_URL


@dataclass
class ChannelTarget:
    edition_key: str
    bot_token: str
    chat_id: str
    thread_id: str = ""


def _strip_tags(raw: str) -> str:
    cleaned = re.sub(r"<[^>]+>", "", raw)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _escape_tg_html(text: str) -> str:
    return html.escape(text, quote=False)


def _utf16_len(plain_text: str) -> int:
    return len(plain_text.encode("utf-16-le")) // 2


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return text[:max_len]
    return text[: max_len - 1].rstrip() + "…"


def _detect_edition(filename: str, title_text: str) -> tuple[str, str]:
    if filename.startswith("AI+_Brief") or "AI+ Brief" in title_text:
        return "ai", "AI+ Brief"
    if filename.startswith("AEC_Brief") or "AEC Brief" in title_text:
        return "aec", "AEC Brief"
    return "other", "Weekly Brief"


def thumb_filename(filepath: Path) -> str:
    safe_stem = filepath.stem.replace("+", "Plus")
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]", "_", safe_stem)
    return f"{safe_stem}.png"


def _find_chrome_binary() -> str | None:
    for candidate in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def ensure_thumbnail(
    brief_path: Path,
    repo_root: Path,
    force: bool = False,
) -> Path | None:
    """기존 thumbs/*.png가 있으면 재사용하고, 없으면 임시 파일로 1면 화면을 캡처합니다."""
    existing = repo_root / THUMBS_DIR_NAME / thumb_filename(brief_path)
    if existing.exists() and existing.stat().st_size > 10_000 and not force:
        return existing

    chrome = _find_chrome_binary()
    if not chrome:
        return existing if existing.exists() else None

    tmp_dir = Path(tempfile.gettempdir()) / "brief_tg_thumbs"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out_path = tmp_dir / thumb_filename(brief_path)

    file_uri = brief_path.resolve().as_uri()
    cmd = [
        chrome,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--hide-scrollbars",
        f"--window-size={THUMB_VIEWPORT_W},{THUMB_VIEWPORT_H}",
        "--virtual-time-budget=5000",
        f"--screenshot={out_path}",
        file_uri,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out_path.exists():
        print(f"[thumb] 캡처 실패 ({brief_path.name}): {proc.stderr[-400:]}", file=sys.stderr)
        return existing if existing.exists() else None

    size_kb = out_path.stat().st_size // 1024
    print(f"[thumb] 1면 캡처 준비 완료: {out_path.name} ({size_kb}KB)")
    return out_path


def _url_is_live(url: str) -> bool:
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "BriefTelegramNotifier/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


def resolve_brief_url(
    filepath: Path,
    base_url: str | None = None,
    wait_for_live: bool = False,
    timeout_sec: int = 90,
) -> str:
    """브리프 공개 URL을 결정하고 필요 시 배포 완료(HTTP 200)까지 대기합니다."""
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


def parse_brief_html(
    filepath: Path,
    repo_root: Path,
    base_url: str | None = None,
    wait_for_live: bool = False,
    dry_run: bool = False,
    no_photo: bool = False,
    force_thumbs: bool = False,
) -> BriefMeta:
    content = filepath.read_text(encoding="utf-8")

    title_m = re.search(r"<title>(.*?)</title>", content, re.S | re.I)
    title_text = _strip_tags(title_m.group(1)) if title_m else filepath.stem

    edition_key, brand = _detect_edition(filepath.name, title_text)

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

    three_lines: list[str] = []
    tl_m = re.search(r'<ol[^>]*id="threeLines"[^>]*>(.*?)</ol>', content, re.S | re.I)
    if tl_m:
        for li_m in re.finditer(r"<li([^>]*)>(.*?)</li>", tl_m.group(1), re.S | re.I):
            li_attrs, li_raw = li_m.groups()
            tg_override = re.search(r'data-tg="([^"]+)"', li_attrs)
            if tg_override:
                cleaned_li = html.unescape(tg_override.group(1)).strip()
            else:
                without_idx = re.sub(
                    r'<span[^>]*class="[^"]*\bidx\b[^"]*"[^>]*>.*?</span>',
                    "",
                    li_raw,
                    flags=re.S | re.I,
                )
                cleaned_li = condense_summary_line(_strip_tags(without_idx))
            if cleaned_li:
                three_lines.append(cleaned_li)

    if dry_run and not base_url:
        encoded_fname = urllib.parse.quote(filepath.name, safe="_.-")
        page_url = f"{GH_PAGES_BASE}/{encoded_fname}"
    else:
        page_url = resolve_brief_url(
            filepath, base_url=base_url, wait_for_live=wait_for_live
        )

    thumb_path: Path | None = None
    if not no_photo:
        if dry_run:
            candidate = repo_root / THUMBS_DIR_NAME / thumb_filename(filepath)
            thumb_path = candidate if candidate.exists() else None
        else:
            thumb_path = ensure_thumbnail(filepath, repo_root=repo_root, force=force_thumbs)

    return BriefMeta(
        filepath=filepath,
        edition_key=edition_key,
        brand=brand,
        edition_date=edition_date,
        cover_label=cover_label,
        cover_value=cover_value,
        lead_title=lead_title,
        three_lines=three_lines,
        page_url=page_url,
        thumb_path=thumb_path,
    )


def condense_summary_line(raw: str, max_chars: int = 68) -> str:
    """장문 복문이나 잡다한 나열형 문장이 들어오더라도 단일 주제의 간결한 한 줄로 정제합니다."""
    s = raw.strip()
    if not s:
        return ""

    # 이미 간결하고 나열이 적은 문장은 원문 그대로 유지
    if len(s) <= max_chars and s.count("·") <= 2:
        return s

    # 1) 7자 이상의 부연 설명 괄호 제거
    s = re.sub(r"\([^)]{7,}\)", "", s)

    # 2) 'A과/와 함께 B·C·D가 이어졌습니다/잇따랐습니다' 형태의 무관한 나열 꼬리 제거
    m_grabbag = re.match(
        r"^(.+?)(?:과|와)\s+함께\s+.+(?:이어졌|잇따랐|진행됐).*$", s
    )
    if m_grabbag:
        head = m_grabbag.group(1).strip(" ,·")
        parts = [p.strip() for p in head.split("·") if p.strip()]
        s = (f"{parts[0]}·{parts[1]}" if len(parts) >= 2 else head) + " 성과가 주목받았습니다."

    # 3) 여전히 길면 복문 접속구 기준으로 핵심 주절만 남김
    if len(s) > max_chars:
        m_clause = re.match(
            r"^(.+?)(?:한 가운데,|이 겹친 가운데,|가 겹친 가운데,|커지는 사이,\s*(?:국내에서는\s*)?|겹치며,)\s*(.+)$",
            s,
        )
        if m_clause:
            c1, c2 = m_clause.group(1).strip(), m_clause.group(2).strip()
            s = c2 if len(c2) <= max_chars + 10 else c1

    if len(s) > max_chars:
        m_and = re.match(
            r"^(.+?(?:출범했|진단했|발표했|세웠|열었|돌파했|낮췄|완화했|나섰))(?:고|으며),\s+.+$",
            s,
        )
        if m_and:
            s = m_and.group(1) + "습니다."

    # 4) 가운뎃점(·) 3개 이상 과다 나열 시 앞 2개 항목으로 제한
    if s.count("·") >= 3:
        parts = [p.strip() for p in s.split("·") if p.strip()]
        tail_verb = parts[-1].split(" ", 1)[1] if " " in parts[-1] else ""
        s = f"{parts[0]}·{parts[1]}" + (f" 등 {tail_verb}" if tail_verb else "")

    return re.sub(r"\s+", " ", s).strip()


def build_telegram_message(meta: BriefMeta, max_visible_len: int = TELEGRAM_CAPTION_MAX) -> tuple[str, dict]:
    """이모지 없는 담백한 뉴스레터 요약 메시지(HTML)와 인라인 버튼 마크업을 생성합니다."""
    edition_part = meta.edition_date.split("·")[0].strip() if meta.edition_date else ""
    date_part = (
        meta.edition_date.split("·", 1)[1].strip()
        if "·" in meta.edition_date
        else ""
    )

    if edition_part and date_part:
        header_html = f"<b>{_escape_tg_html(meta.brand)} · {_escape_tg_html(edition_part)}</b> · {_escape_tg_html(date_part)}"
    elif meta.edition_date:
        header_html = f"<b>{_escape_tg_html(meta.brand)}</b> · {_escape_tg_html(meta.edition_date)}"
    else:
        header_html = f"<b>{_escape_tg_html(meta.brand)}</b>"

    def _assemble(line_limit: int | None) -> str:
        blocks: list[str] = [
            header_html,
            f"<b>{_escape_tg_html(meta.lead_title)}</b>",
        ]
        if meta.cover_value:
            if meta.cover_label:
                blocks.append(
                    f"이번 호 숫자 · {_escape_tg_html(meta.cover_label)}: <b>{_escape_tg_html(meta.cover_value)}</b>"
                )
            else:
                blocks.append(f"이번 호 숫자: <b>{_escape_tg_html(meta.cover_value)}</b>")

        if meta.three_lines and (line_limit is None or line_limit > 0):
            summary_rows = ["<b>핵심 3줄 요약</b>"]
            for idx, item in enumerate(meta.three_lines[:3], start=1):
                clipped = _truncate(item, line_limit) if line_limit else item
                summary_rows.append(f"{idx}. {_escape_tg_html(clipped)}")
            blocks.append("\n".join(summary_rows))

        return "\n\n".join(blocks)

    html_text = _assemble(None)
    plain_check = html.unescape(re.sub(r"<[^>]+>", "", html_text))

    if _utf16_len(plain_check) > max_visible_len:
        for limit in (90, 70, 55, 0):
            html_text = _assemble(limit)
            plain_check = html.unescape(re.sub(r"<[^>]+>", "", html_text))
            if _utf16_len(plain_check) <= max_visible_len:
                break

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "전문 읽기", "url": meta.page_url},
                {"text": "아카이브", "url": meta.hub_url},
            ]
        ]
    }
    return html_text, reply_markup


def normalize_bot_token(raw_token: str) -> str:
    """@BotFather 안내 문구나 공백·따옴표가 함께 복사된 경우에도 순수 토큰만 안전하게 추출합니다."""
    val = raw_token.strip().strip("\"'`")
    if not val:
        return ""

    # 1) 문장/URL 안에 '1234567890:AAH...' 형태의 토큰이 포함된 경우 추출
    m = re.search(r"(?:bot)?(\d{6,15}:[A-Za-z0-9_-]{20,})", val)
    if not m:
        # 2) 콜론 앞뒤 등에 실수로 공백이 섞인 경우 공백 제거 후 재시도
        compact = re.sub(r"\s+", "", val)
        m = re.search(r"(?:bot)?(\d{6,15}:[A-Za-z0-9_-]{20,})", compact)

    if not m:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN 시크릿 값이 올바른 봇 토큰 형식이 아닙니다 (공백/문구가 섞였거나 '숫자ID:영문토큰' 형태가 아님).\n"
            "해결 방법: 텔레그램 @BotFather 채팅창에서 'Use this token to access the HTTP API:' 바로 아래 줄의\n"
            "'1234567890:AAH...' 형태 문자열만 복사해 GitHub Secrets -> TELEGRAM_BOT_TOKEN 에 다시 저장해 주세요."
        )

    cleaned = m.group(1)
    if os.environ.get("GITHUB_ACTIONS") == "true" and cleaned != raw_token:
        # 원본 시크릿에서 부분 문자열을 추출한 경우에도 GitHub Actions 로그에서 마스킹 보장
        print(f"::add-mask::{cleaned}", flush=True)
    return cleaned


def normalize_chat_id(raw_chat_id: str) -> str:
    val = raw_chat_id.strip().strip("\"'`")
    if not val:
        return ""
    # 1) https://t.me/channel_name 형태가 포함된 경우 @channel_name으로 자동 보정 (+초대링크 제외)
    m_url = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})/?", val, re.I)
    if m_url:
        return f"@{m_url.group(1)}"
    # 2) '-1001234567890' 등 숫자형 채널/그룹 ID 추출 (앞뒤 설명 문구 제거)
    m_num = re.search(r"(-100\d{6,}|-?\d{6,})", val)
    if m_num and (val.startswith("-") or val.isdigit() or "id" in val.lower()):
        return m_num.group(1)
    # 3) '@channel_name' 추출 (앞뒤 공백/설명 문구 제거)
    m_at = re.search(r"(@[A-Za-z0-9_]{4,})", val)
    if m_at:
        return m_at.group(1)
    return val


def resolve_channel_target(edition_key: str, cli_token: str = "", cli_chat_id: str = "") -> ChannelTarget | None:
    """에디션(aec / ai)에 맞는 봇 토큰과 채널 ID를 반환합니다."""
    env = os.environ
    raw_default_tok = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    default_chat = env.get("TELEGRAM_CHAT_ID", "").strip()

    if cli_token or cli_chat_id:
        tok_src = cli_token.strip() or raw_default_tok
        chat_src = cli_chat_id.strip() or default_chat
        if not tok_src or not chat_src:
            return None
        return ChannelTarget(
            edition_key=edition_key,
            bot_token=normalize_bot_token(tok_src),
            chat_id=normalize_chat_id(chat_src),
        )

    if edition_key == "aec":
        tok_src = env.get("AEC_TELEGRAM_BOT_TOKEN", "").strip() or raw_default_tok
        chat_id = normalize_chat_id(
            env.get("AEC_TELEGRAM_CHAT_ID", "").strip() or default_chat
        )
        thread_id = env.get("AEC_TELEGRAM_THREAD_ID", "").strip()
        if not tok_src or not chat_id:
            return None
        return ChannelTarget("aec", normalize_bot_token(tok_src), chat_id, thread_id)

    if edition_key == "ai":
        tok_src = env.get("AI_TELEGRAM_BOT_TOKEN", "").strip() or raw_default_tok
        # AI+ Brief는 전용 AI_TELEGRAM_CHAT_ID가 설정된 경우에만 발송 (AEC 채널 오발송 방지)
        chat_id = normalize_chat_id(env.get("AI_TELEGRAM_CHAT_ID", "").strip())
        thread_id = env.get("AI_TELEGRAM_THREAD_ID", "").strip()
        if not tok_src or not chat_id:
            return None
        return ChannelTarget("ai", normalize_bot_token(tok_src), chat_id, thread_id)

    if raw_default_tok and default_chat:
        return ChannelTarget(
            edition_key,
            normalize_bot_token(raw_default_tok),
            normalize_chat_id(default_chat),
        )
    return None


def _tg_explain_error(code: int, err_body: str, chat_id: str) -> str:
    desc = err_body
    try:
        parsed = json.loads(err_body)
        desc = parsed.get("description", err_body)
    except Exception:
        pass

    hints: list[str] = []
    lower = desc.lower()
    if "bots can't send messages to bots" in lower or chat_id.lower().endswith("bot"):
        hints.append(
            f"CHAT_ID('{chat_id}')에 봇 아이디가 들어가 있습니다. 봇은 자기 자신에게 메시지를 보낼 수 없으므로, "
            "구독용 텔레그램 '채널'(예: @aec_brief 또는 -100xxxxxxxxxx)을 만들고 @aec_brief_bot 을 관리자로 초대해 채널 ID를 넣어주세요."
        )
    elif "chat not found" in lower:
        hints.append(
            f"채널/채팅방('{chat_id}')을 찾을 수 없습니다. "
            "1) 공개 채널이면 '@채널아이디' 철자를 확인하고, "
            "2) 비공개 채널이면 '-100'으로 시작하는 숫자 ID인지 확인하며, "
            "3) @aec_brief_bot 이 해당 채널의 관리자로 추가되어 있는지 확인해 주세요."
        )
    elif "not enough rights" in lower or "need administrator rights" in lower or code == 403:
        hints.append(
            f"봇이 '{chat_id}' 채널에 글을 쓸 권한이 없습니다. "
            "채널 설정 -> 관리자(Administrators) -> @aec_brief_bot 에서 '메시지 게시(Post messages)' 권한을 켜주세요."
        )
    elif code == 401 or "unauthorized" in lower:
        hints.append(
            "TELEGRAM_BOT_TOKEN 값이 올바르지 않습니다. @BotFather 에서 발급받은 토큰 문자열을 다시 확인해 주세요."
        )

    hint_str = ("\n💡 해결 안내: " + " ".join(hints)) if hints else ""
    return f"Telegram API HTTP {code} 오류: {desc}{hint_str}"


def _tg_get_json(bot_token: str, method: str) -> dict:
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/{method}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(_tg_explain_error(e.code, err_body, "")) from e


def _tg_post_json(bot_token: str, method: str, payload: dict) -> dict:
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/{method}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            _tg_explain_error(e.code, err_body, str(payload.get("chat_id", "")))
        ) from e


def _tg_post_multipart(
    bot_token: str,
    method: str,
    fields: dict[str, str],
    file_field: str,
    file_path: Path,
) -> dict:
    boundary = f"----BriefBoundary{uuid.uuid4().hex}"
    body = bytearray()

    for key, val in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8")
        )
        body.extend(f"{val}\r\n".encode("utf-8"))

    mime_type = mimetypes.guess_type(file_path.name)[0] or "image/png"
    file_bytes = file_path.read_bytes()
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'.encode(
            "utf-8"
        )
    )
    body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
    body.extend(file_bytes)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/{method}"
    req = urllib.request.Request(
        url,
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            _tg_explain_error(e.code, err_body, fields.get("chat_id", ""))
        ) from e


def _list_bot_channels(bot_token: str) -> dict[str, tuple[str, str]]:
    """getUpdates 이벤트에서 봇이 관리자로 추가된 채널/그룹 목록(id -> (title, username))을 수집합니다."""
    channels: dict[str, tuple[str, str]] = {}
    try:
        updates = _tg_get_json(bot_token, "getUpdates").get("result", [])
    except Exception:
        return channels

    for upd in updates:
        for key in ("my_chat_member", "channel_post", "edited_channel_post", "message"):
            obj = upd.get(key)
            if not isinstance(obj, dict):
                continue
            chat = obj.get("chat")
            if not isinstance(chat, dict) or "id" not in chat:
                continue
            if chat.get("type") in ("channel", "supergroup"):
                cid = str(chat["id"])
                title = str(chat.get("title") or "").strip()
                uname = str(chat.get("username") or "").strip()
                channels[cid] = (title, uname)
    return channels


def validate_target_not_self_bot(target: ChannelTarget) -> str:
    """봇 정보를 확인하고 CHAT_ID 오입력(봇 자신 입력, 초대링크/채널명 입력 등)을 점검·보정합니다."""
    me_resp = _tg_get_json(target.bot_token, "getMe")
    bot_info = me_resp.get("result", {})
    bot_username = bot_info.get("username", "")

    normalized_chat = target.chat_id.lstrip("@").lower()
    if bot_username and normalized_chat == bot_username.lower():
        raise RuntimeError(
            f"TELEGRAM_CHAT_ID에 봇 자신의 아이디(@{bot_username})가 설정되어 있습니다.\n"
            "텔레그램 봇은 자기 자신에게 메시지를 보낼 수 없습니다.\n"
            "해결 방법:\n"
            f"  1) 텔레그램에서 새 '채널(Channel)'을 생성하고 @{bot_username} 을 관리자(메시지 게시 권한)로 추가하세요.\n"
            "  2) 공개 채널이면 '@채널아이디'를, 비공개 채널이면 '-100...' 형태의 숫자 ID를 TELEGRAM_CHAT_ID 시크릿에 등록하세요."
        )

    # '@채널명'도 아니고 숫자 ID('-100...')도 아닌 경우 (예: 비공개 초대링크 t.me/+... 또는 채널 제목 입력)
    if not target.chat_id.startswith("@") and not re.match(r"^-?\d+$", target.chat_id):
        known_channels = _list_bot_channels(target.bot_token)
        # 1) 채널 제목이 일치하는 경우 자동 매칭
        for cid, (title, _uname) in known_channels.items():
            if title and title.lower() == target.chat_id.strip().lower():
                print(f"[chat] 채널 제목('{title}') 기반으로 숫자 Chat ID 자동 연결 완료: {cid}")
                target.chat_id = cid
                return bot_username
        # 2) 비공개 초대링크(t.me/+...) 등을 넣었는데 봇에 연결된 채널이 정확히 1개인 경우 자동 매칭
        if len(known_channels) == 1:
            cid, (title, _uname) = next(iter(known_channels.items()))
            print(f"[chat] 봇에 연결된 단일 채널('{title}')로 Chat ID 자동 연결 완료: {cid}")
            target.chat_id = cid
            return bot_username

    return bot_username


def send_brief_to_telegram(
    meta: BriefMeta,
    target: ChannelTarget,
    edit_message_id: int | None = None,
) -> dict:
    max_len = TELEGRAM_CAPTION_MAX if meta.thumb_path else TELEGRAM_MESSAGE_MAX
    html_text, reply_markup = build_telegram_message(meta, max_visible_len=max_len)

    if edit_message_id is not None:
        if meta.thumb_path and meta.thumb_path.exists():
            return _tg_post_json(
                target.bot_token,
                "editMessageCaption",
                {
                    "chat_id": target.chat_id,
                    "message_id": edit_message_id,
                    "caption": html_text,
                    "parse_mode": "HTML",
                    "reply_markup": reply_markup,
                },
            )
        return _tg_post_json(
            target.bot_token,
            "editMessageText",
            {
                "chat_id": target.chat_id,
                "message_id": edit_message_id,
                "text": html_text,
                "parse_mode": "HTML",
                "reply_markup": reply_markup,
                "disable_web_page_preview": False,
            },
        )

    if meta.thumb_path and meta.thumb_path.exists():
        fields: dict[str, str] = {
            "chat_id": target.chat_id,
            "caption": html_text,
            "parse_mode": "HTML",
            "reply_markup": json.dumps(reply_markup, ensure_ascii=False),
        }
        if target.thread_id:
            fields["message_thread_id"] = target.thread_id
        return _tg_post_multipart(
            bot_token=target.bot_token,
            method="sendPhoto",
            fields=fields,
            file_field="photo",
            file_path=meta.thumb_path,
        )

    payload: dict = {
        "chat_id": target.chat_id,
        "text": html_text,
        "parse_mode": "HTML",
        "reply_markup": reply_markup,
        "disable_web_page_preview": False,
    }
    if target.thread_id:
        payload["message_thread_id"] = int(target.thread_id)
    return _tg_post_json(target.bot_token, "sendMessage", payload)


def discover_chats(bot_token: str) -> int:
    me = _tg_get_json(bot_token, "getMe").get("result", {})
    print(f"봇 확인 완료: @{me.get('username')} (이름: {me.get('first_name')}, ID: {me.get('id')})")

    updates = _tg_get_json(bot_token, "getUpdates").get("result", [])
    seen: dict[str, tuple[str, str]] = {}
    for upd in updates:
        for key in ("channel_post", "edited_channel_post", "message", "my_chat_member"):
            obj = upd.get(key)
            if not isinstance(obj, dict):
                continue
            chat = obj.get("chat")
            if not isinstance(chat, dict) or "id" not in chat:
                continue
            cid = str(chat["id"])
            ctype = chat.get("type", "")
            title = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
            uname = f" (@{chat['username']})" if chat.get("username") else ""
            seen[cid] = (ctype, f"{title}{uname}".strip())

    if not seen:
        print(
            "\n최근 감지된 채널/채팅방 이벤트가 없습니다.\n"
            "채널에 봇을 관리자로 초대한 뒤 테스트 글을 1건 올리고 다시 실행하면 숫자 Chat ID(-100...)가 표시됩니다."
        )
        return 0

    print("\n감지된 채팅방 / 채널 목록 (TELEGRAM_CHAT_ID 후보):")
    for cid, (ctype, label) in seen.items():
        print(f"  - chat_id: {cid:<18} | 유형: {ctype:<10} | 이름: {label}")
    return 0


def find_latest_briefs(repo_root: Path, edition_filter: str = "all") -> list[Path]:
    patterns: list[str] = []
    if edition_filter in ("all", "aec"):
        patterns.append("AEC_Brief_*.html")
    if edition_filter in ("all", "ai"):
        patterns.append("AI+_Brief_*.html")

    results: list[Path] = []
    for pattern in patterns:
        matches = sorted(repo_root.glob(pattern))
        if matches:
            results.append(matches[-1])
    return results


def resolve_input_files(raw_files: list[str], repo_root: Path) -> list[Path]:
    if not raw_files or raw_files == ["latest"]:
        return find_latest_briefs(repo_root, "all")
    if raw_files == ["latest-aec"]:
        return find_latest_briefs(repo_root, "aec")
    if raw_files == ["latest-ai"]:
        return find_latest_briefs(repo_root, "ai")

    target_paths: list[Path] = []
    for item in raw_files:
        if item == "latest":
            target_paths.extend(find_latest_briefs(repo_root, "all"))
        elif item == "latest-aec":
            target_paths.extend(find_latest_briefs(repo_root, "aec"))
        elif item == "latest-ai":
            target_paths.extend(find_latest_briefs(repo_root, "ai"))
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
    return target_paths


def main() -> int:
    parser = argparse.ArgumentParser(
        description="새 브리프 발행 시 텔레그램 채널 자동 배포 스크립트"
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="발송할 브리프 HTML 경로 ('latest-aec', 'latest-ai', 'latest' 지원)",
    )
    parser.add_argument(
        "--bot-token",
        default="",
        help="텔레그램 Bot Token 직접 지정 (미지정 시 에디션별 환경변수 사용)",
    )
    parser.add_argument(
        "--chat-id",
        default="",
        help="텔레그램 Chat ID 직접 지정 (예: @aec_brief 또는 -100...)",
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
        "--no-photo",
        action="store_true",
        help="1면 캡처 이미지 없이 텍스트+버튼 메시지로만 발송",
    )
    parser.add_argument(
        "--force-thumbs",
        action="store_true",
        help="기존 thumbs/*.png 가 있어도 1면 이미지를 새로 캡처",
    )
    parser.add_argument(
        "--discover-chats",
        action="store_true",
        help="봇이 참여 중인 채널/채팅방의 Chat ID 목록을 조회 후 종료",
    )
    parser.add_argument(
        "--edit-message-id",
        type=int,
        default=None,
        help="새 메시지 발송 대신 기존 채널 메시지(message_id)의 본문만 제자리에서 수정",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="텔레그램 API를 호출하지 않고 파싱 결과 및 발송 예정 전문만 출력",
    )

    args = parser.parse_args()

    if args.discover_chats:
        tok_raw = (
            args.bot_token
            or os.environ.get("AEC_TELEGRAM_BOT_TOKEN", "").strip()
            or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        )
        if not tok_raw:
            print("오류: --bot-token 또는 TELEGRAM_BOT_TOKEN 환경변수가 필요합니다.", file=sys.stderr)
            return 1
        try:
            return discover_chats(normalize_bot_token(tok_raw))
        except (ValueError, RuntimeError) as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1

    repo_root = Path(__file__).resolve().parents[2]
    target_paths = resolve_input_files(args.files, repo_root)
    if not target_paths:
        print("발송할 브리프 HTML 파일이 없습니다.")
        return 0

    if args.dry_run:
        for path in target_paths:
            meta = parse_brief_html(
                path,
                repo_root=repo_root,
                base_url=args.base_url,
                wait_for_live=False,
                dry_run=True,
                no_photo=args.no_photo,
            )
            max_len = TELEGRAM_CAPTION_MAX if meta.thumb_path else TELEGRAM_MESSAGE_MAX
            msg_html, markup = build_telegram_message(meta, max_visible_len=max_len)
            plain_preview = html.unescape(re.sub(r"<[^>]+>", "", msg_html))
            print("=" * 68)
            print(f"파일명   : {path.name} (에디션: {meta.edition_key.upper()})")
            print(f"발송방식 : {'sendPhoto (1면 이미지 + 캡션)' if meta.thumb_path else 'sendMessage (텍스트)'}")
            print(f"이미지   : {meta.thumb_path if meta.thumb_path else '(없음)'}")
            print(f"링크     : {meta.page_url}")
            print(f"글자수   : {_utf16_len(plain_preview)} / {max_len} 자")
            print("-" * 68)
            print(msg_html)
            print("-" * 68)
            print("버튼     :", [f"{b['text']} -> {b['url']}" for b in markup["inline_keyboard"][0]])
        print("=" * 68)
        return 0

    sent_count = 0
    skipped_ai = False
    missing_aec = False
    validated_tokens: dict[tuple[str, str], str] = {}

    for path in target_paths:
        quick_brand_key, _ = _detect_edition(path.name, "")
        try:
            target = resolve_channel_target(
                quick_brand_key, cli_token=args.bot_token, cli_chat_id=args.chat_id
            )
        except ValueError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1

        if target is None:
            if quick_brand_key == "ai":
                print(
                    f"[skip] {path.name}: AI_TELEGRAM_CHAT_ID 미설정 상태이므로 건너뜁니다 (현재 AEC 채널만 활성)."
                )
                skipped_ai = True
                continue
            print(
                f"오류: {path.name} 발송에 필요한 텔레그램 시크릿(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)이 설정되지 않았습니다.\n"
                "설정 방법은 .github/TELEGRAM_SETUP.md 를 참고하세요.",
                file=sys.stderr,
            )
            missing_aec = True
            continue

        try:
            cache_key = (target.bot_token, target.chat_id)
            if cache_key not in validated_tokens:
                bot_u = validate_target_not_self_bot(target)
                # target.chat_id가 자동 보정되었을 수 있으므로 보정된 키도 함께 캐시
                validated_tokens[cache_key] = bot_u
                validated_tokens[(target.bot_token, target.chat_id)] = bot_u
            bot_uname = validated_tokens[(target.bot_token, target.chat_id)]

            meta = parse_brief_html(
                path,
                repo_root=repo_root,
                base_url=args.base_url,
                wait_for_live=args.wait_for_live,
                dry_run=False,
                no_photo=args.no_photo,
                force_thumbs=args.force_thumbs,
            )
            resp = send_brief_to_telegram(
                meta, target, edit_message_id=args.edit_message_id
            )
        except RuntimeError as exc:
            err_msg = str(exc)
            print(f"❌ {err_msg}", file=sys.stderr)
            if "chat not found" in err_msg.lower() or "찾을 수 없습니다" in err_msg:
                known = _list_bot_channels(target.bot_token)
                if known:
                    print("\n📌 현재 봇이 감지한 채널 목록 (TELEGRAM_CHAT_ID에 아래 ID를 등록하세요):", file=sys.stderr)
                    for cid, (title, uname) in known.items():
                        u_part = f" / @{uname}" if uname else ""
                        print(f"  - {cid} ({title}{u_part})", file=sys.stderr)
            return 1

        if not resp.get("ok"):
            print(f"텔레그램 응답 오류 ({path.name}): {resp}", file=sys.stderr)
            return 1

        msg_id = resp.get("result", {}).get("message_id", args.edit_message_id or "")
        action_lbl = "메시지 수정 완료" if args.edit_message_id else "발송 완료"
        print(
            f"✅ 텔레그램 {action_lbl}: {path.name} -> {target.chat_id} (봇: @{bot_uname}, message_id={msg_id})"
        )
        sent_count += 1

    if missing_aec and sent_count == 0:
        return 1
    if sent_count == 0 and skipped_ai:
        print("발송 대상이 AI+ Brief 뿐이며 AI 채널 시크릿이 아직 설정되지 않아 정상 종료합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
