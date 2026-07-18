#!/usr/bin/env bash
# PreToolUse 훅: Claude가 Bash 명령을 실행하기 직전에 검사한다.
# 위험한 패턴이면 거부(exit 2)해서 코딩 모드(bypassPermissions)에서도 실행을 막는다.
# 입력: stdin 으로 tool 호출 JSON ({"tool_input":{"command":"..."}})
# 출력: 허용이면 그냥 종료(0), 거부면 사유를 stderr 로 내고 exit 2.

payload=$(cat)

# jq 없이도 동작하도록 python 으로 command 추출 (서버에 python3.12 있음)
cmd=$(printf '%s' "$payload" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get("tool_input", {}).get("command", ""))
except Exception:
    print("")
' 2>/dev/null)

# 검사할 위험 패턴 (정규식). 실수 한 방으로 되돌릴 수 없는 것들.
deny() { echo "🛡️ 안전장치가 이 명령을 막았어요: $1" >&2; exit 2; }

# rm -rf 로 홈/루트/상위경로를 지우는 시도
if printf '%s' "$cmd" | grep -Eq 'rm[[:space:]]+(-[a-zA-Z]*f[a-zA-Z]*[[:space:]]+)?(-[a-zA-Z]+[[:space:]]+)*(~|/|\$HOME)([[:space:]]|/|$)'; then
    deny "홈/루트 디렉터리 삭제"
fi
if printf '%s' "$cmd" | grep -Eq 'rm[[:space:]]+-[a-zA-Z]*r[a-zA-Z]*f|rm[[:space:]]+-[a-zA-Z]*f[a-zA-Z]*r'; then
    # rm -rf 자체는 흔하니, 위험 대상만 추가 차단
    if printf '%s' "$cmd" | grep -Eq '(~|/etc|/usr|/var|/opt|/home|/boot|/\*|\$HOME)'; then
        deny "시스템/홈 경로를 rm -rf"
    fi
fi
# 디스크 통째로 밀기, fork bomb, 권한 무력화 등
printf '%s' "$cmd" | grep -Eq 'mkfs|dd[[:space:]]+if=.*of=/dev/|:\(\)\{.*\};:|chmod[[:space:]]+-R[[:space:]]+777[[:space:]]+/' && deny "디스크/시스템 파괴 명령"
# 시스템 종료/재부팅
printf '%s' "$cmd" | grep -Eq '\b(shutdown|reboot|halt|poweroff)\b' && deny "서버 종료/재부팅"

exit 0
