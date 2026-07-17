# MCP 서버 & Skill 관리

봇(코딩 모드)은 텔레그램 메시지만으로 MCP 서버를 추가/제거할 수 있다.
예: "context7 연결해줘", "playwright MCP 추가해줘" → 봇이 아래 명령을 직접 실행.

## 현재 세팅된 MCP (키 불필요, 바로 작동)

`workspace/.mcp.json`에 정의됨 — git으로 서버에 자동 배포된다.

| 이름 | 용도 | 방식 |
|---|---|---|
| context7 | 라이브러리 최신 공식 문서 실시간 조회 | HTTP (키 불필요) |

## 추가하기 (봇 작업폴더에서 실행)

```bash
cd ~/telegram-bot/workspace   # 서버 기준

# HTTP 방식 (키 불필요)
claude mcp add --scope project --transport http context7 https://mcp.context7.com/mcp

# npx 방식 (키 불필요, 서버에 자동 설치됨)
claude mcp add --scope project playwright -- npx -y @playwright/mcp@latest
claude mcp add --scope project pinecone   -- npx -y @pinecone-database/mcp
claude mcp add --scope project firebase   -- npx -y firebase-tools@latest mcp
```

`--scope project`로 추가하면 `workspace/.mcp.json`에 기록되고, git push하면 서버에 반영된다.

## 인증이 필요한 MCP (토큰 있어야 작동)

이 맥에는 아래도 설치돼 있지만 전부 OAuth/API 키가 필요하다. 서버 봇에서 쓰려면
해당 서비스의 토큰을 발급해 환경변수나 헤더로 넣어야 한다. 필요할 때 하나씩 연결.

| 서비스 | 인증 방식 | 추가 예시 |
|---|---|---|
| Notion | OAuth | `claude mcp add --scope project --transport http notion https://mcp.notion.com/mcp` (첫 사용 시 브라우저 인증 필요 → 서버에선 토큰 방식으로) |
| Gmail / Calendar / Drive | Google OAuth | 데스크톱 앱 전용에 가까움. 서버에선 Google 서비스계정 토큰 필요 |
| Slack | OAuth | `--transport http slack https://mcp.slack.com/mcp` + 봇 토큰 |
| Supabase | API 키 | `--transport http supabase https://mcp.supabase.com/mcp` + 헤더 토큰 |
| Figma / Linear / Vercel / Sentry / GitLab / Stripe / Atlassian / Asana / PostHog | 각 서비스 OAuth·API 키 | 필요 시 해당 서비스 토큰으로 |

> 인증이 필요한 MCP는 토큰이 **서버 환경변수/헤더**로 들어가야 하므로, 토큰을 준비한 뒤
> `/etc/claude-bot.env` 또는 `.mcp.json`의 `headers`에 넣는다. (git에는 올리지 않는다)

## Skill (커스텀 작업 지침)

`workspace/.claude/skills/<이름>/SKILL.md` 형태로 두면 봇이 해당 작업 때 자동으로 불러 쓴다.
자주 시키는 반복 작업(예: "일일 리포트 형식", "코드리뷰 체크리스트")을 스킬로 만들어두면 편하다.

```
workspace/.claude/skills/
  daily-report/
    SKILL.md      # 리포트 작성 규칙
```

봇에게 "○○ 하는 스킬 만들어줘"라고 하면 이 구조로 직접 만들어준다.

## 확인 명령

```bash
cd ~/telegram-bot/workspace
claude mcp list        # 연결 상태 확인
```
