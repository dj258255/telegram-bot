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
import time
import uuid
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("claude-bot")

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "600"))  # 초

# 사용 모델. 비우면 구독 기본값(보통 Sonnet). "opus"/"sonnet" 또는 정확한 모델명.
# 채팅 중 /model 명령으로 바꾸면 이 값을 덮어쓴다.
DEFAULT_MODEL = os.environ.get("CLAUDE_MODEL", "").strip()

# tool_use 이벤트를 텔레그램에 보여줄 사람말로 변환
TOOL_LABELS = {
    "Write": "📝 파일 작성",
    "Edit": "✏️ 파일 수정",
    "MultiEdit": "✏️ 파일 수정",
    "Read": "👀 파일 읽기",
    "Bash": "⚡ 명령 실행",
    "Glob": "🔍 파일 탐색",
    "Grep": "🔍 내용 검색",
    "WebSearch": "🌐 웹 검색",
    "WebFetch": "🌐 웹 페이지 열기",
    "TodoWrite": "📋 계획 정리",
    "Task": "🤖 하위 작업",
}


def describe_tool(name: str, tool_input: dict) -> str:
    """tool_use 블록을 텔레그램에 보여줄 한 줄로 요약."""
    label = TOOL_LABELS.get(name, f"🔧 {name}")
    detail = ""
    if name in ("Write", "Edit", "MultiEdit", "Read"):
        detail = os.path.basename(str(tool_input.get("file_path", "")))
    elif name == "Bash":
        detail = str(tool_input.get("description") or tool_input.get("command", ""))[:60]
    elif name in ("Glob", "Grep"):
        detail = str(tool_input.get("pattern", ""))[:40]
    elif name in ("WebSearch", "WebFetch"):
        detail = str(tool_input.get("query") or tool_input.get("url", ""))[:50]
    return f"{label}: {detail}" if detail else label

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

# MCP 서버 설정 (context7 등). 있으면 봇 실행 시 자동 로드.
MCP_CONFIG = WORKDIR / ".mcp.json"

# 채팅방별 세션 ID를 저장해서 봇을 재시작해도 대화가 이어지게 함
SESSIONS_FILE = Path(__file__).parent / "sessions.json"

# 텔레그램에 어울리는 답변 스타일. 취향대로 수정하세요.
SYSTEM_PROMPT = (
    "당신은 텔레그램 메신저에서 대화하는 어시스턴트입니다. "
    "답변은 한국어로, 메신저에 어울리게 간결하게 작성하세요. "
    "표나 복잡한 마크다운은 피하고 짧은 문단 위주로 답하세요."
)

TELEGRAM_MSG_LIMIT = 4000  # 실제 한도는 4096, 여유를 둠

# 코딩 모드에서 명령 실행 전 취소할 수 있는 대기 시간(초). 0이면 대기 없음.
CANCEL_DELAY = int(os.environ.get("CANCEL_DELAY", "4"))

# 실행 중인 claude 프로세스 (중단 버튼이 죽일 수 있게 보관)
running_procs: dict[int, asyncio.subprocess.Process] = {}
# 시작 전 대기 중 취소 신호
pending_cancel: dict[int, asyncio.Event] = {}


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
START_TIME = time.time()
# 채팅방별 작업 디렉터리 오버라이드 (/cd 명령). 기본은 WORKDIR.
chat_workdirs: dict[int, Path] = {}


def fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}일")
    if h:
        parts.append(f"{h}시간")
    parts.append(f"{m}분")
    return " ".join(parts)




# 채팅방별 모델 오버라이드 (/model 명령으로 설정)
chat_models: dict[int, str] = {}


