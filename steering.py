"""작업 중 도착한 텔레그램 메시지의 실시간 스티어링 큐.

봇이 어떤 채팅의 claude 턴을 돌리는 동안 그 채팅에 새 메시지가 오면
큐 파일(workspace/.steer/<chat_id>.json)에 쌓인다. 진행 중인 claude에는
Claude Code 훅(hooks/steer_hook.py)이 도구 호출 사이(PostToolUse)·턴 종료
직전(Stop)에 이 큐를 읽어 컨텍스트로 끼워 넣고(pending → injected), 훅이
소비하지 못한 메시지는 턴이 끝난 뒤 봇이 다음 턴으로 처리한다(폴백).
봇 프로세스와 훅 프로세스가 같은 파일을 만지므로 모든 접근은 flock으로
직렬화한다.

파일 형식: [{"id": 메시지id, "text": 프롬프트, "ts": epoch,
            "status": "pending"|"injected"}]
빈 큐는 파일을 지우지 않고 0바이트로 남긴다 — 훅 쪽 셸 가드([ -s ])가
파이썬을 띄우지 않게 하는 빠른 경로이자, unlink 시 동시 enqueue가 새
inode에 쓴 항목이 유실되는 경쟁을 피하기 위함이다.

이 모듈은 훅이 시스템 python3(3.9)로 임포트할 수도 있어 3.9 호환을 지킨다.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

STEER_DIRNAME = ".steer"
# settings.json 안에서 우리 훅 항목을 알아보는 표식 (갱신 시 이 항목만 교체)
STEER_HOOK_MARK = "steer_hook.py"


def steer_file(workdir: Path, chat_id: int) -> Path:
    return workdir / STEER_DIRNAME / f"{chat_id}.json"


def _locked_mutate(path: Path, mutate):
    """flock 아래에서 entries를 읽고 mutate(entries) -> (new_entries, result) 적용."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        raw = f.read()
        try:
            entries = json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError:
            entries = []
        if not isinstance(entries, list):
            entries = []
        new_entries, result = mutate(entries)
        f.seek(0)
        f.truncate()
        if new_entries:
            f.write(json.dumps(new_entries, ensure_ascii=False))
        f.flush()
    return result


def enqueue(path: Path, entry_id: int, text: str) -> None:
    def _mut(entries):
        entries.append({"id": entry_id, "text": text, "ts": time.time(), "status": "pending"})
        return entries, None

    _locked_mutate(path, _mut)


def take_pending(path: Path) -> list:
    """훅용: pending 항목 텍스트를 도착 순서로 모아 injected로 표시하고 반환."""

    def _mut(entries):
        texts = [e.get("text", "") for e in entries if e.get("status") == "pending"]
        for e in entries:
            if e.get("status") == "pending":
                e["status"] = "injected"
        return entries, [t for t in texts if t]

    return _locked_mutate(path, _mut)


def claim(path: Path, entry_id: int):
    """폴백용(봇, 앞 턴이 끝나 락을 잡은 직후): 내 메시지의 운명을 확인한다.
    - ("injected", None): 훅이 진행 중이던 턴에 이미 끼워 넣음 → 새 턴 불필요
    - ("run", 합친 프롬프트): 아직 pending → 지금 pending 전부를 합쳐 한 턴으로
    - ("gone", None): 앞선 대기자가 이미 자기 턴에 합쳐 감 → 새 턴 불필요
    """

    def _mut(entries):
        mine = None
        for e in entries:
            if e.get("id") == entry_id:
                mine = e
                break
        if mine is None:
            return entries, ("gone", None)
        if mine.get("status") == "injected":
            remaining = [e for e in entries if e.get("id") != entry_id]
            return remaining, ("injected", None)
        texts = [e.get("text", "") for e in entries if e.get("status") == "pending"]
        remaining = [e for e in entries if e.get("status") != "pending"]
        return remaining, ("run", "\n\n".join(t for t in texts if t))

    return _locked_mutate(path, _mut)


def drain_leftovers(workdir: Path) -> list:
    """시작 시: 재시작으로 처리 못 한 큐를 비우고 [(chat_id, [텍스트…]), …] 반환."""
    out = []
    d = workdir / STEER_DIRNAME
    if not d.is_dir():
        return out

    def _mut(entries):
        return [], [e.get("text", "") for e in entries]

    for p in sorted(d.glob("*.json")):
        if not p.stem.lstrip("-").isdigit():  # 그룹 채팅 id는 음수
            continue
        texts = [t for t in _locked_mutate(p, _mut) if t]
        if texts:
            out.append((int(p.stem), texts))
    return out


def format_injected_context(texts: list) -> str:
    """PostToolUse 주입용: 하던 작업을 유지하면서 새 메시지를 반영하라는 지시문."""
    lines = "\n".join(f"- {t}" for t in texts)
    return (
        "[텔레그램 실시간 메시지] 작업 중에 사용자가 새 메시지를 보냈습니다:\n"
        f"{lines}\n"
        "명시적인 중단·방향 전환 지시가 아니라면 지금 하던 작업을 버리지 마세요. "
        "관련된 지시면 진행 방향에 반영하고, 별개 요청이면 하던 작업과 함께 처리한 뒤 "
        "최종 답변에 이 메시지에 대한 응답도 포함하세요."
    )


def format_stop_reason(texts: list) -> str:
    """Stop 차단용: 턴을 끝내기 전에 방금 온 메시지까지 처리하라는 지시문."""
    lines = "\n".join(f"- {t}" for t in texts)
    return (
        "턴을 끝내기 전에, 방금 텔레그램으로 도착한 사용자 메시지를 마저 처리하세요:\n"
        f"{lines}\n"
        "지금까지의 작업 결과와 이 메시지에 대한 답을 합쳐 최종 답변을 완성하세요."
    )


def ensure_steer_hooks(settings_path: Path, hook_script: Path, python_bin: str = "python3") -> bool:
    """workspace/.claude/settings.json에 스티어링 훅(PostToolUse·Stop)을 병합한다.

    기존 훅(위험 명령 차단 등)은 그대로 두고 steer_hook.py 항목만 갱신한다.
    셸 가드([ -s ])로 큐가 비어 있으면 파이썬을 아예 띄우지 않는다.
    설정 파일이 깨져 있으면 덮어쓰지 않고 False를 돌려준다(폴백 예약만 동작).
    """
    try:
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(settings, dict):
        return False

    hooks = settings.setdefault("hooks", {})

    def _entry(event_arg: str) -> dict:
        cmd = f'[ -s "$TG_STEER_FILE" ] && {python_bin} {hook_script} {event_arg} || true'
        e = {"hooks": [{"type": "command", "command": cmd, "timeout": 20}]}
        if event_arg == "post":
            e["matcher"] = "*"
        return e

    for event, arg in (("PostToolUse", "post"), ("Stop", "stop")):
        prev = hooks.get(event) or []
        kept = [x for x in prev if STEER_HOOK_MARK not in json.dumps(x)]
        kept.append(_entry(arg))
        hooks[event] = kept

    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = settings_path.with_name(settings_path.name + ".tmp")
        tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, settings_path)
    except OSError:
        return False
    return True
