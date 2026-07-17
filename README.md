# 텔레그램 Claude 봇

텔레그램으로 메시지를 보내면 Claude(구독 CLI 인증)가 답해주는 봇.
API 키 과금 없이 Claude Code 구독 인증(`claude -p`)을 그대로 사용한다.

```
텔레그램 메시지 → bot.py → claude -p 실행 → 응답을 텔레그램으로 답장
```

- 채팅방별 세션 유지 (`--resume`) — 대화 맥락이 이어짐
- `ALLOWED_USER_IDS` 로 본인만 사용 가능하게 제한
- `CLAUDE_PERMISSION_MODE=bypassPermissions` 로 코딩 모드(파일 수정/명령 실행) 활성화 가능
- 배포: discord-bot과 동일 패턴 — OCI VM + systemd + GitHub Actions 자동 배포

## 로컬 실행 (맥)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="봇파더_토큰"
export ALLOWED_USER_IDS="내_유저_ID"
.venv/bin/python bot.py
```

## OCI 서버 배포 (Rocky Linux, discord-bot과 같은 서버)

서버 주소·계정·키 경로 실제 값은 `docs/DEPLOY-RECORD.local.md` 참고 (git 제외).

### 최초 1회 셋업

```bash
# 서버 접속 후:
sudo dnf install -y python3 python3-pip git nodejs npm

# Claude CLI 설치
sudo npm install -g @anthropic-ai/claude-code

# 코드 받기
git clone https://github.com/<계정>/telegram-bot.git ~/telegram-bot
cd ~/telegram-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 환경변수 (.env — git 제외)
cp .env.example .env && nano .env
chmod 600 .env

# systemd 등록
sudo cp deploy/claude-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now claude-bot
systemctl status claude-bot
```

`CLAUDE_CODE_OAUTH_TOKEN`은 **맥에서** `claude setup-token` 실행(브라우저 로그인)으로 발급해 `.env`에 넣는다.

GitHub Actions가 재시작할 수 있도록 sudo 허용:

```bash
echo "rocky ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart claude-bot" | sudo tee /etc/sudoers.d/claude-bot
```

### 코드 수정 후 배포하기 (이게 전부)

```bash
git add -A && git commit -m "변경 내용" && git push
# → GitHub Actions가 알아서: 서버 git reset → pip install → systemd 재시작
```

GitHub Secrets는 discord-bot과 동일한 값 재사용: `OCI_HOST` / `OCI_USER` / `OCI_SSH_KEY`.

### 서버 직접 확인

```bash
systemctl status claude-bot
journalctl -u claude-bot -f          # 실시간 로그
sudo systemctl restart claude-bot
```

## 명령어

| 명령 | 동작 |
|---|---|
| `/start` | 인사 + 내 유저 ID 표시 |
| `/new` | 대화 세션 초기화 |
| (일반 메시지) | Claude에게 전달, 대화 맥락 유지 |

## 설정 (환경변수)

| 변수 | 설명 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 봇파더 토큰 (필수) |
| `ALLOWED_USER_IDS` | 허용 유저 ID (쉼표 구분). 비우면 전체 허용 — 비추천 |
| `CLAUDE_CODE_OAUTH_TOKEN` | 서버에서 구독 인증용 (맥에선 불필요) |
| `CLAUDE_PERMISSION_MODE` | `bypassPermissions` = 코딩 모드. 비우면 대화 전용 |
| `CLAUDE_TIMEOUT` | 응답 대기 한도, 기본 600초 |

## 주의사항

- 구독 사용량(rate limit)은 Claude Code와 공유 — 한도 초과 시 봇도 응답 불가
- 코딩 모드는 텔레그램 메시지로 서버 명령이 실행되는 것이므로 `ALLOWED_USER_IDS` 설정이 되어 있을 때만 켤 것
- 토큰 유출 시: BotFather `/revoke` (텔레그램) / `claude setup-token` 재발급 (Claude)
