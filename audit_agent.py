"""
audit_agent.py

"전체 과정 감사(Audit)" Agent - 워크플로우 마지막 노드. Upstage Solar API 기반 구현.

역할 (사용자가 제공한 프롬프트 기준)
- parser_result, fact_check_result, dedup_result, debate_judge_result 및 각 단계의
  실행 로그를 받아, 앞선 단계들이 정의된 원칙과 절차를 실제로 지켰는지 감사한다.
- 리포트를 승인/기각하지 않는다. AUTO_REJECT/DUPLICATE를 이 단계가 새로 정하지 않는다.
- routing_decision / priority / audit_status(PASS 또는 FAIL) / reason / key_points_for_human만 낸다.

핵심 설계 결정: "감사"는 상당 부분 결정론적으로 검증 가능하다
------------------------------------------------------------
감사 기준 6개 중 다음은 LLM 판단 없이도 구조적으로 확인 가능하다.
  - 원칙 3 (AUTO_REJECT/DUPLICATE를 이 단계가 새로 정하면 안 됨)
    -> 아예 출력 스키마에 그런 필드를 두지 않아서 이 Agent가 물리적으로 그 값을 낼 수 없게 만든다.
  - "절차를 실제로 밟았는가" (예: cited_functions가 있는데 function_check가 비어있다면
    사실 판단 Agent가 절차 1을 건너뛴 것)
    -> parser_result와 fact_check_result의 필드를 서로 대조하면 100% 결정론적으로 알 수 있다.
  - "fact_check_result와 충돌하는 다른 Agent의 주장을 신뢰하지 않는다" (원칙 5)
    -> 찬반 Agent의 evidence 문자열에 fact_check_result가 "존재하지 않는다"고 확정한
       함수/커밋 이름이 "존재한다"는 문맥으로 등장하는지 키워드 대조로 1차 스크리닝 가능.

그래서 이 모듈은 DeterministicAuditChecks를 LLM 호출 전에 먼저 돌리고, VIOLATION이
하나라도 나오면 LLM이 뭐라고 하든 최종 audit_status를 FAIL로 강제 덮어쓴다. LLM은
결정론적으로 못 잡는 미묘한 절차 위반(맥락상 판단이 필요한 것)만 보완하는 역할이다.
이건 이 프로젝트 전체에서 반복해온 "Deterministic-first, LLM은 결정론이 못 미치는
곳만" 원칙을 감사 단계에도 그대로 적용한 것이다.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI, RateLimitError
from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger("audit_agent")


# --------------------------------------------------------------------------
# 출력 스키마
# --------------------------------------------------------------------------

class RoutingDecision(str, Enum):
    SECURITY_TEAM_ESCALATION = "SECURITY_TEAM_ESCALATION"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    LOW_PRIORITY_REVIEW = "LOW_PRIORITY_REVIEW"
    NEEDS_REPRODUCTION = "NEEDS_REPRODUCTION"
    NEEDS_EXPERT_REVIEW = "NEEDS_EXPERT_REVIEW"


class Priority(str, Enum):
    URGENT = "URGENT"
    NORMAL = "NORMAL"
    LOW = "LOW"


class AuditStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class AuditResult(BaseModel):
    routing_decision: RoutingDecision
    priority: Priority
    audit_status: AuditStatus
    reason: str = Field(..., min_length=1)
    key_points_for_human: List[str] = Field(default_factory=list)

    @field_validator("key_points_for_human")
    @classmethod
    def _clean_points(cls, v: List[str]) -> List[str]:
        return [p.strip() for p in v if p and p.strip()][:10]


AUDIT_RESULT_JSON_SCHEMA = {
    "name": "audit_result",
    "schema": {
        "type": "object",
        "properties": {
            "routing_decision": {"type": "string", "enum": [e.value for e in RoutingDecision]},
            "priority": {"type": "string", "enum": [e.value for e in Priority]},
            "audit_status": {"type": "string", "enum": [e.value for e in AuditStatus]},
            "reason": {"type": "string"},
            "key_points_for_human": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["routing_decision", "priority", "audit_status", "reason", "key_points_for_human"],
        "additionalProperties": False,
    },
    "strict": True,
}

# 감사 자체가 실패했을 때(=LLM/파싱 오류)의 안전 기본값.
# 감사가 안 됐다는 건 "문제없다"가 아니라 "확인이 안 됐다"이므로, 반드시 FAIL + 사람 검토로 보낸다.
SAFE_FALLBACK = AuditResult(
    routing_decision=RoutingDecision.HUMAN_REVIEW,
    priority=Priority.NORMAL,
    audit_status=AuditStatus.FAIL,
    reason="감사 Agent 응답이 스키마 검증을 통과하지 못해 안전 기본값(FAIL)으로 처리함 - 사람이 직접 감사 필요",
    key_points_for_human=["자동 감사 실패 케이스입니다. 원본 LLM 응답 로그와 결정론적 사전검사 결과를 함께 확인하세요."],
)


# --------------------------------------------------------------------------
# 결정론적 사전 감사 (LLM 호출 전)
# --------------------------------------------------------------------------

@dataclass
class DeterministicFinding:
    code: str
    message: str
    severity: str  # "VIOLATION" | "WARNING"

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "severity": self.severity}


class DeterministicAuditChecks:
    """
    LLM 없이 구조 대조만으로 확인 가능한 감사 항목.
    VIOLATION: 최종 audit_status를 FAIL로 강제한다.
    WARNING: LLM에게 참고 근거로만 전달한다 (휴리스틱이라 오탐 가능성 있음).
    """

    def run(self, ctx: Dict[str, Any]) -> List[DeterministicFinding]:
        findings: List[DeterministicFinding] = []
        findings += self._check_procedure_completeness(ctx)
        findings += self._check_role_boundaries(ctx)
        findings += self._check_evidence_contradictions(ctx)
        return findings

    # ---- 원칙 4: 절차가 실제로 수행됐는가 ----
    def _check_procedure_completeness(self, ctx: Dict[str, Any]) -> List[DeterministicFinding]:
        findings: List[DeterministicFinding] = []
        parser = ctx.get("parser_result") or {}
        fact = ctx.get("fact_check_result") or {}
        judge = ctx.get("debate_judge_result") or {}

        if parser.get("cited_functions") and not fact.get("function_check"):
            findings.append(DeterministicFinding(
                "MISSING_FUNCTION_CHECK",
                "parser_result.cited_functions가 존재하는데 fact_check_result.function_check가 비어 있음 "
                "- 사실 판단 Agent 절차 1(symbol_lookup 전체 호출) 미준수 가능성",
                "VIOLATION",
            ))
        if parser.get("cited_commits") and not fact.get("commit_check"):
            findings.append(DeterministicFinding(
                "MISSING_COMMIT_CHECK",
                "parser_result.cited_commits가 존재하는데 fact_check_result.commit_check가 비어 있음 "
                "- 절차 4(git_history_query 호출) 미준수 가능성",
                "VIOLATION",
            ))
        if parser.get("cited_headers") and not fact.get("header_check"):
            findings.append(DeterministicFinding(
                "MISSING_HEADER_CHECK",
                "parser_result.cited_headers가 존재하는데 fact_check_result.header_check가 비어 있음 "
                "- 절차 3(header_lookup 호출) 미준수 가능성",
                "VIOLATION",
            ))
        if parser.get("poc_present") and parser.get("poc_code"):
            compilable = (fact.get("poc_check") or {}).get("compilable")
            if compilable is None:
                findings.append(DeterministicFinding(
                    "MISSING_POC_CHECK",
                    "poc_present=true이고 poc_code가 있는데 poc_check.compilable이 null임 "
                    "- 절차 6(can_compile 호출) 미준수 가능성",
                    "VIOLATION",
                ))
        if not judge.get("verdict"):
            findings.append(DeterministicFinding(
                "INCOMPLETE_JUDGE_STAGE",
                "debate_judge_result.verdict가 비어 있음 - 찬반토론 Judge 단계가 완료되지 않은 채로 "
                "감사 단계에 도달함",
                "VIOLATION",
            ))
        return findings

    # ---- 원칙 1/2/3: 역할 경계 (자동 승인/패치/기각/중복을 이 단계가 침범하지 않았는가) ----
    def _check_role_boundaries(self, ctx: Dict[str, Any]) -> List[DeterministicFinding]:
        findings: List[DeterministicFinding] = []

        dedup = ctx.get("dedup_result") or {}
        verdict = dedup.get("verdict")
        if verdict is not None and verdict not in ("DUPLICATE", "NOT_DUPLICATE"):
            findings.append(DeterministicFinding(
                "INVALID_DEDUP_VERDICT",
                f"dedup_result.verdict가 정의되지 않은 값 '{verdict}'임 - 중복 판별 Agent의 "
                "출력 스키마(DUPLICATE|NOT_DUPLICATE) 위반",
                "VIOLATION",
            ))

        # 워크플로우 상태가 이 감사 단계 도달 이전에 이미 종결 상태로 바뀌어 있으면
        # 그 자체가 "누군가 임의로 자동 승인/패치/기각했다"는 강한 신호다.
        status = ctx.get("workflow_status")
        forbidden_terminal_states = {"AUTO_APPROVED", "PATCHED", "AUTO_REJECTED", "CLOSED"}
        if status in forbidden_terminal_states:
            findings.append(DeterministicFinding(
                "PREMATURE_TERMINAL_STATUS",
                f"workflow_status가 이미 '{status}'로 설정되어 있음 - 원칙 1/2/3 위반 소지. "
                "이 파이프라인의 어떤 개별 Agent도 자동 승인/최종 패치/자동 기각을 단독으로 "
                "확정할 권한이 없다.",
                "VIOLATION",
            ))
        return findings

    # ---- 원칙 5: fact_check_result와 충돌하는 주장을 신뢰하지 않았는가 ----
    def _check_evidence_contradictions(self, ctx: Dict[str, Any]) -> List[DeterministicFinding]:
        """
        얕은 키워드 대조 휴리스틱이다. 오탐 가능성이 있어 VIOLATION이 아니라 WARNING으로만
        표시하고, 최종 판단은 LLM/사람에게 맡긴다.
        """
        findings: List[DeterministicFinding] = []
        fact = ctx.get("fact_check_result") or {}

        false_functions = {f["name"] for f in fact.get("function_check", []) if f.get("exists") is False and f.get("name")}
        false_commits = {c["ref"] for c in fact.get("commit_check", []) if c.get("exists") is False and c.get("ref")}
        false_headers = {h["name"] for h in fact.get("header_check", []) if h.get("exists") is False and h.get("name")}

        for side_key in ("vulnerable_agent_result", "not_vulnerable_agent_result"):
            side = ctx.get(side_key) or {}
            for ev in (side.get("evidence") or []):
                if not isinstance(ev, str):
                    continue
                for bucket, label in ((false_functions, "함수"), (false_commits, "커밋"), (false_headers, "헤더")):
                    for name in bucket:
                        if name in ev and "존재" in ev and "존재하지" not in ev and "없" not in ev:
                            findings.append(DeterministicFinding(
                                "EVIDENCE_CONTRADICTS_FACT_CHECK",
                                f"[{side_key}] evidence 항목 '{ev}'가 fact_check_result에서 존재하지 않는다고 "
                                f"확인된 {label} '{name}'을(를) 존재하는 것처럼 언급하는 것으로 보임 (키워드 대조, 사람 확인 필요)",
                                "WARNING",
                            ))
        return findings


# --------------------------------------------------------------------------
# 시스템 프롬프트 (사용자 제공 원문)
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """너는 전체 과정 감사(Audit) Agent다.
너의 임무는 parser_result, fact_check_result, dedup_result, debate_judge_result 등 전체 과정 파일을 바탕으로, 앞선 단계들이 정의된 중요 원칙과 절차를 올바르게 준수하며 수행되었는지 철저히 검증하고 감사하는 것이다.
중요 원칙 (감사 기준):
1. 리포트가 자동 승인되어서는 안 된다.
2. 최종 패치 여부가 임의로 결정되어서는 안 된다.
3. AUTO_REJECT나 DUPLICATE가 이 단계에서 새로 결정되어서는 안 된다. (자동 기각/중복 처리는 반드시 앞선 단계의 역할이어야 함)
4. 모든 판단은 fact_check_result와 debate_judge_result를 기준으로 검증해야 한다.
5. fact_check_result와 충돌하는 다른 Agent의 주장은 신뢰해서는 안 된다.
6. '글이 AI가 쓴 것 같다'는 주관적인 인상이 판단 근거로 사용되어서는 안 된다.
수행 절차:
1. 입력된 전체 과정 파일(parser, fact_check, dedup, debate_judge)을 취합한다.
2. 각 단계의 결과물이 '중요 원칙'을 위배하지 않고 정당하게 도출되었는지 하나씩 검증한다.
3. 특히 fact_check_result의 핵심 근거와 다른 Agent의 주장 사이에 충돌이 없는지 확인한다.
4. 임의로 자동 기각(AUTO_REJECT)이나 중복(DUPLICATE)을 새로 내린 단계가 있는지 모니터링한다.
5. 감사 과정에서 발견된 특이사항이나 원칙 위배 항목, 또는 사람이 검토할 때 반드시 짚고 넘어가야 할 핵심 쟁점을 key_points_for_human에 정리한다.

