import json
import unittest
from unittest.mock import Mock

from pydantic import ValidationError

from audit_agent import AuditAgent, AuditAgentConfig, AuditResult


def result_payload(**updates):
    payload = {
        "routing_decision": "HUMAN_REVIEW",
        "priority": "NORMAL",
        "audit_status": "PASS",
        "reason": "검사 결과 원칙 위반이 발견되지 않았습니다.",
        "failure_codes": [],
        "key_points_for_human": [],
    }
    payload.update(updates)
    return payload


class AuditResultContractTest(unittest.TestCase):
    def test_pass_requires_empty_failure_codes(self):
        result = AuditResult.model_validate(result_payload())
        self.assertEqual(result.failure_codes, [])

        with self.assertRaises(ValidationError):
            AuditResult.model_validate(
                result_payload(failure_codes=["PROCEDURE_VIOLATION"])
            )

    def test_fail_requires_failure_code(self):
        with self.assertRaises(ValidationError):
            AuditResult.model_validate(result_payload(
                audit_status="FAIL", reason="절차 위반이 확인됨"
            ))

        result = AuditResult.model_validate(result_payload(
            audit_status="FAIL",
            reason="절차 위반이 확인됨",
            failure_codes=["PROCEDURE_VIOLATION"],
        ))
        self.assertEqual(result.failure_codes, ["PROCEDURE_VIOLATION"])

    def test_failure_codes_are_required(self):
        payload = result_payload()
        del payload["failure_codes"]
        with self.assertRaises(ValidationError):
            AuditResult.model_validate(payload)

    def test_reason_status_contradictions_are_rejected(self):
        with self.assertRaises(ValidationError):
            AuditResult.model_validate(result_payload(
                reason="절차 위반으로 판단됨"
            ))
        with self.assertRaises(ValidationError):
            AuditResult.model_validate(result_payload(
                audit_status="FAIL",
                reason="모든 단계가 정의된 원칙과 절차를 준수했습니다.",
                failure_codes=["UNKNOWN_FAILURE"],
            ))

    def test_contradiction_uses_existing_repair_retry(self):
        agent = AuditAgent(AuditAgentConfig(api_key="test"))
        contradictory = result_payload(reason="절차 위반으로 판단됨")
        repaired = result_payload(reason="검사 결과 문제 없음")
        agent._call_with_retry = Mock(side_effect=[
            json.dumps(contradictory, ensure_ascii=False),
            json.dumps(repaired, ensure_ascii=False),
        ])

        result = agent.run(
            parser_result={},
            fact_check_result={},
            dedup_result={"verdict": "NO_MATCH"},
            debate_judge_result={},
            workflow_status="DROPPED",
        )

        self.assertEqual(agent._call_with_retry.call_count, 2)
        self.assertEqual(result["audit_status"], "PASS")
        self.assertEqual(result["failure_codes"], [])

    def test_deterministic_violation_is_added_to_failure_codes(self):
        agent = AuditAgent(AuditAgentConfig(api_key="test"))
        agent._call_with_retry = Mock(return_value=json.dumps(
            result_payload(reason="검사 결과 문제 없음"), ensure_ascii=False
        ))

        result = agent.run(
            parser_result={},
            fact_check_result={},
            dedup_result={"verdict": "NO_MATCH"},
            debate_judge_result={},
            workflow_status="RUNNING",
        )

        self.assertEqual(result["audit_status"], "FAIL")
        self.assertIn("INCOMPLETE_JUDGE_STAGE", result["failure_codes"])


if __name__ == "__main__":
    unittest.main()