async def run_claude(chat_id: int, prompt: str, on_progress=None) -> str:
    """chat_id의 세션으로 claude를 스트리밍 실행한다.
    tool_use가 나올 때마다 on_progress(설명) 콜백을 호출하고, 최종 답변 텍스트를 반환한다.
    """
    key = str(chat_id)
    cmd = [CLAUDE_BIN, "-p", "--output-format", "stream-json", "--verbose"]
    if CLAUDE_PERMISSION_MODE:
        cmd += ["--permission-mode", CLAUDE_PERMISSION_MODE]
    model = chat_models.get(chat_id, DEFAULT_MODEL)
    if model:
        cmd += ["--model", model]
    # MCP 서버(context7 등)는 cwd(workspace)의 .mcp.json 에서 자동 로드된다.

    if key in sessions:
        cmd += ["--resume", sessions[key]]
    else:
        session_id = str(uuid.uuid4())
        sessions[key] = session_id
        save_sessions(sessions)
        cmd += ["--session-id", session_id, "--system-prompt", SYSTEM_PROMPT]

    cmd.append(prompt)

    workdir = chat_workdirs.get(chat_id, WORKDIR)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(workdir),
    )
    running_procs[chat_id] = proc  # 중단 버튼이 이 프로세스를 죽일 수 있게 등록

    final_text = ""

    async def read_stream() -> None:
        nonlocal final_text
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use" and on_progress:
                        await on_progress(describe_tool(block.get("name", ""), block.get("input", {}) or {}))
            elif etype == "result":
                final_text = str(event.get("result", "") or "")

    try:
        await asyncio.wait_for(read_stream(), timeout=CLAUDE_TIMEOUT)
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        return "⏰ 응답 시간이 너무 오래 걸려 중단했어요. 다시 시도해 주세요."
    finally:
        running_procs.pop(chat_id, None)

    # 중단 버튼으로 죽인 경우 (kill → 음수 리턴코드)
    if proc.returncode and proc.returncode < 0:
        return "🛑 중단했어요. (진행 중이던 작업은 여기서 멈춥니다)"

    if proc.returncode != 0:
        stderr = (await proc.stderr.read()).decode(errors="replace").strip() if proc.stderr else ""
        log.error("claude 실행 실패 (chat=%s): %s", chat_id, stderr)
        sessions.pop(key, None)
        save_sessions(sessions)
        return f"⚠️ Claude 실행에 실패했어요. 세션을 초기화했으니 다시 보내주세요.\n({stderr[:200]})"

    return final_text.strip() or "(빈 응답)"


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
        "사진·파일을 보내면 분석하고, 코딩도 실제로 해드려요.\n\n"
        "/new — 대화 초기화\n"
        "/model — 모델 확인·변경 (fable / opus / sonnet)\n"
        "/status — 봇 상태 확인\n"
        "/cd — 작업 폴더 전환\n"
        "/ls — 현재 폴더 파일 목록\n"
        f"\n당신의 유저 ID: {update.effective_user.id}"
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    sessions.pop(str(update.effective_chat.id), None)
    save_sessions(sessions)
    await update.message.reply_text("🆕 새 대화를 시작합니다.")


# 고를 수 있는 모델 별칭과 설명
MODEL_CHOICES = {
    "fable": "가장 똑똑한 최신 모델 (어려운 작업·긴 코딩). 느리고 사용량 많음",
    "opus": "고성능. 복잡한 코딩·추론에 강함",
    "sonnet": "빠르고 균형 잡힘. 일상 작업에 적합 (기본)",
    "haiku": "가장 빠르고 가벼움. 간단한 질문용",
}


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    arg = " ".join(context.args).strip() if context.args else ""
    if not arg:
        current = chat_models.get(chat_id, DEFAULT_MODEL) or "기본값(구독, 보통 sonnet)"
        menu = "\n".join(f"• {name} — {desc}" for name, desc in MODEL_CHOICES.items())
        await update.message.reply_text(
            f"현재 모델: {current}\n\n"
            f"고를 수 있는 모델:\n{menu}\n\n"
            "바꾸기: /model fable  ·  /model opus  ·  /model sonnet\n"
            "기본값으로: /model default"
        )
        return
    if arg.lower() in ("default", "기본", "reset"):
        chat_models.pop(chat_id, None)
        await update.message.reply_text("모델을 기본값으로 되돌렸어요.")
    else:
        chat_models[chat_id] = arg
        note = f" — {MODEL_CHOICES[arg.lower()]}" if arg.lower() in MODEL_CHOICES else ""
        await update.message.reply_text(f"모델을 '{arg}'(으)로 설정했어요{note}. (다음 메시지부터 적용)")


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


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    model = chat_models.get(chat_id, DEFAULT_MODEL) or "기본(구독)"
    workdir = chat_workdirs.get(chat_id, WORKDIR)
    mode = "코딩 모드" if CLAUDE_PERMISSION_MODE else "대화 전용"
    busy = "작업 중" if chat_id in running_procs else "대기 중"
    # 서버 부하 (load average)
    try:
        load1, load5, _ = os.getloadavg()
        load = f"{load1:.2f} / {load5:.2f}"
    except OSError:
        load = "N/A"
    await update.message.reply_text(
        f"🤖 봇 상태\n"
        f"- 가동시간: {fmt_uptime(time.time() - START_TIME)}\n"
        f"- 모델: {model}\n"
        f"- 모드: {mode}\n"
        f"- 현재: {busy}\n"
        f"- 저장된 대화: {len(sessions)}개\n"
        f"- 작업 폴더: {workdir}\n"
        f"- 서버 부하(1분/5분): {load}"
    )


async def cmd_cd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    arg = " ".join(context.args).strip() if context.args else ""
    if not arg or arg.lower() in ("reset", "default", "기본"):
        chat_workdirs.pop(chat_id, None)
        await update.message.reply_text(f"작업 폴더를 기본값으로 되돌렸어요.\n{WORKDIR}")
        return
    target = Path(arg).expanduser()
    if not target.is_absolute():
        target = WORKDIR / target
    if not target.is_dir():
        await update.message.reply_text(f"그런 폴더가 없어요: {target}")
        return
    chat_workdirs[chat_id] = target
    await update.message.reply_text(f"작업 폴더를 바꿨어요:\n{target}\n\n/ls 로 내용을 볼 수 있어요.")


