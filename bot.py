"""텔레그램 ↔ Claude CLI 연동 봇.

텔레그램 메시지를 받으면 `claude -p` (구독 인증 사용)를 실행하고
그 출력을 답장으로 보낸다. 채팅방마다 Claude 세션을 유지해서
대화 맥락이 이어진다.

실행 전 필요한 것:
  export TELEGRAM_BOT_TOKEN="봇파더에게 받은 토큰"
  export ALLOWED_USER_IDS="내 텔레그램 유저 ID"   # 비우면 아무나 사용 가능 (비추천)
  # 서버(headless)에서는 추가로:
  export CLAUDE_CODE_OAUTH_TOKEN="claude setup-token 으로 발급한 토큰"
"""

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("claude-bot")

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "600"))  # 초

def _parse_allowed_user_ids() -> set[int]:
    """ALLOWED_USER_IDS 환경변수를 파싱한다. 잘못된 값이면 즉시 종료 —
    조용히 무시하면 '빈 허용 목록 = 전체 허용'으로 오작동할 수 있다."""
    raw = os.environ.get("ALLOWED_USER_IDS", "").strip()
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise SystemExit(f"ALLOWED_USER_IDS 형식 오류: {part!r} — 숫자 ID를 쉼표로 구분해 주세요")
        ids.add(int(part))
    return ids


# 시작 시 1회만 파싱해서 모든 곳(접근 제한 + 코딩 모드 판정)이 같은 값을 쓴다
ALLOWED_IDS = _parse_allowed_user_ids()

# 코딩 모드(기본 켜짐): Claude가 workspace 폴더 안에서 실제 파일 생성/수정/명령
# 실행까지 한다. 대화 전용으로 바꾸려면 CLAUDE_PERMISSION_MODE=off 로 설정.
# 단, 허용 목록이 비어 있으면(누구나 사용 가능) 아무나 서버 명령을
# 실행할 수 있게 되므로 코딩 모드를 강제로 끈다.
_mode = os.environ.get("CLAUDE_PERMISSION_MODE", "bypassPermissions").strip()
CLAUDE_PERMISSION_MODE = "" if _mode.lower() in ("", "off", "none") else _mode
if CLAUDE_PERMISSION_MODE and not ALLOWED_IDS:
    log.warning("ALLOWED_USER_IDS가 비어 있어 코딩 모드를 끕니다 (아무나 서버 명령 실행 방지)")
    CLAUDE_PERMISSION_MODE = ""

# 봇 전용 작업 폴더 — claude가 여기를 cwd로 실행됨
WORKDIR = Path(__file__).parent / "workspace"
WORKDIR.mkdir(exist_ok=True)

# 텔레그램으로 받은 사진·파일을 저장하는 폴더 (Claude가 여기서 읽음)
UPLOADS_DIR = WORKDIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

# 채팅방별 세션 ID를 저장해서 봇을 재시작해도 대화가 이어지게 함
SESSIONS_FILE = Path(__file__).parent / "sessions.json"

# 텔레그램에 어울리는 답변 스타일. 취향대로 수정하세요.
SYSTEM_PROMPT = (
    "당신은 텔레그램 메신저에서 대화하는 어시스턴트입니다. "
    "답변은 한국어로, 메신저에 어울리게 간결하게 작성하세요. "
    "표나 복잡한 마크다운은 피하고 짧은 문단 위주로 답하세요."
)

TELEGRAM_MSG_LIMIT = 4000  # 실제 한도는 4096, 여유를 둠


def load_sessions() -> dict[str, str]:
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("sessions.json 파싱 실패 — 새로 시작합니다")
    return {}


def save_sessions(sessions: dict[str, str]) -> None:
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2))


sessions = load_sessions()
chat_locks: dict[int, asyncio.Lock] = {}




