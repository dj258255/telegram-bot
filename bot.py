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
import signal
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO
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
# httpx가 INFO로 요청 URL(텔레그램 봇 토큰 포함)을 로그에 찍는 걸 막는다 (토큰 노출 방지)
logging.getLogger("httpx").setLevel(logging.WARNING)

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
# 응답 대기 한도. fable+xhigh 같은 무거운 설정은 한 작업이 오래 걸리므로 넉넉히 둔다.
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "3600"))  # 초 (기본 1시간)

# 기본 모델·강도는 코드에 박아둔다 (git으로 배포되므로 env 파일과 무관하게 따라감).
# 환경변수로 덮어쓸 수 있고, 채팅 중 /model·/effort 로도 바꾼다.
# 값을 비우고 싶으면(구독 기본값) 환경변수에 "default" 를 넣는다.
def _env_default(key: str, fallback: str) -> str:
    v = os.environ.get(key)
    if v is None:  # 환경변수 자체가 없으면 코드 기본값
        return fallback
    v = v.strip()
    if v.lower() in ("default", "기본"):  # 명시적으로 구독 기본값을 원하면 비움
        return ""
    return v or fallback  # 빈 문자열이면 코드 기본값

DEFAULT_MODEL = _env_default("CLAUDE_MODEL", "opus")
DEFAULT_EFFORT = _env_default("CLAUDE_EFFORT", "max")

# 음성 메시지 → 텍스트 변환용 Groq API 키 (없으면 음성 기능 비활성)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

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
# 다중세션: 채팅방별 '이름 붙인 대화 스레드' 메타(활성 이름 + 이름→세션id). sessions.json은
# 그대로(활성 세션 id) 두어 구버전·롤백과 호환한다. 이 파일은 신버전만 읽고 구버전은 무시.
THREADS_FILE = Path(__file__).parent / "threads.json"
# 리마인더(/remind): 배포로 재시작돼도 안 날아가게 파일에 저장하고 시작 시 복원한다.
REMINDERS_FILE = Path(__file__).parent / "reminders.json"
# 실행 중인 리마인더 태스크 참조 — asyncio가 태스크를 약한참조만 들어 GC하는 걸 막는다.
_reminder_tasks: set = set()

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
# 채팅방별 누적 토큰·비용 (claude 응답이 보고한 값). 인메모리 — 봇 재시작 시 초기화. /usage 로 확인
usage_totals: dict[int, dict] = {}

# 작업 중 표시 파일. 배포 스크립트가 이 파일이 사라질 때까지 재시작을 미룬다.
BUSY_MARKER = WORKDIR / ".busy"


def _sync_busy_marker() -> None:
    """실행 중인 작업이 있으면 표시 파일을 만들고, 없으면 지운다."""
    try:
        if running_procs:
            BUSY_MARKER.touch()
        elif BUSY_MARKER.exists():
            BUSY_MARKER.unlink()
    except OSError:
        pass


def kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """claude 프로세스와 그 자식·손자까지 프로세스 그룹째로 죽인다.
    (claude가 띄운 bash·빌드 명령이 살아남아 파이프를 잡고 있는 걸 막는다.)"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()  # 그룹 죽이기가 안 되면 최소한 본체라도
        except ProcessLookupError:
            pass


def _atomic_write(path: Path, text: str) -> None:
    """temp 파일에 쓰고 rename — 쓰는 중 죽어도 원본이 반쯤 덮여 깨지지 않게(원자적)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def prune_uploads(max_age_days: int = 7) -> int:
    """uploads/의 오래된 첨부를 지워 디스크 무한 증가를 막는다. 지운 개수 반환."""
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    try:
        for p in UPLOADS_DIR.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed


def load_sessions() -> dict[str, str]:
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("sessions.json 파싱 실패 — 새로 시작합니다")
    return {}


def save_sessions(sessions: dict[str, str]) -> None:
    _atomic_write(SESSIONS_FILE, json.dumps(sessions, indent=2))


