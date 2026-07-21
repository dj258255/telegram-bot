# 텔레그램 Claude 봇

폰에서 Claude에게 일을 시키려고 만든 개인 봇입니다. 텔레그램으로 메시지를 보내면 서버에서 Claude를 돌리고 답을 돌려줍니다. API 키로 따로 과금하는 대신 이미 쓰던 Claude Code 구독 인증(`claude -p`)을 그대로 호출합니다.

```
텔레그램 메시지 -> bot.py -> claude (스트리밍) -> 진행 상황과 답변을 텔레그램으로
```

## 왜 이렇게 만들었나

세 가지를 정하고 시작했습니다.

과금은 구독으로 해결했습니다. Claude를 프로그램에서 부르는 길은 API 키로 토큰만큼 따로 내는 방법과 결제하고 있는 구독을 그대로 쓰는 방법이 있습니다. 매달 구독료를 내고 있으니 그 안에서 돌리고 싶었습니다. `claude -p`에 `claude setup-token`으로 발급한 토큰을 붙이면 서버에서도 구독 인증이 통합니다.

입력 수단은 텔레그램이 맞았습니다. 이 봇은 혼자 쓰는 개인 비서라 일대일 대화가 어울렸고 음성 메시지와 파일 첨부를 폰에서 그대로 넘길 수 있었습니다. 롱 폴링으로 도는 덕분에 서버에 포트를 열거나 공인 도메인을 붙일 필요도 없어서 방화벽 안쪽에 두고 돌립니다.

안전 장치도 처음부터 같이 넣었습니다. 코딩 모드는 확인 없이 명령을 실행하기 때문에 취소와 중단 버튼을 두고 위험한 명령은 실행 직전에 막습니다. 같은 서버의 다른 서비스는 봇이 건드리지 못하도록 격리했습니다.

## 기능

- 대화 맥락 유지. 채팅방별 세션(`--resume`)이라 봇을 재시작해도 이어집니다.
- 본인만 사용. `ALLOWED_USER_IDS`로 허용한 계정 외에는 응답하지 않습니다.
- 코딩 모드. 파일을 만들고 명령을 실행하며 그 과정을 텔레그램에 실시간으로 보여줍니다.
- 취소와 중단. 실행 전 취소 버튼과 실행 중 중단 버튼이 있습니다.
- 위험 명령 차단. `rm -rf ~`, 재부팅, 디스크 포맷 같은 명령은 실행 직전에 거부합니다.
- 음성 입력. 음성 메시지를 Groq Whisper로 텍스트로 바꿔 넘깁니다.
- 사진과 파일. 첨부를 저장한 뒤 Claude가 읽고 분석합니다.
- 웹 검색과 MCP. 웹 검색은 기본으로 되고 context7 문서 조회가 붙어 있습니다.
- 모델과 강도 선택. `/model`로 모델을, `/effort`로 사고 강도를 바꿉니다.
- 답장으로 맥락 잇기. 특정 메시지에 답장(reply)하면 그 내용을 맥락으로 이어갑니다.
- 다중 세션. `/session`으로 한 채팅에서 독립된 대화 여러 개를 만들어 오갑니다.
- 긴 답변은 자동으로 `.md` 파일로도 첨부하고, 토큰 사용량은 `/usage`로 봅니다.
- 서버 격리. systemd `InaccessiblePaths`로 지정한 경로는 봇이 접근하지 못합니다.

## 명령어

