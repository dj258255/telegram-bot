#!/usr/bin/env bash
# 새 서버에 봇을 처음부터 구축하는 스크립트 (Rocky Linux 9 / RHEL 계열 기준).
# 서버를 옮겨도 이 스크립트 한 번이면 동일 환경이 재현된다.
#
# 사용법 (서버에서):
#   git clone <저장소> ~/telegram-bot   # 또는 deploy key로 clone
#   cd ~/telegram-bot
#   bash deploy/setup.sh
#   # 마지막에 안내되는 대로 /etc/claude-bot.env 에 토큰을 채우고 재시작
#
# 이미 구축된 서버에서 다시 실행해도 안전하다(idempotent).
set -euo pipefail

REPO_DIR="$HOME/telegram-bot"
ENV_FILE="/etc/claude-bot.env"
SERVICE=claude-bot

echo "==> 1/6 시스템 패키지 설치"
# PTB 22.x는 Python 3.10+ 필요 (Rocky 9 기본 3.9 불가). Claude CLI는 node 필요.
sudo dnf install -y -q python3.12 git nodejs npm >/dev/null 2>&1 || \
  sudo dnf install -y python3.12 git nodejs npm

echo "==> 2/6 Claude Code CLI 설치"
if ! command -v claude >/dev/null 2>&1; then
  sudo npm install -g @anthropic-ai/claude-code
fi
claude --version

echo "==> 3/6 파이썬 가상환경 + 의존성"
cd "$REPO_DIR"
[ -d .venv ] || python3.12 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "==> 4/6 플러그인 마켓플레이스 + 플러그인 설치"
# OAuth 토큰이 있어야 설치 가능. 없으면 이 단계는 건너뛰고 나중에 재실행.
if [ -f "$ENV_FILE" ] && sudo grep -q '^CLAUDE_CODE_OAUTH_TOKEN=sk-' "$ENV_FILE" 2>/dev/null; then
  export CLAUDE_CODE_OAUTH_TOKEN="$(sudo grep '^CLAUDE_CODE_OAUTH_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
  # 마켓플레이스 등록
  while read -r mkt; do
    [[ -z "$mkt" || "$mkt" == \#* ]] && continue
    claude plugin marketplace add "$mkt" >/dev/null 2>&1 || echo "  (마켓플레이스 추가 실패/기존: $mkt)"
  done < deploy/marketplaces.txt
  # 플러그인 설치
  ok=0; fail=0
  while read -r pl; do
    [[ -z "$pl" || "$pl" == \#* ]] && continue
    if claude plugin install "$pl" >/dev/null 2>&1; then ok=$((ok+1)); else echo "  설치실패: $pl"; fail=$((fail+1)); fi
  done < deploy/plugins.txt
  echo "  플러그인: 성공 $ok / 실패 $fail"
else
  echo "  ⏭  OAuth 토큰이 아직 없어 플러그인 설치를 건너뜀."
  echo "     $ENV_FILE 에 CLAUDE_CODE_OAUTH_TOKEN 을 넣은 뒤 이 스크립트를 다시 실행하세요."
fi

echo "==> 5/6 환경변수 파일 준비"
if [ ! -f "$ENV_FILE" ]; then
  sudo cp .env.example "$ENV_FILE"
  sudo chmod 600 "$ENV_FILE"
  sudo restorecon "$ENV_FILE" 2>/dev/null || true
  echo "  $ENV_FILE 생성됨 — 실제 토큰으로 채우세요 (sudo nano $ENV_FILE)"
else
  echo "  $ENV_FILE 이미 있음 (건너뜀)"
fi

echo "==> 6/6 systemd 서비스 + sudo 권한"
sudo cp deploy/$SERVICE.service /etc/systemd/system/
sudo systemctl daemon-reload
# GitHub Actions가 비번 없이 재시작할 수 있게
echo "$(whoami) ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart $SERVICE" | \
  sudo tee /etc/sudoers.d/$SERVICE >/dev/null
sudo systemctl enable "$SERVICE" >/dev/null 2>&1 || true

echo ""
echo "✅ 셋업 완료."
echo "   1) $ENV_FILE 에 토큰 확인 (TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, CLAUDE_CODE_OAUTH_TOKEN, GROQ_API_KEY)"
echo "   2) 토큰을 방금 넣었다면 플러그인 설치를 위해: bash deploy/setup.sh (재실행)"
echo "   3) 시작:  sudo systemctl restart $SERVICE && systemctl status $SERVICE"