def load_threads() -> dict:
    if THREADS_FILE.exists():
        try:
            return json.loads(THREADS_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("threads.json 파싱 실패 — 새로 시작합니다")
    return {}


def save_threads(t: dict) -> None:
    _atomic_write(THREADS_FILE, json.dumps(t, ensure_ascii=False, indent=2))


def load_reminders() -> dict:
    if REMINDERS_FILE.exists():
        try:
            d = json.loads(REMINDERS_FILE.read_text())
            if isinstance(d, dict) and "items" in d:
                return d
        except json.JSONDecodeError:
            log.warning("reminders.json 파싱 실패 — 새로 시작합니다")
    return {"seq": 0, "items": []}


def save_reminders() -> None:
    _atomic_write(REMINDERS_FILE, json.dumps(reminders, ensure_ascii=False, indent=2))


def parse_duration_seconds(s: str) -> int | None:
    """'30s'·'10m'·'2h'·'1d'·'45'(단위 없으면 분) → 초. 잘못되면 None. (테스트용 순수 함수)"""
    s = (s or "").strip().lower()
    if not s:
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s[-1] in units and s[:-1].isdigit():
        return int(s[:-1]) * units[s[-1]]
    if s.isdigit():
        return int(s) * 60
    return None


def schedule_reminder(app, item: dict) -> None:
    """due(epoch) 시각에 알림을 보내고 목록에서 지우는 비동기 태스크를 건다."""
    async def _fire():
        delay = item["due"] - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            await app.bot.send_message(chat_id=item["chat_id"], text=f"⏰ 알림: {item['text']}")
        except Exception:
            pass
        reminders["items"] = [r for r in reminders["items"] if r["id"] != item["id"]]
        save_reminders()

    task = asyncio.create_task(_fire())
    _reminder_tasks.add(task)  # GC로 태스크가 사라지지 않게 참조 보관
    task.add_done_callback(_reminder_tasks.discard)


DEFAULT_THREAD = "기본"


def format_session_lines(entry: dict) -> str:
    """스레드 목록을 사람이 볼 문자열로. 활성엔 ▶, 아직 대화 없는 스레드엔 (새 대화). (테스트용 순수 함수)"""
    active = entry.get("active") or DEFAULT_THREAD
    names = entry.get("names") or {}
    if not names:
        return f"▶ {active} (새 대화)"
    lines = []
    for n, sid in names.items():
        mark = "▶ " if n == active else "   "
        lines.append(f"{mark}{n}{'' if sid else ' (새 대화)'}")
    return "\n".join(lines)


sessions = load_sessions()
chat_threads = load_threads()
reminders = load_reminders()
chat_locks: dict[int, asyncio.Lock] = {}
START_TIME = time.time()
# 채팅방별 작업 디렉터리 오버라이드 (/cd 명령). 기본은 WORKDIR.
chat_workdirs: dict[int, Path] = {}
# 채팅방별 "수정 파일 첨부 전송" 켜짐 여부 (/files 명령). 기본 켜짐.
send_files_on: dict[int, bool] = {}
# 첨부로 보낼 파일 상한 (너무 많거나 큰 파일로 도배 방지)
MAX_FILES_SEND = 10
MAX_FILE_BYTES = 1_000_000  # 1MB
# 답장(reply) 맥락으로 붙이는 인용문 최대 길이 (긴 답변에 답장해도 프롬프트가 안 터지게)
REPLY_QUOTE_LIMIT = 1500
# 이 길이를 넘는 답변은 텍스트 조각과 함께 .md 파일로도 첨부 (긴 답변 저장·가독성)
LONG_ANSWER_LIMIT = 6000


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
# 채팅방별 사고 강도 오버라이드 (/effort 명령으로 설정)
chat_efforts: dict[int, str] = {}


async def run_claude(chat_id: int, prompt: str, on_progress=None, touched_files: set | None = None) -> str:
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
    effort = chat_efforts.get(chat_id, DEFAULT_EFFORT)
    if effort:
        cmd += ["--effort", effort]
    # MCP 서버(context7 등)는 cwd(workspace)의 .mcp.json 에서 자동 로드된다.

    if key in sessions:
        cmd += ["--resume", sessions[key]]
    else:
        session_id = str(uuid.uuid4())
        sessions[key] = session_id
        save_sessions(sessions)
        # 다중세션: 새로 만든 세션 id를 현재 활성 스레드에 기록
        entry = chat_threads.setdefault(key, {"active": DEFAULT_THREAD, "names": {}})
        entry["names"][entry.get("active") or DEFAULT_THREAD] = session_id
        save_threads(chat_threads)
        cmd += ["--session-id", session_id, "--system-prompt", SYSTEM_PROMPT]

    cmd.append(prompt)

    workdir = chat_workdirs.get(chat_id, WORKDIR)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(workdir),
        start_new_session=True,  # 별도 프로세스 그룹 → 중단 시 자식까지 한 번에 정리
    )
    running_procs[chat_id] = proc  # 중단 버튼이 이 프로세스를 죽일 수 있게 등록
    _sync_busy_marker()  # 작업 중 표시 → 배포가 이걸 보고 재시작을 미룬다

    final_text = ""
    agents: dict[str, int] = {}  # Task 도구 tool_use_id → 하위 에이전트 번호

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
            # 하위 에이전트가 낸 이벤트는 parent_tool_use_id 로 어느 에이전트인지 구분된다
            parent_id = event.get("parent_tool_use_id")
            if etype == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") != "tool_use" or not on_progress:
                        continue
                    name = block.get("name", "")
                    tinput = block.get("input", {}) or {}
                    # 만들거나 고친 파일 경로를 수집 (작업 끝나고 첨부로 보내기 위해)
                    if name in ("Write", "Edit", "MultiEdit") and touched_files is not None:
                        fp = tinput.get("file_path")
                        if fp:
                            touched_files.add(str(fp))
                    if name == "Task":
                        # 새 하위 에이전트 생성 — 번호를 매겨 구분
                        n = len(agents) + 1
                        agents[block.get("id", "")] = n
                        label = tinput.get("description") or tinput.get("subagent_type") or "하위 작업"
                        await on_progress(f"🤖 에이전트 {n} 시작: {label}")
                    elif parent_id in agents:
                        # 특정 하위 에이전트의 도구 사용 → "에이전트 N · ..." 로 표시
                        await on_progress(f"🤖 {agents[parent_id]} · {describe_tool(name, tinput)}")
                    else:
                        await on_progress(describe_tool(name, tinput))
            elif etype == "result":
                final_text = str(event.get("result", "") or "")
                accumulate_usage(usage_totals.setdefault(chat_id, {}), event)

    try:
        await asyncio.wait_for(read_stream(), timeout=CLAUDE_TIMEOUT)
        await proc.wait()
    except asyncio.TimeoutError:
        kill_process_group(proc)
        return "⏰ 응답 시간이 너무 오래 걸려 중단했어요. 다시 시도해 주세요."
    finally:
        running_procs.pop(chat_id, None)
        _sync_busy_marker()  # 남은 작업이 없으면 표시 제거

    # 중단 버튼으로 죽인 경우 (kill → 음수 리턴코드)
    if proc.returncode and proc.returncode < 0:
        return "🛑 중단했어요. (진행 중이던 작업은 여기서 멈춥니다)"

    if proc.returncode != 0:
        stderr = (await proc.stderr.read()).decode(errors="replace").strip() if proc.stderr else ""
        log.error("claude 실행 실패 (chat=%s): %s", chat_id, stderr)
        sessions.pop(key, None)
        save_sessions(sessions)
        entry = chat_threads.get(key)
        if entry and entry.get("names"):
            entry["names"][entry.get("active") or DEFAULT_THREAD] = None
            save_threads(chat_threads)
        return f"⚠️ Claude 실행에 실패했어요. 세션을 초기화했으니 다시 보내주세요.\n({stderr[:200]})"

    return final_text.strip() or "(빈 응답)"