| 명령 | 동작 |
|---|---|
| `/start` | 인사와 내 유저 ID 표시 |
| `/help` | 명령어 목록 |
| `/new` | 현재 세션의 대화 초기화 |
| `/model [opus\|sonnet\|haiku\|fable]` | 모델 확인과 변경 |
| `/effort [low ~ max]` | 사고 강도 확인과 변경 |
| `/session [list\|new\|switch\|delete]` | 대화 세션 여러 개 관리·전환 |
| `/usage` | 토큰 사용량·구독 한도 확인 |
| `/status` | 봇 상태 확인 |
| `/cd`, `/ls` | 작업 폴더 전환과 목록 |
| `/files [on\|off]` | 수정한 파일 첨부 전송 켜기/끄기 |
| `/export` | 현재 세션 대화를 `.md`로 내보내기 |
| 일반 메시지 | Claude에게 전달, 맥락 유지 |
| 메시지에 답장(reply) | 그 메시지를 맥락으로 이어서 처리 |
| 사진, 파일, 음성 | 첨부 분석 또는 음성 변환 후 처리 |

## 로컬 실행

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="봇파더_토큰"
export ALLOWED_USER_IDS="내_유저_ID"
.venv/bin/python bot.py
```

봇 토큰은 텔레그램의 @BotFather에게 `/newbot`으로 발급받습니다. `/start`를 보내면 봇이 내 유저 ID를 알려줍니다.

## 서버 배포

Rocky Linux 9 기준입니다. 서버를 옮겨도 아래 순서면 재현됩니다.

```bash
git clone <저장소> ~/telegram-bot
cd ~/telegram-bot

# 시스템 패키지, Claude CLI, venv, systemd, sudoers를 한 번에 준비
bash deploy/setup.sh

# 토큰 채우기
sudo nano /etc/claude-bot.env

# 토큰을 넣은 뒤 다시 실행하면 플러그인과 마켓플레이스까지 설치
bash deploy/setup.sh

sudo systemctl restart claude-bot && systemctl status claude-bot
```

`CLAUDE_CODE_OAUTH_TOKEN`은 로컬에서 `claude setup-token`으로 발급해 `/etc/claude-bot.env`에 넣습니다. 설치할 플러그인과 마켓플레이스 목록은 `deploy/plugins.txt`와 `deploy/marketplaces.txt`에 있어서 서버를 옮겨도 그대로 따라갑니다.

`main`에 push하면 깃허브 액션이 서버에 코드를 반영하고 서비스를 재시작합니다. 필요한 시크릿은 `OCI_HOST`, `OCI_USER`, `OCI_SSH_KEY`입니다.

## 설정

| 변수 | 설명 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 봇파더 토큰 (필수) |
| `ALLOWED_USER_IDS` | 허용 유저 ID. 비우면 전체 허용이 되고 코딩 모드는 자동으로 꺼집니다 |
| `CLAUDE_CODE_OAUTH_TOKEN` | 서버에서 구독 인증에 쓰는 토큰 |
| `CLAUDE_PERMISSION_MODE` | 기본 `bypassPermissions`(코딩 모드). `off`면 대화 전용 |
| `CLAUDE_MODEL` | 기본 모델. 비우면 구독 기본값 |
| `CLAUDE_EFFORT` | 기본 사고 강도 (low ~ max) |
| `CANCEL_DELAY` | 코딩 모드에서 실행 전 취소 대기 시간(초) |
| `GROQ_API_KEY` | 음성 변환용 키. 없으면 음성 기능이 꺼집니다 |

## 보안 구조

- 비밀값은 세 곳으로 나뉩니다. 저장소에는 없고, 깃허브 시크릿에는 접속 정보만, 서버 `/etc/claude-bot.env`에 실제 토큰이 권한 600으로 있습니다.
- 코딩 모드는 `ALLOWED_USER_IDS`가 설정됐을 때만 켜집니다. 값이 잘못되면 봇이 시작을 거부합니다.
- 같은 서버의 다른 서비스 데이터는 봇이 접근하지 못합니다. 높은 권한으로도 우회되지 않습니다.

## 한계

- 봇이 답하려면 서버가 켜져 있어야 합니다.
- 구독 사용량은 Claude Code와 한 통을 나눠 쓰므로 한도를 넘기면 봇도 멈춥니다.
- 지금은 개인 규모입니다. 쓰는 사람은 저 하나입니다.
