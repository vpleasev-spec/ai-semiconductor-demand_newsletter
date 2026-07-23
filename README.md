# AI Demand Monitoring

AI 수요, 플랫폼, 인프라 및 환경 신호를 수집하고 Claude로 요약해 Gmail로 발송하는 GitHub Actions 프로젝트입니다.

## 이번 버전의 핵심 변경

- `data` 폴더를 사용하지 않습니다.
- `history.json`은 첫 실행 때 GitHub Actions가 자동 생성합니다.
- 이후 이력은 GitHub Actions Cache로 복원·저장합니다.
- 저장소에 이력 파일을 자동 커밋하지 않습니다.

## 업로드 권장 방법

GitHub Desktop 또는 Git 명령어를 사용하면 숨김 폴더 `.github`까지 정확히 업로드됩니다.

GitHub 웹 업로드에서 `.github`가 빠지는 경우, 루트의 `github-workflow-newsletter.yml`을 열어 내용을 복사한 뒤 저장소에 아래 파일을 직접 만드세요.

`.github/workflows/newsletter.yml`

## 실행 일정

- 월요일 08:00 KST
- 목요일 08:00 KST
- Actions 탭에서 수동 실행 가능

## GitHub Secrets

`Settings` → `Secrets and variables` → `Actions`

- `ANTHROPIC_API_KEY`
- `SMTP_HOST` = `smtp.gmail.com`
- `SMTP_PORT` = `465`
- `SMTP_USERNAME`
- `SMTP_PASSWORD` = Google 앱 비밀번호
- `EMAIL_FROM`
- `EMAIL_TO`

## 파일 구조

```text
AI-Demand-Monitoring-Reviewed/
├── .github/workflows/newsletter.yml
├── github-workflow-newsletter.yml
├── news.py
├── config.yaml
├── requirements.txt
├── README.md
├── .env.example
└── .gitignore
```

`history.json`과 `data` 폴더는 ZIP에 포함되지 않습니다.