def split_message(text: str) -> list[str]:
    """텔레그램 글자수 제한(4096)에 맞게 나눈다."""
    chunks = []
    while text:
        chunks.append(text[:TELEGRAM_MSG_LIMIT])
        text = text[TELEGRAM_MSG_LIMIT:]
    return chunks


def build_prompt_with_reply(prompt: str, replied_text: str | None) -> str:
    """텔레그램 '답장(reply)' 대상 메시지가 있으면 인용으로 맥락을 붙인다.
    길면 잘라내고, 인용이 비어 있으면 원문 그대로 둔다. (테스트를 위해 순수 함수로 분리)"""
    quoted = (replied_text or "").strip()
    if not quoted:
        return prompt
    if len(quoted) > REPLY_QUOTE_LIMIT:
        quoted = quoted[:REPLY_QUOTE_LIMIT] + " …(생략)"
    return f"[사용자가 이 메시지에 답장함]\n> {quoted}\n\n{prompt}".strip()


def accumulate_usage(acc: dict, event: dict) -> dict:
    """claude 'result' 이벤트의 usage/비용을 누적 딕셔너리에 더한다. 필드가 없어도 안전. (테스트용 분리)"""
    u = event.get("usage") or {}
    acc["input"] = acc.get("input", 0) + int(u.get("input_tokens", 0) or 0)
    acc["output"] = acc.get("output", 0) + int(u.get("output_tokens", 0) or 0)
    acc["cache_read"] = acc.get("cache_read", 0) + int(u.get("cache_read_input_tokens", 0) or 0)
    acc["cost"] = acc.get("cost", 0.0) + float(event.get("total_cost_usd", 0) or 0)
    acc["turns"] = acc.get("turns", 0) + 1
    return acc


def is_allowed(update: Update) -> bool:
    if not ALLOWED_IDS:
        return True  # 허용 목록이 비어 있으면 전체 허용 (이때 코딩 모드는 자동 꺼짐)
    return update.effective_user is not None and update.effective_user.id in ALLOWED_IDS


# 명령어 목록 (한 곳에서 관리 — /help, /start, 텔레그램 자동완성 메뉴가 공유)
COMMANDS = [
    ("new", "대화 초기화"),
    ("model", "모델 확인·변경 (fable / opus / sonnet / haiku)"),
    ("effort", "사고 강도 (low ~ max)"),
    ("status", "봇 상태 확인"),
    ("usage", "토큰 사용량 (구독 기준)"),
    ("session", "대화 세션 여러 개 관리·전환"),
    ("cd", "작업 폴더 전환"),
    ("ls", "현재 폴더 파일 목록"),
    ("files", "수정한 파일 첨부 전송 on/off"),
    ("export", "현재 세션 대화 내보내기(.md)"),
    ("remind", "리마인더 예약 (예: /remind 10m 커피)"),
    ("help", "명령어 도움말"),
]


def commands_text() -> str:
    return "\n".join(f"/{name} — {desc}" for name, desc in COMMANDS)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "안녕하세요! 메시지를 보내면 Claude가 답해드려요.\n"
        "사진·파일을 보내면 분석하고, 코딩도 실제로 해드려요.\n\n"
        f"{commands_text()}\n"
        f"\n당신의 유저 ID: {update.effective_user.id}"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "📖 명령어\n"
        f"{commands_text()}\n\n"
        "그 외엔 그냥 메시지를 보내면 Claude가 답하고, "
        "사진·파일을 보내면 분석해요. 코딩 모드에선 파일 작성·명령 실행도 합니다."
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    key = str(update.effective_chat.id)
    sessions.pop(key, None)
    save_sessions(sessions)
    # 현재 활성 스레드의 대화만 초기화 (다른 스레드는 유지)
    entry = chat_threads.get(key)
    if entry and entry.get("names"):
        entry["names"][entry.get("active") or DEFAULT_THREAD] = None
        save_threads(chat_threads)
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
            "바꾸기: /model opus  ·  /model sonnet  ·  /model haiku  ·  /model fable\n"
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


