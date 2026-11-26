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
        for li_raw in re.findall(r"<li[^>]*>(.*?)</li>", tl_m.group(1), re.S | re.I):
            without_idx = re.sub(
                r'<span[^>]*class="[^"]*\bidx\b[^"]*"[^>]*>.*?</span>',
                "",
                li_raw,
                flags=re.S | re.I,
            )
            cleaned_li = _strip_tags(without_idx)
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
                    f"이번 주 숫자 · {_escape_tg_html(meta.cover_label)}: <b>{_escape_tg_html(meta.cover_value)}</b>"
                )
            else:
                blocks.append(f"이번 주 숫자: <b>{_escape_tg_html(meta.cover_value)}</b>")

        if meta.three_lines and (line_limit is None or line_limit > 0):
            summary_rows = ["<b>이번 주 3줄 요약</b>"]
            for idx, item in enumerate(meta.three_lines[:3], start=1):
                clipped = _truncate(item, line_limit) if line_limit else item
                summary_rows.append(f"{idx}. {_escape_tg_html(clipped)}")
            blocks.append("\n".join(summary_rows))

        return "\n\n".join(blocks)

    html_text = _assemble(None)
    plain_check = html.unescape(re.sub(r"<[^>]+>", "", html_text))

    if _utf16_len(plain_check) > max_visible_len:
        for limit in (140, 110, 85, 60, 0):
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


def normalize_chat_id(raw_chat_id: str) -> str:
    val = raw_chat_id.strip()
    if not val:
        return ""
    # https://t.me/channel_name 또는 t.me/channel_name 형태로 입력한 경우 @channel_name으로 자동 보정
    m = re.match(r"^(?:https?://)?t\.me/([A-Za-z0-9_]+)/?$", val, re.I)
    if m:
        slug = m.group(1)
        if not slug.startswith("+"):
            return f"@{slug}"
    return val


def resolve_channel_target(edition_key: str, cli_token: str = "", cli_chat_id: str = "") -> ChannelTarget | None:
    """에디션(aec / ai)에 맞는 봇 토큰과 채널 ID를 반환합니다."""
    env = os.environ
    default_token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    default_chat = env.get("TELEGRAM_CHAT_ID", "").strip()

    if cli_token or cli_chat_id:
        return ChannelTarget(
            edition_key=edition_key,
            bot_token=cli_token or default_token,
            chat_id=normalize_chat_id(cli_chat_id or default_chat),
        )

    if edition_key == "aec":
        token = env.get("AEC_TELEGRAM_BOT_TOKEN", "").strip() or default_token
        chat_id = normalize_chat_id(
            env.get("AEC_TELEGRAM_CHAT_ID", "").strip() or default_chat
        )
        thread_id = env.get("AEC_TELEGRAM_THREAD_ID", "").strip()
        if not token or not chat_id:
            return None
        return ChannelTarget("aec", token, chat_id, thread_id)

    if edition_key == "ai":
        token = env.get("AI_TELEGRAM_BOT_TOKEN", "").strip() or default_token
        # AI+ Brief는 전용 AI_TELEGRAM_CHAT_ID가 설정된 경우에만 발송 (AEC 채널 오발송 방지)
        chat_id = normalize_chat_id(env.get("AI_TELEGRAM_CHAT_ID", "").strip())
        thread_id = env.get("AI_TELEGRAM_THREAD_ID", "").strip()
        if not token or not chat_id:
            return None
        return ChannelTarget("ai", token, chat_id, thread_id)

    if default_token and default_chat:
        return ChannelTarget(edition_key, default_token, normalize_chat_id(default_chat))
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


def validate_target_not_self_bot(target: ChannelTarget) -> str:
    """봇 정보를 확인하고 실수로 봇 자신의 아이디(@..._bot)를 CHAT_ID에 넣지 않았는지 점검합니다."""
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
    return bot_username


def send_brief_to_telegram(meta: BriefMeta, target: ChannelTarget) -> dict:
    max_len = TELEGRAM_CAPTION_MAX if meta.thumb_path else TELEGRAM_MESSAGE_MAX
    html_text, reply_markup = build_telegram_message(meta, max_visible_len=max_len)

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
        "--dry-run",
        action="store_true",
        help="텔레그램 API를 호출하지 않고 파싱 결과 및 발송 예정 전문만 출력",
    )

    args = parser.parse_args()

    if args.discover_chats:
        tok = (
            args.bot_token
            or os.environ.get("AEC_TELEGRAM_BOT_TOKEN", "").strip()
            or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        )
        if not tok:
            print("오류: --bot-token 또는 TELEGRAM_BOT_TOKEN 환경변수가 필요합니다.", file=sys.stderr)
            return 1
        return discover_chats(tok)

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
        # 먼저 에디션만 판별해 미설정 채널(AI+)이면 불필요한 배포 대기 없이 건너뜀
        quick_brand_key, _ = _detect_edition(path.name, "")
        target = resolve_channel_target(
            quick_brand_key, cli_token=args.bot_token, cli_chat_id=args.chat_id
        )
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

        cache_key = (target.bot_token, target.chat_id)
        if cache_key not in validated_tokens:
            validated_tokens[cache_key] = validate_target_not_self_bot(target)
        bot_uname = validated_tokens[cache_key]

        meta = parse_brief_html(
            path,
            repo_root=repo_root,
            base_url=args.base_url,
            wait_for_live=args.wait_for_live,
            dry_run=False,
            no_photo=args.no_photo,
            force_thumbs=args.force_thumbs,
        )
        resp = send_brief_to_telegram(meta, target)
        if not resp.get("ok"):
            print(f"텔레그램 응답 오류 ({path.name}): {resp}", file=sys.stderr)
            return 1

        msg_id = resp.get("result", {}).get("message_id", "")
        print(
            f"✅ 텔레그램 발송 완료: {path.name} -> {target.chat_id} (봇: @{bot_uname}, message_id={msg_id})"
        )
        sent_count += 1

    if missing_aec and sent_count == 0:
        return 1
    if sent_count == 0 and skipped_ai:
        print("발송 대상이 AI+ Brief 뿐이며 AI 채널 시크릿이 아직 설정되지 않아 정상 종료합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
