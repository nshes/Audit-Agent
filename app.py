"""
app.py

Audit Agent FastAPI 서버 — 오케스트레이터 표준 형식 (Active Pull + /invoke).

배포 방식:
  오케스트레이터가 Git pull → Docker build → docker run 으로 에이전트를 기동합니다.

동작 흐름:
  1. 오케스트레이터가 POST /invoke 로 report_id / trace_id / request_id 전달
  2. Audit Agent가 오케스트레이터 DB에서 공통 JSON을 직접 GET으로 가져옴
  3. 결정론적 사전검사 → LLM 감사 수행
  4. 감사 결과를 오케스트레이터 DB invocations에 등록
  5. 표준 응답 형식으로 결과 반환

오케스트레이터 연동 엔드포인트 (내부 호출):
  GET  {ORCHESTRATOR_BASE_URL}/upstageknu2607/db/workflows/{report_id}
  POST {ORCHESTRATOR_BASE_URL}/upstageknu2607/db/workflows/{report_id}/agents/audit_agent/invocations

Audit Agent 노출 엔드포인트:
  GET  /health   - 에이전트 상태 확인 (ready / working {job-id})
  POST /invoke   - 감사 실행
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Generator, List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

load_dotenv()

from audit_agent import (
    AuditAgent,
    AuditAgentConfig,
    DeterministicAuditChecks,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("audit_app")
SOLAR_MODEL = os.environ.get("SOLAR_MODEL", "solar-pro3")
UPSTAGE_BASE_URL = os.environ.get("UPSTAGE_BASE_URL", "https://api.upstage.ai/v1")


# ---------------------------------------------------------------------------
# FastAPI 앱 초기화
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Audit Agent",
    description=(
        "버그 바운티 / 취약점 제보 파이프라인 감사 에이전트.\n\n"
        "오케스트레이터는 `report_id`만 전달하면 되며, Audit Agent가 직접 오케스트레이터 DB에서 "
        "공통 파이프라인 JSON을 조회하고, 감사 결과도 직접 DB에 등록합니다.\n\n"
        "### 감사 기준 (중요 원칙)\n"
        "1. 리포트가 자동 승인되어서는 안 된다\n"
        "2. 최종 패치 여부가 임의로 결정되어서는 안 된다\n"
        "3. AUTO_REJECT/DUPLICATE가 이 단계에서 새로 결정되어서는 안 된다\n"
        "4. 모든 판단은 fact_check + debate judge 결과를 기준으로 검증해야 한다\n"
        "5. fact_check 결과와 충돌하는 주장은 신뢰해서는 안 된다\n"
        "6. 'AI가 쓴 것 같다'는 주관적 인상이 판단 근거가 되어서는 안 된다\n\n"
        "### 설계 원칙\n"
        "**Deterministic-first**: LLM 호출 전에 구조 대조 기반 결정론적 검사를 먼저 수행합니다. "
        "VIOLATION이 하나라도 발견되면 LLM 판단과 무관하게 `audit_status`를 `FAIL`로 강제합니다."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


# ---------------------------------------------------------------------------
# 오케스트레이터 클라이언트
# ---------------------------------------------------------------------------

class OrchestratorClient:
    """
    오케스트레이터 DB/API와 통신하는 HTTP 클라이언트.

      GET  {base_url}/upstageknu2607/db/workflows/{report_id}
      POST {base_url}/upstageknu2607/db/workflows/{report_id}/agents/audit_agent/invocations
    """

    AGENT_NAME = "final_audit"
    TIMEOUT = 30.0

    def __init__(self) -> None:
        self.base_url = os.environ.get("ORCHESTRATOR_BASE_URL", "").rstrip("/")
        self.api_key = os.environ.get("ORCHESTRATOR_API_KEY", "")
        if not self.base_url:
            raise RuntimeError(
                "환경변수 ORCHESTRATOR_BASE_URL이 설정되지 않았습니다. "
                "agent.yaml의 env.required 항목을 확인하세요."
            )

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def fetch_pipeline_report(self, report_id: str) -> Dict[str, Any]:
        """GET /upstageknu2607/db/workflows/{report_id}"""
        url = f"{self.base_url}/upstageknu2607/db/workflows/{report_id}"
        logger.info("[Orchestrator] GET %s", url)
        try:
            with httpx.Client(timeout=self.TIMEOUT) as client:
                resp = client.get(url, headers=self._headers())
            resp.raise_for_status()
            logger.info("[Orchestrator] 공통 JSON 수신 완료: report_id=%s", report_id)
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"오케스트레이터 DB 조회 실패: report_id={report_id}, HTTP {e.response.status_code}",
            )
        except httpx.RequestError as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"오케스트레이터 연결 실패: {self.base_url} — {e}",
            )

    def register_invocation(
        self,
        report_id: str,
        audit_result: Dict[str, Any],
        trace_id: Optional[str] = None,
        request_id: Optional[str] = None,
        duration_ms: Optional[int] = None,
        agent_job_id: Optional[int] = None,
        token_usage: Optional[Dict[str, int]] = None,
    ) -> None:
        """POST /upstageknu2607/db/workflows/{report_id}/agents/audit_agent/invocations"""
        url = (
            f"{self.base_url}/upstageknu2607/db/workflows/{report_id}"
            f"/agents/{self.AGENT_NAME}/invocations"
        )
        payload: Dict[str, Any] = {
            "status_code": 200,
            "message": "final_audit completed",
            "status": "SUCCEEDED",
            "output": audit_result,
            "agent_job_id": agent_job_id,
            "model": SOLAR_MODEL,
            "token_usage": token_usage or {},
        }
        if trace_id:
            payload["trace_id"] = trace_id
        if request_id:
            payload["request_id"] = request_id
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms

        logger.info("[Orchestrator] POST invocations: report_id=%s", report_id)
        try:
            with httpx.Client(timeout=self.TIMEOUT) as client:
                resp = client.post(url, json=payload, headers=self._headers())
            resp.raise_for_status()
            logger.info(
                "[Orchestrator] invocation 등록 완료: audit_status=%s",
                audit_result.get("audit_status"),
            )
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            # 결과 등록 실패는 경고만 기록하고 응답은 정상 반환
            logger.warning("[Orchestrator] invocation 등록 실패 (무시): %s", e)


# ---------------------------------------------------------------------------
# 파이프라인 공통 JSON 스키마 (Pydantic)
# ---------------------------------------------------------------------------

class InputSection(BaseModel):
    raw_report_txt: Optional[str] = None
    model_config = {"extra": "allow"}


class BugReportChecker(BaseModel):
    is_bug_report: Optional[bool] = None
    confidence: Optional[float] = None
    reason: Optional[str] = None
    model_config = {"extra": "allow"}


class ReporterInfo(BaseModel):
    name: Optional[str] = None
    team: Optional[str] = None
    contacts: List[str] = Field(default_factory=list)
    model_config = {"extra": "allow"}


class ParserAgentResult(BaseModel):
    reporter: Optional[ReporterInfo] = None
    title: Optional[str] = None
    vuln_type: Optional[str] = None
    affected_software: Optional[str] = None
    affected_version: Optional[str] = None
    summary: Optional[str] = None
    cited_functions: List[str] = Field(default_factory=list)
    function_calls: List[str] = Field(default_factory=list)
    cited_headers: List[str] = Field(default_factory=list)
    cited_commits: List[str] = Field(default_factory=list)
    poc_present: bool = False
    poc_code: Optional[str] = None
    repro_steps: List[str] = Field(default_factory=list)
    claimed_impact: List[str] = Field(default_factory=list)
    model_config = {"extra": "allow"}


class FunctionCheckItem(BaseModel):
    name: str
    exists: bool
    location: Optional[str] = None
    model_config = {"extra": "allow"}


class CommitCheckItem(BaseModel):
    ref: str
    exists: bool
    reason: Optional[str] = None
    model_config = {"extra": "allow"}


class HeaderCheckItem(BaseModel):
    name: str
    exists: bool
    model_config = {"extra": "allow"}


class PocCheck(BaseModel):
    compilable: Optional[bool] = None
    compile_error: Optional[str] = None
    model_config = {"extra": "allow"}


class Reachability(BaseModel):
    verdict: Optional[str] = "UNKNOWN"
    reason: Optional[str] = None
    model_config = {"extra": "allow"}


class FactCheckAgentResult(BaseModel):
    function_check: List[FunctionCheckItem] = Field(default_factory=list)
    file_check: List[Dict[str, Any]] = Field(default_factory=list)
    header_check: List[HeaderCheckItem] = Field(default_factory=list)
    commit_check: List[CommitCheckItem] = Field(default_factory=list)
    function_call_check: List[Dict[str, Any]] = Field(default_factory=list)
    poc_check: Optional[PocCheck] = None
    reachability: Optional[Reachability] = None
    summary: Optional[str] = None
    model_config = {"extra": "allow"}


class DedupMatch(BaseModel):
    title: str
    similarity: float
    same_root_cause: bool
    previous_result: Optional[str] = None
    model_config = {"extra": "allow"}


class DedupAgentResult(BaseModel):
    signature: Optional[str] = None
    matches: List[DedupMatch] = Field(default_factory=list)
    verdict: Optional[str] = None
    duplicate_of: Optional[str] = None
    model_config = {"extra": "allow"}


class DebateSideAgent(BaseModel):
    position: Optional[str] = None
    argument: Optional[str] = None
    evidence: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)
    strength: Optional[str] = None
    confidence: Optional[float] = None
    model_config = {"extra": "allow"}


class DebateJudgeAgentResult(BaseModel):
    verdict: Optional[str] = None
    winning_side: Optional[str] = None
    reason: Optional[str] = None
    confidence: Optional[float] = None
    next_step: Optional[str] = None
    model_config = {"extra": "allow"}


class DebateSection(BaseModel):
    debate_logs: List[Dict[str, Any]] = Field(default_factory=list)
    vulnerable_agent: Optional[DebateSideAgent] = None
    not_vulnerable_agent: Optional[DebateSideAgent] = None
    judge: Optional[DebateJudgeAgentResult] = None
    verdict: Optional[DebateJudgeAgentResult] = None
    model_config = {"extra": "allow"}


class AgentResults(BaseModel):
    bug_report_checker: Optional[BugReportChecker] = None
    parser: Optional[ParserAgentResult] = None
    fact_check: Optional[FactCheckAgentResult] = None
    dedup: Optional[DedupAgentResult] = None
    debate: Optional[DebateSection] = None
    model_config = {"extra": "allow"}


class PipelineReport(BaseModel):
    """오케스트레이터 DB의 공통 파이프라인 JSON 스키마"""
    report_id: str
    workflow_status: str
    created_at: Optional[str] = None
    input: Optional[InputSection] = None
    agent_results: Optional[AgentResults] = None
    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# 요청 / 응답 스키마
# ---------------------------------------------------------------------------

class InvokeRequest(BaseModel):
    """
    `POST /invoke` 요청 바디 (오케스트레이터 표준 형식).
    오케스트레이터는 이 세 필드만 전달합니다.
    Audit Agent가 직접 오케스트레이터 DB에서 공통 JSON을 가져와 감사를 수행합니다.
    """
    report_id: str = Field(
        ...,
        description="감사 대상 리포트 ID",
        examples=["RPT-CURL-0001"],
    )
    trace_id: Optional[str] = Field(
        None,
        description="분산 추적 ID (cross-service trace)",
        examples=["trace-RPT-CURL-0001-20260709"],
    )
    request_id: Optional[str] = Field(
        None,
        description="오케스트레이터가 생성한 요청 ID",
        examples=["req-audit-001"],
    )
    agent_job_id: Optional[int] = Field(None, description="claimed workflow_agent_jobs.id")


class InvokeResponse(BaseModel):
    """
    `POST /invoke` 응답 바디 (오케스트레이터 표준 형식).
    """
    status_code: int = Field(..., description="HTTP 상태 코드", examples=[200])
    message: str = Field(..., description="처리 결과 요약 메시지", examples=["audit completed"])
    output: Dict[str, Any] = Field(..., description="감사 결과 상세 데이터")
    token_usage: Dict[str, int] = Field(default_factory=dict, description="이번 호출의 LLM 토큰 사용량")


class HealthResponse(BaseModel):
    status: str = Field(
        ...,
        description="`ready` (유휴) 또는 `working {job_id}` (처리 중)",
        examples=["ready", "working RPT-CURL-0001"],
    )


# ---------------------------------------------------------------------------
# Job 상태 추적 (thread-safe)
# ---------------------------------------------------------------------------

_job_lock = threading.Lock()
_current_job_id: Optional[str] = None


@contextmanager
def _job_context(job_id: str) -> Generator[None, None, None]:
    """엔드포인트 실행 동안 현재 처리 중인 job_id를 등록하고, 완료 시 해제한다."""
    global _current_job_id
    with _job_lock:
        _current_job_id = job_id
    logger.info("job 시작: %s", job_id)
    try:
        yield
    finally:
        with _job_lock:
            _current_job_id = None
        logger.info("job 완료: %s", job_id)


# ---------------------------------------------------------------------------
# 내부 변환 헬퍼 — PipelineReport → audit_agent 인자
# ---------------------------------------------------------------------------

def _extract_audit_context(report: PipelineReport) -> Dict[str, Any]:
    ar = report.agent_results or AgentResults()
    debate = ar.debate or DebateSection()
    fallback_judge = (debate.verdict or DebateJudgeAgentResult()).model_dump()
    explicit_judge = (debate.judge or DebateJudgeAgentResult()).model_dump()
    resolved_judge = {
        key: value
        for key, value in fallback_judge.items()
    }
    resolved_judge.update({
        key: value
        for key, value in explicit_judge.items()
        if value is not None and value != ""
    })
    return {
        "parser_result":               (ar.parser or ParserAgentResult()).model_dump(),
        "fact_check_result":           (ar.fact_check or FactCheckAgentResult()).model_dump(),
        "dedup_result":                (ar.dedup or DedupAgentResult()).model_dump(),
        "debate_judge_result":         DebateJudgeAgentResult.model_validate(resolved_judge).model_dump(),
        "vulnerable_agent_result":     (debate.vulnerable_agent or DebateSideAgent()).model_dump(),
        "not_vulnerable_agent_result": (debate.not_vulnerable_agent or DebateSideAgent()).model_dump(),
        "workflow_status":             report.workflow_status,
    }


# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------

@app.get(
    "/health",
    response_model=HealthResponse,
    summary="헬스체크",
    description=(
        "오케스트레이터가 에이전트 준비 상태를 확인하는 엔드포인트입니다.\n\n"
        "- `{\"status\": \"ready\"}` → 유휴 상태, 새 작업 수신 가능\n"
        "- `{\"status\": \"working {job_id}\"}` → 해당 report_id 처리 중"
    ),
    tags=["Monitoring"],
)
def health_check() -> HealthResponse:
    with _job_lock:
        job = _current_job_id
    return HealthResponse(status=f"working {job}" if job else "ready")


@app.post(
    "/invoke",
    response_model=InvokeResponse,
    summary="감사 실행",
    description=(
        "오케스트레이터 표준 `/invoke` 엔드포인트.\n\n"
        "`report_id` / `trace_id` / `request_id` 를 받아 아래 순서로 실행합니다:\n\n"
        "1. `GET {ORCHESTRATOR_BASE_URL}/upstageknu2607/db/workflows/{report_id}` 로 공통 JSON 조회\n"
        "2. 결정론적 사전검사 (VIOLATION 발견 시 즉시 `audit_status: FAIL` 확정)\n"
        f"3. LLM({SOLAR_MODEL}) 감사 수행\n"
        "4. `POST .../agents/audit_agent/invocations` 로 결과 등록\n"
        "5. 표준 응답 형식으로 결과 반환\n\n"
        "> ⚠️ `UPSTAGE_API_KEY`와 `ORCHESTRATOR_BASE_URL`이 반드시 설정되어 있어야 합니다."
    ),
    tags=["Invoke"],
    responses={
        200: {"description": "감사 완료"},
        502: {"description": "오케스트레이터 DB 연결 실패"},
        503: {"description": "필수 환경변수 미설정 (UPSTAGE_API_KEY / ORCHESTRATOR_BASE_URL)"},
    },
)
def invoke(request: InvokeRequest) -> InvokeResponse:
    # 환경변수 확인
    upstage_key = os.environ.get("UPSTAGE_API_KEY", "")
    if not upstage_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="UPSTAGE_API_KEY가 설정되지 않았습니다. agent.yaml의 env.required를 확인하세요.",
        )

    start_ms = int(time.time() * 1000)

    with _job_context(request.report_id):
        # 1) 오케스트레이터 DB에서 공통 JSON 조회
        orch = OrchestratorClient()
        raw = orch.fetch_pipeline_report(request.report_id)

        # 오케스트레이터 응답은 {"workflow": {...}} 또는 직접 {...} 두 형태 모두 지원
        if "workflow" in raw and isinstance(raw["workflow"], dict):
            raw = raw["workflow"]

        report = PipelineReport.model_validate(raw)
        logger.info(
            "감사 시작: report_id=%s workflow_status=%s trace_id=%s",
            report.report_id, report.workflow_status, request.trace_id,
        )

        # 2~3) 감사 수행
        ctx = _extract_audit_context(report)
        agent = AuditAgent(
            AuditAgentConfig(
                api_key=upstage_key,
                model=SOLAR_MODEL,
                base_url=UPSTAGE_BASE_URL,
            )
        )
        audit_result = agent.run(
            parser_result=ctx["parser_result"],
            fact_check_result=ctx["fact_check_result"],
            dedup_result=ctx["dedup_result"],
            debate_judge_result=ctx["debate_judge_result"],
            vulnerable_agent_result=ctx["vulnerable_agent_result"],
            not_vulnerable_agent_result=ctx["not_vulnerable_agent_result"],
            workflow_status=ctx["workflow_status"],
        )
        audit_result["report_id"] = report.report_id
        token_usage = dict(agent.token_usage)

        duration_ms = int(time.time() * 1000) - start_ms

        # 4) 오케스트레이터 DB에 결과 등록 (실패해도 응답은 정상 반환)
        orch.register_invocation(
            report_id=report.report_id,
            audit_result=audit_result,
            trace_id=request.trace_id,
            request_id=request.request_id,
            duration_ms=duration_ms,
            agent_job_id=request.agent_job_id,
            token_usage=token_usage,
        )

    # 5) 표준 응답 형식으로 반환
    return InvokeResponse(
        status_code=200,
        message="audit completed",
        output=audit_result,
        token_usage=token_usage,
    )