# 사고 강도 단계와 설명
EFFORT_CHOICES = {
    "low": "빠름·적은 토큰. 간단한 질문·잡담",
    "medium": "균형. 보통 작업",
    "high": "깊게 생각. 복잡한 코딩·추론 (기본)",
    "xhigh": "더 깊게. 어려운 작업",
    "max": "최대. 가장 어려운 문제 (느림·토큰 많음)",
}


async def cmd_effort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    arg = " ".join(context.args).strip().lower() if context.args else ""
    if not arg:
        current = chat_efforts.get(chat_id, DEFAULT_EFFORT) or "기본(high)"
        menu = "\n".join(f"• {name} — {desc}" for name, desc in EFFORT_CHOICES.items())
        await update.message.reply_text(
            f"현재 사고 강도: {current}\n\n"
            f"단계:\n{menu}\n\n"
            "바꾸기: /effort low  ·  /effort high  ·  /effort max\n"
            "기본값으로: /effort default"
        )
        return
    if arg in ("default", "기본", "reset"):
        chat_efforts.pop(chat_id, None)
        await update.message.reply_text("사고 강도를 기본값으로 되돌렸어요.")
    elif arg in EFFORT_CHOICES:
        chat_efforts[chat_id] = arg
        await update.message.reply_text(
            f"사고 강도를 '{arg}'(으)로 설정했어요 — {EFFORT_CHOICES[arg]}. (다음 메시지부터 적용)"
        )
    else:
        await update.message.reply_text(
            f"'{arg}'는 없는 단계예요. low / medium / high / xhigh / max 중에 골라주세요."
        )


async def transcribe_voice(update: Update) -> str | None:
    """음성/오디오 메시지를 Groq Whisper로 텍스트 변환. 없거나 실패하면 None."""
    msg = update.message
    media = msg.voice or msg.audio
    if not media:
        return None
    if not GROQ_API_KEY:
        await msg.reply_text(
            "🎤 음성을 받았지만 변환 키(GROQ_API_KEY)가 없어요.\n"
            "console.groq.com 에서 무료 키를 발급해 서버에 넣어주세요."
        )
        return None

    tg_file = await media.get_file()
    audio_path = UPLOADS_DIR / f"voice_{media.file_unique_id}.ogg"
    await tg_file.download_to_drive(custom_path=str(audio_path))

    import httpx
    try:
        with open(audio_path, "rb") as f:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    GROQ_STT_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    files={"file": (audio_path.name, f, "audio/ogg")},
                    data={"model": "whisper-large-v3-turbo", "language": "ko"},
                )
        if resp.status_code != 200:
            log.error("Groq 변환 실패 %s: %s", resp.status_code, resp.text[:200])
            await msg.reply_text(f"🎤 음성 변환에 실패했어요. ({resp.status_code})")
            return None
        text = resp.json().get("text", "").strip()
        return text or None
    except Exception as e:
        log.error("Groq 변환 예외: %s", e)
        await msg.reply_text("🎤 음성 변환 중 오류가 났어요.")
        return None
    finally:
        try:
            audio_path.unlink()
        except OSError:
            pass


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

    prune_uploads()  # 오래된 첨부 정리(디스크 무한 증가 방지)
    dest = UPLOADS_DIR / filename
    await tg_file.download_to_drive(custom_path=str(dest))
    log.info("첨부 저장: %s", dest)
    return str(dest)


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """다중세션: 한 채팅 안에서 이름 붙인 대화 스레드를 만들고 전환한다."""
    if not is_allowed(update):
        return
    key = str(update.effective_chat.id)
    entry = chat_threads.setdefault(key, {"active": DEFAULT_THREAD, "names": {}})
    # 초기 상태(스레드 기록 없음)면 진행 중 세션을 '기본' 스레드로 흡수
    if (entry.get("active") or DEFAULT_THREAD) not in entry["names"]:
        entry["names"][entry.get("active") or DEFAULT_THREAD] = sessions.get(key)
    args = context.args or []
    sub = args[0].lower() if args else "list"
    name = " ".join(args[1:]).strip()

    if sub in ("list", "ls", "목록"):
        await update.message.reply_text(
            "🧵 대화 세션\n" + format_session_lines(entry) +
            "\n\n새로: /session new 이름 · 전환: /session switch 이름 · 삭제: /session delete 이름"
        )
        return
    if sub in ("new", "create", "새로"):
        if not name:
            await update.message.reply_text("세션 이름을 적어줘. 예: /session new 결제")
            return
        entry["names"].setdefault(name, None)
        entry["active"] = name
        sessions.pop(key, None)  # 첫 메시지에서 새 세션 생성
        save_sessions(sessions)
        save_threads(chat_threads)
        await update.message.reply_text(f"🧵 '{name}' 세션을 만들고 전환했어요. 다음 메시지부터 새 맥락이에요.")
        return
    if sub in ("switch", "use", "전환"):
        if name not in entry["names"]:
            await update.message.reply_text(f"'{name}' 세션이 없어요. /session list 로 확인해줘.")
            return
        entry["active"] = name
        sid = entry["names"].get(name)
        if sid:
            sessions[key] = sid
        else:
            sessions.pop(key, None)
        save_sessions(sessions)
        save_threads(chat_threads)
        await update.message.reply_text(f"🧵 '{name}' 세션으로 전환했어요.")
        return
    if sub in ("delete", "del", "rm", "삭제"):
        if name not in entry["names"]:
            await update.message.reply_text(f"'{name}' 세션이 없어요.")
            return
        entry["names"].pop(name, None)
        if (entry.get("active") or DEFAULT_THREAD) == name:  # 활성 스레드를 지웠으면 다른 스레드로
            entry["active"] = next(iter(entry["names"]), DEFAULT_THREAD)
            sid = entry["names"].get(entry["active"])
            if sid:
                sessions[key] = sid
            else:
                sessions.pop(key, None)
        save_sessions(sessions)
        save_threads(chat_threads)
        await update.message.reply_text(f"🧵 '{name}' 세션을 삭제했어요. 현재: '{entry.get('active')}'")
        return
    await update.message.reply_text("사용법: /session [list | new 이름 | switch 이름 | delete 이름]")


