# 텔레그램 Claude 봇

텔레그램으로 메시지를 보내면 Claude(구독 CLI 인증)가 답하는 개인 봇.
API 키 과금 없이 Claude Code 구독 인증(`claude -p`)을 그대로 사용한다.

```
텔레그램 메시지 → bot.py → claude (스트리밍) → 진행 상황 + 답변을 텔레그램으로
```

## 기능

- **대화 맥락 유지** — 채팅방별 세션(`--resume`), 봇 재시작해도 이어짐
- **본인만 사용** — `ALLOWED_USER_IDS`로 잠금
- **코딩 모드** — 파일 생성·수정·명령 실행 (기본 켜짐). 작업 과정이 텔레그램에 실시간 표시됨
- **사진·파일 첨부** — 스크린샷/문서를 보내면 `uploads/`에 저장 후 Claude가 읽고 분석
- **웹검색** — 기본 내장 (별도 설정 불필요)
- **MCP 연동** — context7(문서조회) 기본 세팅, 봇에게 말하면 스스로 추가. `docs/MCP-SKILLS.md` 참고
- **Skill** — `workspace/.claude/skills/`에 반복 작업 지침 저장, 자동 로드
- **메모리** — "기억해줘" → `workspace/memory/`에 저장, `/new` 후에도 유지
- **모델 선택** — `/model opus` 등으로 전환
- **balruno 격리** — 코딩 모드여도 balruno는 못 건드림 (systemd `InaccessiblePaths`)

## 명령어

| 명령 | 동작 |
|---|---|
| `/start` | 인사 + 내 유저 ID 표시 |
| `/new` | 대화 세션 초기화 |
| `/model [opus\|sonnet\|default]` | 모델 확인·변경 |
| (일반 메시지) | Claude에게 전달, 대화 맥락 유지 |
| (사진·파일) | 첨부 저장 후 분석 |

## 로컬 실행 (맥)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="봇파더_토큰"
export ALLOWED_USER_IDS="내_유저_ID"
.venv/bin/python bot.py
```

## 서버 배포 (OCI Rocky Linux)

서버 주소·계정·키 실제 값은 `docs/DEPLOY-RECORD.local.md` 참고 (git 제외).
현재 운영: balruno 서버(SERVER_IP)에 systemd로 상시 가동.

### 최초 1회 셋업 — 서버 옮겨도 이 순서면 끝 (자동화됨)

```bash
# 1) 비공개 저장소 clone (서버에 deploy key 등록 후)
git clone git@github.com-telegram:dj258255/telegram-bot.git ~/telegram-bot
cd ~/telegram-bot

# 2) 원클릭 구축 — 시스템 패키지·Claude CLI·venv·systemd·sudoers 자동
bash deploy/setup.sh

# 3) 토큰 채우기
sudo nano /etc/claude-bot.env
#   TELEGRAM_BOT_TOKEN / ALLOWED_USER_IDS / CLAUDE_CODE_OAUTH_TOKEN / GROQ_API_KEY

# 4) 토큰 넣은 뒤 재실행하면 플러그인 52개 + 마켓플레이스까지 자동 설치
bash deploy/setup.sh

# 5) 시작
sudo systemctl restart claude-bot && systemctl status claude-bot
```

- `CLAUDE_CODE_OAUTH_TOKEN`은 **맥에서** `claude setup-token`(브라우저 로그인)으로 발급.
- 설치할 플러그인/마켓플레이스 목록은 `deploy/plugins.txt` / `deploy/marketplaces.txt` (git 관리 → 서버 옮겨도 동일 재현).
- `setup.sh`는 여러 번 실행해도 안전(idempotent).

### 코드 수정 후 배포 (이게 전부)

```bash
git add -A && git commit -m "변경 내용" && git push
# → GitHub Actions가 서버 git reset → pip install → systemd 재시작
```

GitHub Secrets: `OCI_HOST` / `OCI_USER` / `OCI_SSH_KEY` (discord-bot과 동일 키 재사용).

### 서버 확인

```bash
systemctl status claude-bot
journalctl -u claude-bot -f          # 실시간 로그
```

## 설정 (환경변수)

| 변수 | 설명 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 봇파더 토큰 (필수) |
| `ALLOWED_USER_IDS` | 허용 유저 ID (쉼표 구분). 비우면 전체 허용 + 코딩 모드 자동 해제 |
| `CLAUDE_CODE_OAUTH_TOKEN` | 서버 구독 인증용 (맥에선 불필요) |
| `CLAUDE_PERMISSION_MODE` | 기본 `bypassPermissions`(코딩 모드). `off`면 대화 전용 |
| `CLAUDE_MODEL` | 기본 모델. 비우면 구독 기본(Sonnet). `opus` 등 |
| `CLAUDE_TIMEOUT` | 응답 대기 한도, 기본 600초 |

## 보안 구조

- 비밀값 3분리: git엔 없음 / GitHub Secrets엔 접속정보만 / 서버 `/etc/claude-bot.env`엔 실제 토큰(600)
- 코딩 모드는 `ALLOWED_USER_IDS`가 있을 때만 켜짐 (없으면 자동 해제). 잘못된 ID 값이면 시작 거부
- balruno 데이터/설정은 봇에서 접근 불가 (sudo로도 우회 안 됨)

## 주의사항

- 구독 사용량(rate limit)은 Claude Code와 공유 — 한도 초과 시 봇도 응답 불가
- 토큰 유출 시: BotFather `/revoke` (텔레그램) / `claude setup-token` 재발급 (Claude) → `/etc/claude-bot.env` 갱신 → 재시작
- 봇은 맥이 아니라 **서버**에서 작업한다. "파일 만들어줘"의 결과물은 서버 `workspace/`에 생김
