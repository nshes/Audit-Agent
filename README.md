# Audit Agent

버그 바운티 / 취약점 제보 파이프라인의 마지막 단계에서 앞선 에이전트들의 **원칙 준수 여부**와 **실행 절차**를 검증하는 감사(Audit) 에이전트입니다.

오케스트레이터가 **Git pull → Docker build** 방식으로 배포하며, 오케스트레이터 표준 `/invoke` 엔드포인트를 통해 연동됩니다.

---

## 배포 방식 (오케스트레이터 → Git pull + Docker build)

```bash
# 오케스트레이터 서버(Ubuntu 24.04 LTS)에서 실행되는 배포 순서

# 1. 레포지토리 클론 또는 업데이트
git clone https://github.com/upstageknu/Audit-Agent.git
# 또는 업데이트 시
git pull

# 2. 환경변수 파일 생성 (최초 1회)
cp .env.example .env
# .env 파일에 실제 API 키 값 입력

# 3. Docker 이미지 빌드
docker build -t audit-agent .

# 4. 컨테이너 실행
docker compose up -d
```

---

## 에이전트 명세 (`agent.yaml`)

오케스트레이터가 이 파일을 참조하여 에이전트의 요구 환경변수와 엔드포인트를 파악합니다.

```yaml
name: audit-agent
env:
  required:
    - UPSTAGE_API_KEY          # Upstage Solar LLM API 키
    - ORCHESTRATOR_BASE_URL    # 오케스트레이터 API 기본 주소
  optional:
    - ORCHESTRATOR_API_KEY     # 오케스트레이터 인증 토큰 (필요시)
endpoints:
  health: GET /health
  invoke: POST /invoke
port: 8000
```

---

## 연동 흐름 (Active Pull)

```
Orchestrator
  │
  └─ POST /invoke
       {"report_id": "RPT-CURL-0001", "trace_id": "...", "request_id": "..."}
              │
              ▼
       Audit Agent
              │
              ├─ GET  {ORCHESTRATOR_BASE_URL}/upstageknu2607/db/workflows/{report_id}
              │       (공통 파이프라인 JSON 직접 조회)
              │
              ├─ 결정론적 사전검사 (LLM 없음)
              │   → VIOLATION 발견 시 즉시 audit_status: FAIL 확정
              │
              ├─ LLM 감사 (Upstage solar-pro3)
              │
              ├─ POST {ORCHESTRATOR_BASE_URL}/upstageknu2607/db/workflows/{report_id}
              │       /agents/audit_agent/invocations
              │       (감사 결과 DB 등록)
              │
              └─ 응답 반환
                   {"status_code": 200, "message": "audit completed", "output": {...}}
```

---

## API 명세

### `GET /health`
에이전트 현재 상태를 반환합니다.

**응답 예시:**
```json
{"status": "ready"}
```
```json
{"status": "working RPT-CURL-0001"}
```

---

### `POST /invoke`
감사를 실행하는 표준 엔드포인트입니다.

**요청 바디:**
```json
{
  "report_id": "RPT-CURL-0001",
  "trace_id": "trace-RPT-CURL-0001-20260709",
  "request_id": "req-audit-001"
}
```

**응답 예시 (성공):**
```json
{
  "status_code": 200,
  "message": "audit completed",
  "output": {
    "report_id": "RPT-CURL-0001",
    "audit_status": "PASS",
    "routing_decision": "HUMAN_REVIEW",
    "priority": "NORMAL",
    "reason": "인용된 함수와 호출 경로가 실제 코드베이스에서 확인됨. PoC 부재로 재현 필요.",
    "failure_codes": [],
    "key_points_for_human": [
      "curl_mfprintf 함수 존재 확인됨",
      "PoC 컴파일 불가 — 재현 검증 필요"
    ]
  }
}
```

`failure_codes`는 필수 필드입니다. `PASS`이면 빈 배열이어야 하고,
`FAIL`이면 `UPPER_SNAKE_CASE` 코드가 하나 이상 있어야 합니다.
`reason`과 `audit_status`가 명백히 모순되면 해당 응답을 거부하고 한 번
자가 수정 재시도를 수행합니다. `workflow_status=DROPPED`인 입력은 Debate가
실행되지 않은 정상 종료 경로이므로 빈 Debate verdict를 위반으로 보지 않습니다.

---

## 로컬 개발 환경 설정

```bash
# 1. 가상환경 생성 및 패키지 설치
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 환경변수 설정
cp .env.example .env
# .env 파일에 UPSTAGE_API_KEY, ORCHESTRATOR_BASE_URL 값 입력

# 3. 서버 실행 (개발 모드 - 코드 변경 시 자동 재시작)
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Swagger UI: `http://localhost:8000/docs`

---

## 프로젝트 구조

```
.
├── agent.yaml          # 오케스트레이터 에이전트 명세 (env, endpoints)
├── app.py              # FastAPI 서버 (GET /health, POST /invoke)
├── audit_agent.py      # 감사 코어 로직 (결정론적 검사 + LLM 감사)
├── Dockerfile          # Ubuntu 24.04 LTS 기반 컨테이너 정의
├── docker-compose.yml  # 컨테이너 실행 설정
├── requirements.txt    # Python 의존성
└── .env.example        # 환경변수 설정 가이드
```

---

## 감사 기준 (중요 원칙)

| 원칙 | 내용 |
|------|------|
| 1 | 리포트가 자동 승인(`AUTO_APPROVED`)되어서는 안 된다 |
| 2 | 최종 패치(`PATCHED`) 여부가 임의로 결정되어서는 안 된다 |
| 3 | `AUTO_REJECT`/`DUPLICATE`가 감사 단계 이전에 부당하게 설정되어서는 안 된다 |
| 4 | 모든 판단은 `fact_check` + `debate.judge` 결과를 기준으로 검증해야 한다 |
| 5 | `fact_check`에서 존재하지 않는다고 판정된 요소를 인용한 주장은 신뢰해서는 안 된다 |
| 6 | "AI가 쓴 것 같다"는 주관적 인상이 판단 근거가 되어서는 안 된다 |