# --- 구독 한도 조회 (비공식 /api/oauth/usage — 실패해도 봇은 정상 동작) ---
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_LIMIT_TTL = 120  # 초 — 이 엔드포인트가 429가 잦아 캐시해서 가끔만 부른다
_limit_cache: dict = {"at": 0.0, "data": None, "error": None}


def _oauth_token() -> str | None:
    """구독 한도 조회용 토큰. 인터랙티브 `claude login`의 access token(user:profile scope 보유)을
    우선 쓰고, 없으면 setup-token(env)으로 폴백한다. setup-token은 scope가 부족해 403이 날 수 있다."""
    try:
        d = json.loads((Path.home() / ".claude" / ".credentials.json").read_text())
        tok = (d.get("claudeAiOauth") or {}).get("accessToken")
        if tok:
            return tok
    except Exception:
        pass
    return (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip() or None


def fetch_subscription_limits() -> dict:
    """비공식 /api/oauth/usage 호출 → 5시간·주간 사용률 JSON. (블로킹 — to_thread로 부른다)
    User-Agent 헤더 필수(없으면 429 폭탄). 실패는 호출부에서 처리."""
    tok = _oauth_token()
    if not tok:
        raise RuntimeError("no-token")
    req = urllib.request.Request(OAUTH_USAGE_URL, headers={
        "Authorization": f"Bearer {tok}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/2.1.212",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _fmt_reset(iso: str) -> str:
    """ISO8601(UTC) 리셋 시각 → 'MM/DD HH:MM KST'."""
    try:
        dt = datetime.fromisoformat(iso).astimezone(timezone(timedelta(hours=9)))
        return dt.strftime("%m/%d %H:%M") + " KST"
    except Exception:
        return (iso or "?")[:16]


def format_limit_line(label: str, window: dict | None) -> str | None:
    """한 창(5시간/주간)의 남은 여유를 한 줄로. 데이터 없으면 None. (테스트용 순수 함수)"""
    if not window or window.get("utilization") is None:
        return None
    used = float(window["utilization"])
    remaining = max(0.0, 100.0 - used)
    return f"- {label}: {remaining:.0f}% 남음 (사용 {used:.0f}% · 리셋 {_fmt_reset(window.get('resets_at', ''))})"


async def subscription_limits_text() -> str:
    """구독 한도를 사람이 볼 문자열로. 캐시 + 실패 시 사유만 안내(봇은 안 죽음)."""
    now = time.time()
    if now - _limit_cache["at"] > USAGE_LIMIT_TTL:
        _limit_cache["at"] = now  # 성공/실패 무관하게 백오프 (429 방지)
        try:
            _limit_cache["data"] = await asyncio.to_thread(fetch_subscription_limits)
            _limit_cache["error"] = None
        except urllib.error.HTTPError as e:
            _limit_cache["data"] = None
            if e.code == 403:
                _limit_cache["error"] = "권한 부족 — 서버에서 'claude login' 필요 (setup-token엔 user:profile scope 없음)"
            elif e.code == 429:
                _limit_cache["error"] = "요청 과다(429) — 잠시 후 다시"
            else:
                _limit_cache["error"] = f"HTTP {e.code}"
        except Exception as e:
            _limit_cache["data"] = None
            _limit_cache["error"] = "토큰 없음" if str(e) == "no-token" else type(e).__name__
    data = _limit_cache["data"]
    if not data:
        return f"구독 한도: 조회 실패 ({_limit_cache['error'] or '알 수 없음'})"
    parts = []
    for label, key in (("5시간", "five_hour"), ("주간(전체)", "seven_day"), ("주간(Sonnet)", "seven_day_sonnet")):
        line = format_limit_line(label, data.get(key))
        if line:
            parts.append(line)
    return "구독 한도 (남은 여유)\n" + ("\n".join(parts) if parts else "(데이터 없음)")


async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    limits = await subscription_limits_text()
    acc = usage_totals.get(update.effective_chat.id)
    if acc:
        body = (
            "📊 토큰 사용량 (봇 시작 이후 · 이 채팅)\n"
            f"- 요청: {acc.get('turns', 0)}회\n"
            f"- 입력 토큰: {acc.get('input', 0):,}\n"
            f"- 출력 토큰: {acc.get('output', 0):,}\n"
            f"- 캐시 읽기 토큰: {acc.get('cache_read', 0):,}\n"
            f"- API 환산가(참고): ${acc.get('cost', 0.0):.4f}"
        )
    else:
        body = "📊 토큰 사용량 — 이 채팅은 아직 주고받은 기록이 없어요."
    await update.message.reply_text(
        f"🟢 {limits}\n\n"
        f"{body}\n\n"
        "※ 환산가는 구독이라 실제 청구가 아니라 'API로 썼다면' 참고치예요.\n"
        "※ 구독 한도는 비공식 경로라 부정확·지연될 수 있어요."
    )


def _project_dir_for(workdir: Path) -> Path:
    """Claude Code는 cwd 절대경로의 '/'를 '-'로 바꿔 트랜스크립트 프로젝트 폴더명으로 쓴다."""
    return Path.home() / ".claude" / "projects" / str(workdir.resolve()).replace("/", "-")


def _blocks_text(content) -> str:
    """트랜스크립트 메시지 content(문자열 또는 블록 리스트)에서 텍스트만 뽑는다. (테스트용 순수 함수)"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def build_transcript_md(session_id: str, workdir: Path) -> str | None:
    """claude 트랜스크립트(jsonl)에서 user/assistant 텍스트만 뽑아 마크다운으로. 없으면 None."""
    f = _project_dir_for(workdir) / f"{session_id}.jsonl"
    if not f.exists():
        return None
    out = []
    for raw in f.read_text(errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if ev.get("type") not in ("user", "assistant"):
            continue
        text = _blocks_text((ev.get("message") or {}).get("content")).strip()
        if not text:
            continue
        who = "## 🧑 나" if ev["type"] == "user" else "## 🤖 Claude"
        out.append(f"{who}\n\n{text}\n")
    return ("# 대화 내보내기\n\n" + "\n".join(out)) if out else None


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    sid = sessions.get(str(chat_id))
    if not sid:
        await update.message.reply_text("내보낼 대화가 없어요. (이 세션에서 아직 주고받은 게 없음)")
        return
    workdir = chat_workdirs.get(chat_id, WORKDIR)
    md = await asyncio.to_thread(build_transcript_md, sid, workdir)
    if not md:
        await update.message.reply_text("대화 기록 파일을 찾지 못했어요.")
        return
    bio = BytesIO(md.encode("utf-8"))
    bio.name = f"대화_{update.message.message_id}.md"
    await update.message.reply_document(document=bio, caption="현재 세션 대화 내보내기")


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    args = context.args or []
    mine = [r for r in reminders["items"] if r["chat_id"] == chat_id]

    if not args or args[0].lower() in ("list", "목록"):
        if not mine:
            await update.message.reply_text("예약된 리마인더가 없어요.\n예: /remind 10m 커피 마시기")
            return
        now = time.time()
        lines = [f"- id {r['id']}: {r['text']} ({fmt_uptime(max(0, r['due'] - now))} 남음)" for r in mine]
        await update.message.reply_text(
            "⏰ 예약된 리마인더\n" + "\n".join(lines) + "\n\n취소: /remind cancel <id>"
        )
        return

    if args[0].lower() in ("cancel", "취소") and len(args) >= 2 and args[1].isdigit():
        rid = int(args[1])
        before = len(reminders["items"])
        reminders["items"] = [
            r for r in reminders["items"] if not (r["id"] == rid and r["chat_id"] == chat_id)
        ]
        save_reminders()
        await update.message.reply_text(
            f"id {rid} 리마인더를 취소했어요." if len(reminders["items"]) < before else f"id {rid} 리마인더가 없어요."
        )
        return

    secs = parse_duration_seconds(args[0])
    if secs is None:
        await update.message.reply_text(
            "시간 형식: 30s · 10m · 2h · 1d · 숫자(분)\n예: /remind 10m 커피 마시기"
        )
        return
    text = " ".join(args[1:]).strip() or "(알림)"
    reminders["seq"] += 1
    item = {"id": reminders["seq"], "chat_id": chat_id, "due": time.time() + secs, "text": text}
    reminders["items"].append(item)
    save_reminders()
    schedule_reminder(context.application, item)
    await update.message.reply_text(f"⏰ {fmt_uptime(secs)} 뒤 알림 예약: {text} (id {item['id']})")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    model = chat_models.get(chat_id, DEFAULT_MODEL) or "기본(구독)"
    effort = chat_efforts.get(chat_id, DEFAULT_EFFORT) or "기본(high)"
    workdir = chat_workdirs.get(chat_id, WORKDIR)
    mode = "코딩 모드" if CLAUDE_PERMISSION_MODE else "대화 전용"
    busy = "작업 중" if chat_id in running_procs else "대기 중"
    # 절대경로(서버 내부 구조)를 그대로 노출하지 않고 홈 기준 상대경로로 표시
    try:
        shown_dir = "~/" + str(workdir.relative_to(Path.home()))
    except ValueError:
        shown_dir = workdir.name  # 홈 밖이면 폴더 이름만
    await update.message.reply_text(
        f"🤖 봇 상태\n"
        f"- 가동시간: {fmt_uptime(time.time() - START_TIME)}\n"
        f"- 모델: {model}\n"
        f"- 사고 강도: {effort}\n"
        f"- 모드: {mode}\n"
        f"- 현재: {busy}\n"
        f"- 저장된 대화: {len(sessions)}개\n"
        f"- 작업 폴더: {shown_dir}"
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


async def send_answer_file(update: Update, text: str) -> None:
    """긴 답변을 .md 파일로도 첨부한다 (텍스트 조각과 별개로 저장·가독성용). 디스크 안 씀."""
    try:
        bio = BytesIO(text.encode("utf-8"))
        bio.name = f"답변_{update.message.message_id}.md"
        await update.message.reply_document(document=bio)
    except Exception:
        pass  # 첨부 실패해도 본문은 이미 전송됨


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
        # 음성 메시지면 먼저 텍스트로 변환
        if update.message.voice or update.message.audio:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            transcript = await transcribe_voice(update)
            if not transcript:
                return  # 변환 실패 시 안내는 transcribe_voice가 이미 보냄
            await update.message.reply_text(f"🎤 들은 내용: {transcript}")
            prompt = transcript

        attachment = await save_attachment(update, context)
        if attachment:
            # Claude가 읽을 수 있도록 저장 경로를 프롬프트에 포함
            note = f"[사용자가 파일을 첨부함: {attachment}]"
            prompt = f"{note}\n{prompt}" if prompt else f"{note}\n이 파일을 확인하고 설명해 주세요."
        elif not prompt:
            return  # 내용 없는 메시지는 무시

        # 텔레그램 '답장(reply)'으로 보냈으면, 답장 대상 메시지를 맥락으로 붙인다.
        # (봇의 이전 답변이든 내 메시지든 — 세션 히스토리 중 '어느 것에 대한 얘기인지' 짚어준다)
        replied = update.message.reply_to_message
        if replied:
            prompt = build_prompt_with_reply(prompt, replied.text or replied.caption)

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
        # 진행 메시지 최상단에 현재 모델·강도를 표시
        cur_model = chat_models.get(chat_id, DEFAULT_MODEL) or "기본(구독)"
        cur_effort = chat_efforts.get(chat_id, DEFAULT_EFFORT) or "high"
        header = f"현재 모델: {cur_model} · 강도 {cur_effort}\n\n"

        async def refresh(text: str) -> None:
            try:
                await context.bot.edit_message_text(
                    header + text, chat_id=chat_id, message_id=status_msg.message_id, reply_markup=stop_kb,
                )
            except Exception:
                pass

        await refresh("🤔 작업 시작…")
        work_start = asyncio.get_event_loop().time()
        last_activity = work_start  # 마지막으로 뭔가 일어난 시각 (침묵 감지용)

        async def on_progress(desc: str) -> None:
            nonlocal last_edit, last_activity
            actions.append(desc)
            last_activity = asyncio.get_event_loop().time()
            now = last_activity
            if now - last_edit < 1.5:  # 텔레그램 편집 제한 대비
                return
            last_edit = now
            await refresh("\n".join(actions[-6:]))
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        async def heartbeat() -> None:
            """진행 신호가 뜸한 동안에도 '살아있음'을 보여준다.
            타이핑 표시를 유지하고, 오래 조용하면 경과 시간을 알린다."""
            while True:
                await asyncio.sleep(9)  # 텔레그램 타이핑 표시는 ~5초라 그 전에 갱신
                try:
                    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                except Exception:
                    pass
                idle = asyncio.get_event_loop().time() - last_activity
                if idle > 20:  # 20초 넘게 조용하면 경과 시간 표시 (생각 중인 구간)
                    mins = int((asyncio.get_event_loop().time() - work_start) // 60)
                    tail = f" ({mins}분 경과)" if mins else ""
                    body = "\n".join(actions[-5:] + [f"🤔 생각 중…{tail}"]) if actions else f"🤔 생각 중…{tail}"
                    await refresh(body)

        touched: set[str] = set()
        hb = asyncio.create_task(heartbeat())
        try:
            reply = await run_claude(chat_id, prompt, on_progress=on_progress, touched_files=touched)
        finally:
            hb.cancel()

        # 진행 메시지 지우고 최종 답변 전송
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        except Exception:
            pass
        # 30초 넘게 걸린 긴 작업이 "성공"했을 때만 완료 표시를 붙인다.
        # 타임아웃·중단·실패 메시지(⏰🛑⚠️로 시작)엔 붙이지 않는다.
        elapsed = asyncio.get_event_loop().time() - work_start
        is_error = reply.startswith(("⏰", "🛑", "⚠️"))
        prefix = "✅ 완료!\n\n" if (elapsed > 30 and not is_error) else ""
        chunks = split_message(prefix + reply)
        for chunk in chunks:
            await update.message.reply_text(chunk)

        # 긴 답변은 .md 파일로도 첨부 (저장·가독성)
        if not is_error and len(reply) > LONG_ANSWER_LIMIT:
            await send_answer_file(update, reply)

        # 수정·생성한 파일을 첨부로 보낸다 (끄려면 /files off)
        if touched and not is_error and send_files_on.get(chat_id, True):
            await send_touched_files(update, context, touched)


async def send_touched_files(update: Update, context: ContextTypes.DEFAULT_TYPE, paths: set[str]) -> None:
    """작업 중 만들거나 고친 파일을 첨부로 보낸다. 작업 폴더 안, 크기·개수 제한."""
    chat_id = update.effective_chat.id
    base = chat_workdirs.get(chat_id, WORKDIR).resolve()
    sent = 0
    skipped = 0
    for p in sorted(paths):
        if sent >= MAX_FILES_SEND:
            skipped += 1
            continue
        try:
            fp = Path(p).resolve()
            # 작업 폴더 밖 파일은 보내지 않는다 (안전)
            if not fp.is_relative_to(base) or not fp.is_file():
                continue
            if fp.stat().st_size > MAX_FILE_BYTES:
                await update.message.reply_text(f"📎 {fp.name} 은 너무 커서 첨부를 건너뛰었어요.")
                continue
            with open(fp, "rb") as f:
                await context.bot.send_document(chat_id=chat_id, document=f, filename=fp.name)
            sent += 1
        except Exception as e:
            log.warning("파일 첨부 실패 %s: %s", p, e)
    if skipped:
        await update.message.reply_text(f"📎 파일이 많아 {sent}개만 보냈어요. (나머지 {skipped}개 생략)")


async def cmd_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    arg = " ".join(context.args).strip().lower() if context.args else ""
    if arg in ("on", "켜", "켜기"):
        send_files_on[chat_id] = True
        await update.message.reply_text("📎 수정한 파일을 첨부로 보냅니다.")
    elif arg in ("off", "꺼", "끄기"):
        send_files_on[chat_id] = False
        await update.message.reply_text("📎 파일 첨부를 끕니다.")
    else:
        state = "켜짐" if send_files_on.get(chat_id, True) else "꺼짐"
        await update.message.reply_text(f"📎 파일 첨부: {state}\n바꾸기: /files on  또는  /files off")


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
            kill_process_group(proc)  # 자식·손자까지 그룹째 죽여 즉시 멈춘다
            await query.answer("중단했어요.")
        else:
            await query.answer("실행 중인 작업이 없어요.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """처리 중 예외가 나도 봇이 멈추지 않게 로그를 남기고 사용자에게 알린다."""
    log.error("처리 중 예외: %s", context.error, exc_info=context.error)
    # 실행 중 잠금이 걸려 있으면 사용자 쪽엔 다른 메시지가 나갔을 수 있으니, 조용히 로그만 남기고
    # 채팅이 특정되면 짧게 알린다.
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ 처리 중 문제가 생겼어요. 다시 시도해 주세요. (문제가 계속되면 /new 로 초기화)",
            )
        except Exception:
            pass


def build_application(token: str) -> Application:
    """텔레그램 Application을 만들고 모든 핸들러를 등록해 반환한다.
    (main에서 분리 — 배선을 오프라인 스모크 테스트로 검증하기 위함)"""

    # 텔레그램 "/" 자동완성 메뉴 등록 + 시작 알림
    async def post_init(application: Application) -> None:
        await application.bot.set_my_commands([(name, desc) for name, desc in COMMANDS])
        prune_uploads()  # 시작 시 오래된 첨부 정리
        # 예약된 리마인더 복원 — 지난 건 지금 발송, 남은 건 다시 스케줄 (재시작에도 안 날아가게)
        now = time.time()
        for item in list(reminders["items"]):
            if item["due"] <= now:
                try:
                    await application.bot.send_message(
                        chat_id=item["chat_id"], text=f"⏰ (지난) 알림: {item['text']}"
                    )
                except Exception:
                    pass
                reminders["items"] = [r for r in reminders["items"] if r["id"] != item["id"]]
            else:
                schedule_reminder(application, item)
        save_reminders()
        # 봇이 시작·재시작되면 허용된 사용자에게 알린다. 예상치 못한 알림이 오면 문제 신호.
        if os.environ.get("STARTUP_NOTIFY", "1") != "0":
            for uid in ALLOWED_IDS:
                try:
                    await application.bot.send_message(chat_id=uid, text="✅ 봇이 시작됐어요.")
                except Exception:
                    pass

    # concurrent_updates=True: 긴 작업이 도는 중에도 중단 버튼 콜백을 즉시 처리한다.
    # (기본값 False면 업데이트를 하나씩 처리해서, 작업이 끝나야 중단 버튼이 먹힌다.)
    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("effort", cmd_effort))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("ls", cmd_ls))
    app.add_handler(CommandHandler("files", cmd_files))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CallbackQueryHandler(on_button))
    # 텍스트 + 사진 + 문서 + 음성/오디오 모두 처리
    app.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.Document.ALL
        | filters.VOICE | filters.AUDIO,
        on_message,
    ))

    return app


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN 환경변수를 설정해 주세요. (@BotFather 에서 발급)")

    # Python 3.12+ 에서는 메인 스레드에 이벤트 루프가 자동 생성되지 않으므로 직접 만든다
    asyncio.set_event_loop(asyncio.new_event_loop())

    # 시작 시엔 실행 중인 작업이 없으므로 남아 있던 작업 표시를 지운다 (강제 종료 대비)
    running_procs.clear()
    _sync_busy_marker()

    app = build_application(token)
    log.info("봇 시작! (작업 폴더: %s)", WORKDIR)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
