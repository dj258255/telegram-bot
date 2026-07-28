#!/usr/bin/env python3
"""Claude Code 훅: 작업 중 도착한 텔레그램 메시지를 진행 중인 턴에 끼워 넣는다.

봇(run_claude)이 TG_STEER_FILE 환경변수로 이 채팅의 스티어링 큐 경로를 넘긴다.
- post (PostToolUse): 도구 호출이 끝날 때마다 pending 메시지를 additionalContext로 주입
- stop (Stop): 턴이 끝나려는 순간 pending이 남아 있으면 턴을 이어가게 막고 메시지 전달
큐가 없거나 비어 있으면 아무 출력 없이 종료한다 (settings.json의 [ -s ] 가드가 1차 방어).

주입 즉시 pending → injected 로 표시하므로 Stop 차단이 무한 반복될 수 없다
(두 번째 Stop에는 pending이 없어 그대로 종료된다).
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo 루트 (steering 임포트)
import steering  # noqa: E402


def main() -> None:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    raw = os.environ.get("TG_STEER_FILE", "")
    if event not in ("post", "stop") or not raw:
        return
    path = Path(raw)
    if not path.is_file():
        return
    texts = steering.take_pending(path)
    if not texts:
        return
    if event == "post":
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": steering.format_injected_context(texts),
            }
        }
    else:
        out = {"decision": "block", "reason": steering.format_stop_reason(texts)}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