async def cmd_ls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    base = chat_workdirs.get(chat_id, WORKDIR)
    arg = " ".join(context.args).strip() if context.args else ""
    target = (base / arg).expanduser() if arg else base
    if arg and Path(arg).is_absolute():
        target = Path(arg)
    if not target.is_dir():
        await update.message.reply_text(f"그런 폴더가 없어요: {target}")
        return
    try:
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        await update.message.reply_text(f"폴더를 열 권한이 없어요: {target}")
        return
    if not entries:
        await update.message.reply_text(f"📂 {target}\n(비어 있음)")
        return
    lines = []
    for p in entries[:100]:  # 너무 길면 100개까지만
        if p.is_dir():
            lines.append(f"📁 {p.name}/")
        else:
            size = p.stat().st_size
            unit = f"{size}B" if size < 1024 else f"{size // 1024}KB"
            lines.append(f"📄 {p.name} ({unit})")
    more = f"\n… 외 {len(entries) - 100}개" if len(entries) > 100 else ""
    body = f"📂 {target}\n" + "\n".join(lines) + more
    for chunk in split_message(body):
        await update.message.reply_text(chunk)


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
        attachment = await save_attachment(update, context)
        if attachment:
            # Claude가 읽을 수 있도록 저장 경로를 프롬프트에 포함
            note = f"[사용자가 파일을 첨부함: {attachment}]"
            prompt = f"{note}\n{prompt}" if prompt else f"{note}\n이 파일을 확인하고 설명해 주세요."
        elif not prompt:
            return  # 내용 없는 메시지는 무시

        # 코딩 모드면 실행 전 잠깐 취소 기회를 준다 (잘못 보낸 명령 방어)
        status_msg = await update.message.reply_text("🤔 생각 중…")
        if CLAUDE_PERMISSION_MODE and CANCEL_DELAY > 0:
            cancel_ev = asyncio.Event()
            pending_cancel[chat_id] = cancel_ev
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚫 취소", callback_data="cancel")]])
            await context.bot.edit_message_text(
                f"⏳ {CANCEL_DELAY}초 뒤 시작해요. 잘못 보냈다면 취소를 누르세요.",
                chat_id=chat_id, message_id=status_msg.message_id, reply_markup=kb,
            )
            try:
                await asyncio.wait_for(cancel_ev.wait(), timeout=CANCEL_DELAY)
                await context.bot.edit_message_text(
                    "🚫 취소했어요.", chat_id=chat_id, message_id=status_msg.message_id,
                )
                return
            except asyncio.TimeoutError:
                pass  # 시간 지나면 그대로 진행
            finally:
                pending_cancel.pop(chat_id, None)

        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        # 진행 상황 표시 + 중단 버튼
        actions: list[str] = []
        last_edit = 0.0
        stop_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 중단", callback_data="stop")]])

        async def refresh(text: str) -> None:
            try:
                await context.bot.edit_message_text(
                    text, chat_id=chat_id, message_id=status_msg.message_id, reply_markup=stop_kb,
                )
            except Exception:
                pass

        await refresh("🤔 작업 시작…")
        work_start = asyncio.get_event_loop().time()

        async def on_progress(desc: str) -> None:
            nonlocal last_edit
            actions.append(desc)
            now = asyncio.get_event_loop().time()
            if now - last_edit < 1.5:  # 텔레그램 편집 제한 대비
                return
            last_edit = now
            await refresh("\n".join(actions[-6:]))
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        reply = await run_claude(chat_id, prompt, on_progress=on_progress)

        # 진행 메시지 지우고 최종 답변 전송
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        except Exception:
            pass
        # 30초 넘게 걸린 긴 작업이면 완료 표시를 앞에 붙여 눈에 띄게
        elapsed = asyncio.get_event_loop().time() - work_start
        prefix = "✅ 완료!\n\n" if elapsed > 30 else ""
        chunks = split_message(prefix + reply)
        for chunk in chunks:
            await update.message.reply_text(chunk)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """취소/중단 버튼 처리."""
    query = update.callback_query
    if not is_allowed(update):
        await query.answer("권한이 없어요.")
        return
    chat_id = query.message.chat_id
    if query.data == "cancel":
        ev = pending_cancel.get(chat_id)
        if ev:
            ev.set()
            await query.answer("취소했어요.")
        else:
            await query.answer("이미 시작됐어요. 중단 버튼을 쓰세요.")
    elif query.data == "stop":
        proc = running_procs.get(chat_id)
        if proc:
            proc.kill()
            await query.answer("중단하는 중…")
        else:
            await query.answer("실행 중인 작업이 없어요.")


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN 환경변수를 설정해 주세요. (@BotFather 에서 발급)")

    # Python 3.12+ 에서는 메인 스레드에 이벤트 루프가 자동 생성되지 않으므로 직접 만든다
    asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("ls", cmd_ls))
    app.add_handler(CallbackQueryHandler(on_button))
    # 텍스트 + 사진 + 문서 모두 처리
    app.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.Document.ALL,
        on_message,
    ))

    log.info("봇 시작! (작업 폴더: %s)", WORKDIR)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