추가 지침: 이 요청에는 '결정론적 사전검사 결과(deterministic_precheck)'가 함께 제공된다.
severity가 "VIOLATION"인 항목이 하나라도 있으면 반드시 audit_status를 "FAIL"로 판단하라.
severity가 "WARNING"인 항목은 참고하되, 실제 내용을 보고 정말 원칙 위반인지 스스로 판단하라.

출력 규칙:
반드시 JSON만 출력한다.
마크다운, 설명문, 코드펜스는 출력하지 않는다."""


# --------------------------------------------------------------------------
# 예외 / 설정
# --------------------------------------------------------------------------

class UpstageCallError(Exception):
    """Upstage API 호출이 재시도 후에도 실패."""


@dataclass
class AuditAgentConfig:
    api_key: str
    model: str = "solar-pro2"
    base_url: str = "https://api.upstage.ai/v1"
    timeout: float = 30.0
    max_retries: int = 3
    temperature: float = 0.0


# --------------------------------------------------------------------------
# Agent 본체
# --------------------------------------------------------------------------

class AuditAgent:
    def __init__(self, config: AuditAgentConfig):
        self.config = config
        self.client = OpenAI(api_key=config.api_key, base_url=config.base_url, timeout=config.timeout)
        self.deterministic_checks = DeterministicAuditChecks()

    # ---- 공개 API ----

    def run(
        self,
        parser_result: dict,
        fact_check_result: dict,
        dedup_result: dict,
        debate_judge_result: dict,
        vulnerable_agent_result: Optional[dict] = None,
        not_vulnerable_agent_result: Optional[dict] = None,
        workflow_status: Optional[str] = None,
        process_logs: Optional[List[dict]] = None,
    ) -> dict:
        """
        예외를 던지지 않는다 - 어떤 경우든 유효한 AuditResult dict를 반환한다.
        (LLM 호출이 완전히 실패해도 SAFE_FALLBACK으로 파이프라인이 계속 진행되도록.)
        """
        ctx = {
            "parser_result": parser_result,
            "fact_check_result": fact_check_result,
            "dedup_result": dedup_result,
            "debate_judge_result": debate_judge_result,
            "vulnerable_agent_result": vulnerable_agent_result or {},
            "not_vulnerable_agent_result": not_vulnerable_agent_result or {},
            "workflow_status": workflow_status,
            "process_logs": process_logs or [],
        }

        # 1) 결정론적 사전 감사 (LLM 호출 전에 항상 먼저 실행)
        findings = self.deterministic_checks.run(ctx)
        has_hard_violation = any(f.severity == "VIOLATION" for f in findings)
        if findings:
            logger.info("결정론적 사전검사 발견 사항 %d건 (VIOLATION=%s): %s",
                        len(findings), has_hard_violation, [f.code for f in findings])

        # 2) LLM 감사 (사전검사 결과를 근거로 함께 전달)
        payload = dict(ctx)
        payload["deterministic_precheck"] = [f.to_dict() for f in findings]

        result: Optional[AuditResult] = None
        try:
            raw = self._call_with_retry(self._build_messages(payload))
            result = self._parse_and_validate(raw)
        except UpstageCallError as e:
            logger.error("Upstage 호출 최종 실패: %s", e)

        if result is None:
            logger.warning("1차 응답 검증 실패 - 자가 수정 요청 1회 시도")
            repair_messages = self._build_messages(payload) + [{
                "role": "user",
                "content": (
                    "이전 응답이 JSON 스키마를 만족하지 않았다. routing_decision/priority/"
                    "audit_status는 반드시 정의된 값 중 하나여야 한다. 다른 텍스트 없이 "
                    "JSON 객체만 다시 출력하라."
                ),
            }]
            try:
                raw2 = self._call_with_retry(repair_messages)
                result = self._parse_and_validate(raw2)
            except UpstageCallError as e:
                logger.error("자가 수정 요청도 실패: %s", e)

        used_fallback = result is None
        if used_fallback:
            logger.error("최종 검증 실패 - 안전 기본값(FAIL / HUMAN_REVIEW)으로 폴백")
            result = SAFE_FALLBACK

        # 3) 결정론적 VIOLATION이 있으면 LLM 의견과 무관하게 FAIL로 강제 덮어쓴다.
        forced_fail = False
        if has_hard_violation and result.audit_status != AuditStatus.FAIL:
            forced_fail = True
            violation_summary = "; ".join(f.message for f in findings if f.severity == "VIOLATION")
            result = result.model_copy(update={
                "audit_status": AuditStatus.FAIL,
                "reason": f"[결정론적 검사에 의해 FAIL로 강제 조정됨] {violation_summary} | 원본 LLM 판단: {result.reason}",
            })
            logger.warning("LLM이 PASS를 냈으나 결정론적 VIOLATION이 있어 FAIL로 강제 덮어씀")

        self._log_decision(ctx, findings, result, used_fallback, forced_fail)
        return result.model_dump()

    # ---- 내부 구현 ----

    def _build_messages(self, payload: dict) -> list:
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

    def _call_with_retry(self, messages: list) -> str:
        last_err: Optional[Exception] = None
        structured_supported = True

        for attempt in range(1, self.config.max_retries + 1):
            try:
                kwargs = dict(model=self.config.model, messages=messages, temperature=self.config.temperature)
                if structured_supported:
                    kwargs["response_format"] = {"type": "json_schema", "json_schema": AUDIT_RESULT_JSON_SCHEMA}
                else:
                    kwargs["response_format"] = {"type": "json_object"}

                response = self.client.chat.completions.create(**kwargs)
                content = response.choices[0].message.content
                if not content:
                    raise UpstageCallError("빈 응답")
                return content

            except (APIConnectionError, APITimeoutError, RateLimitError) as e:
                last_err = e
                wait = min(2 ** attempt, 10)
                logger.warning("Upstage 일시적 오류 (%s) - %d/%d회차, %.1fs 후 재시도",
                              type(e).__name__, attempt, self.config.max_retries, wait)
                time.sleep(wait)

            except APIError as e:
                msg = str(e).lower()
                if structured_supported and ("response_format" in msg or "json_schema" in msg):
                    logger.warning("response_format(json_schema) 미지원으로 판단 - json_object 모드로 폴백")
                    structured_supported = False
                    continue
                last_err = e
                logger.error("Upstage API 오류: %s", e)
                time.sleep(min(2 ** attempt, 10))

        raise UpstageCallError(f"{self.config.max_retries}회 재시도 후에도 실패: {last_err}")

    def _parse_and_validate(self, raw: str) -> Optional[AuditResult]:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning("JSON 파싱 실패: %s | raw[:300]=%r", e, raw[:300])
            return None
        try:
            return AuditResult.model_validate(data)
        except ValidationError as e:
            logger.warning("스키마 검증 실패: %s", e)
            return None

    def _log_decision(
        self,
        ctx: dict,
        findings: List[DeterministicFinding],
        result: AuditResult,
        used_fallback: bool,
        forced_fail: bool,
    ) -> None:
        logger.info(
            "AUDIT_DECISION status=%s routing=%s priority=%s fallback=%s forced_fail=%s "
            "violations=%d warnings=%d judge_verdict=%s reason=%s",
            result.audit_status.value,
            result.routing_decision.value,
            result.priority.value,
            used_fallback,
            forced_fail,
            sum(1 for f in findings if f.severity == "VIOLATION"),
            sum(1 for f in findings if f.severity == "WARNING"),
            (ctx.get("debate_judge_result") or {}).get("verdict"),
            result.reason[:200],
        )


# --------------------------------------------------------------------------
# 데모 실행
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    api_key = os.environ.get("UPSTAGE_API_KEY")
    if not api_key:
        print("환경변수 UPSTAGE_API_KEY가 필요합니다.")
        sys.exit(1)

    agent = AuditAgent(AuditAgentConfig(api_key=api_key))

    # 케이스 1: 절차가 제대로 지켜진 정상 케이스
    normal_case = dict(
        parser_result={"cited_functions": ["Curl_parse_url"], "cited_commits": ["abc123"],
                       "cited_headers": [], "poc_present": False, "poc_code": None},
        fact_check_result={
            "function_check": [{"name": "Curl_parse_url", "exists": True, "location": "lib/url.c:123"}],
            "commit_check": [{"ref": "abc123", "exists": False, "reason": "저장소에 없음"}],
            "header_check": [], "poc_check": {"compilable": None, "compile_error": None},
        },
        dedup_result={"verdict": "NOT_DUPLICATE", "matches": []},
        debate_judge_result={"verdict": "NEEDS_MORE_EVIDENCE", "winning_side": "TIE",
                             "reason": "함수는 존재하나 커밋은 위조됨", "confidence": 0.6},
        vulnerable_agent_result={"evidence": ["Curl_parse_url 함수가 존재함"]},
        not_vulnerable_agent_result={"evidence": ["커밋 abc123은 존재하지 않음"]},
        workflow_status="RUNNING",
    )

    # 케이스 2: 절차 위반 + 역할 침범이 섞인 케이스 (강제 FAIL이 나와야 정상)
    violation_case = dict(
        parser_result={"cited_functions": ["Curl_parse_url", "fake_fn"], "cited_commits": [],
                       "cited_headers": [], "poc_present": False, "poc_code": None},
        fact_check_result={"function_check": [], "commit_check": [], "header_check": [],
                           "poc_check": {"compilable": None, "compile_error": None}},
        dedup_result={"verdict": "NOT_DUPLICATE", "matches": []},
        debate_judge_result={"verdict": "VALID_LIKELY", "winning_side": "VULNERABLE",
                             "reason": "함수가 존재함", "confidence": 0.9},
        vulnerable_agent_result={"evidence": ["fake_fn 함수가 존재함"]},
        not_vulnerable_agent_result={"evidence": []},
        workflow_status="AUTO_APPROVED",  # <- 원칙 1 위반 신호
    )

    for name, case in [("정상 케이스", normal_case), ("위반 케이스", violation_case)]:
        print(f"\n=== {name} ===")
        result = agent.run(**case)
        print(json.dumps(result, ensure_ascii=False, indent=2))