async def run_claude(chat_id: int, prompt: str) -> str:
    """chat_id의 세션으로 claude -p를 실행하고 응답 텍스트를 돌려준다."""
    key = str(chat_id)
    cmd = [CLAUDE_BIN, "-p", "--output-format", "text"]
    if CLAUDE_PERMISSION_MODE:
        cmd += ["--permission-mode", CLAUDE_PERMISSION_MODE]

    if key in sessions:
        cmd += ["--resume", sessions[key]]
    else:
        session_id = str(uuid.uuid4())
        sessions[key] = session_id
        save_sessions(sessions)
        cmd += ["--session-id", session_id, "--system-prompt", SYSTEM_PROMPT]

    cmd.append(prompt)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=WORKDIR,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CLAUDE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return "⏰ 응답 시간이 너무 오래 걸려 중단했어요. 다시 시도해 주세요."

    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()
        log.error("claude 실행 실패 (chat=%s): %s", chat_id, err)
        # 세션이 깨진 경우가 많으므로 리셋해서 다음 메시지는 새 대화로 시작
        sessions.pop(key, None)
        save_sessions(sessions)
        return f"⚠️ Claude 실행에 실패했어요. 세션을 초기화했으니 다시 보내주세요.\n({err[:200]})"

    text = stdout.decode(errors="replace").strip()
    return text or "(빈 응답)"


def split_message(text: str) -> list[str]:
    """텔레그램 글자수 제한(4096)에 맞게 나눈다."""
    chunks = []
    while text:
        chunks.append(text[:TELEGRAM_MSG_LIMIT])
        text = text[TELEGRAM_MSG_LIMIT:]
    return chunks


def is_allowed(update: Update) -> bool:
    if not ALLOWED_IDS:
        return True  # 허용 목록이 비어 있으면 전체 허용 (이때 코딩 모드는 자동 꺼짐)
    return update.effective_user is not None and update.effective_user.id in ALLOWED_IDS


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "안녕하세요! 메시지를 보내면 Claude가 답해드려요.\n"
        "/new — 대화 초기화\n"
        f"당신의 유저 ID: {update.effective_user.id}"
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    sessions.pop(str(update.effective_chat.id), None)
    save_sessions(sessions)
    await update.message.reply_text("🆕 새 대화를 시작합니다.")


async def save_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """메시지에 사진/문서가 있으면 uploads/에 저장하고 파일 경로를 돌려준다."""
    msg = update.message
    tg_file = None
    filename = None

    if msg.photo:  # 사진은 여러 해상도가 오는데 마지막 것이 가장 크다
        tg_file = await msg.photo[-1].get_file()
        filename = f"photo_{msg.photo[-1].file_unique_id}.jpg"
    elif msg.document:
        tg_file = await msg.document.get_file()
        # 원본 파일명 유지하되 경로 조작 방지를 위해 basename만 사용
        filename = os.path.basename(msg.document.file_name or f"file_{msg.document.file_unique_id}")

    if not tg_file:
        return None

    dest = UPLOADS_DIR / filename
    await tg_file.download_to_drive(custom_path=str(dest))
    log.info("첨부 저장: %s", dest)
    return str(dest)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        log.info("허용되지 않은 사용자 차단: %s", update.effective_user.id if update.effective_user else "?")
        return

    chat_id = update.effective_chat.id
    # 사진은 caption, 일반 메시지는 text 에 내용이 담긴다
    prompt = (update.message.text or update.message.caption or "").strip()

    lock = chat_locks.setdefault(chat_id, asyncio.Lock())
    if lock.locked():
        await update.message.reply_text("🤔 이전 질문에 아직 답하는 중이에요. 잠시만요…")

    async with lock:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        attachment = await save_attachment(update, context)
        if attachment:
            # Claude가 읽을 수 있도록 저장 경로를 프롬프트에 포함
            note = f"[사용자가 파일을 첨부함: {attachment}]"
            prompt = f"{note}\n{prompt}" if prompt else f"{note}\n이 파일을 확인하고 설명해 주세요."
        elif not prompt:
            return  # 내용 없는 메시지는 무시

        reply = await run_claude(chat_id, prompt)
        for chunk in split_message(reply):
            await update.message.reply_text(chunk)


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN 환경변수를 설정해 주세요. (@BotFather 에서 발급)")

    # Python 3.12+ 에서는 메인 스레드에 이벤트 루프가 자동 생성되지 않으므로 직접 만든다
    asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    # 텍스트 + 사진 + 문서 모두 처리
    app.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.Document.ALL,
        on_message,
    ))

    log.info("봇 시작! (작업 폴더: %s)", WORKDIR)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
