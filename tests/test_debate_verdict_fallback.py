import unittest

from app import PipelineReport, _extract_audit_context
from audit_agent import DeterministicAuditChecks


class DebateVerdictFallbackTest(unittest.TestCase):
    def test_uses_numeric_top_level_verdict_when_judge_is_empty(self):
        report = PipelineReport.model_validate({
            "report_id": "RPT-TEST",
            "workflow_status": "DEBATED",
            "input": {"raw_report_txt": "test"},
            "agent_results": {
                "debate": {
                    "judge": {
                        "verdict": None,
                        "winning_side": None,
                        "reason": None,
                        "confidence": None,
                        "next_step": None,
                    },
                    "verdict": {
                        "verdict": 0,
                        "winning_side": "VULNERABLE",
                        "reason": "verified",
                        "confidence": 0.76,
                        "next_step": "SECURITY_TEAM_ESCALATION",
                    },
                }
            },
        })

        context = _extract_audit_context(report)

        self.assertEqual(context["debate_judge_result"]["verdict"], 0)
        self.assertEqual(context["debate_judge_result"]["confidence"], 0.76)
        findings = DeterministicAuditChecks().run(context)
        self.assertNotIn("INCOMPLETE_JUDGE_STAGE", {finding.code for finding in findings})

    def test_non_empty_judge_fields_override_verdict_fallback(self):
        report = PipelineReport.model_validate({
            "report_id": "RPT-TEST",
            "workflow_status": "DEBATED",
            "input": {"raw_report_txt": "test"},
            "agent_results": {
                "debate": {
                    "judge": {"confidence": 0.9},
                    "verdict": {
                        "verdict": "FALSE_POSITIVE_LIKELY",
                        "confidence": 0.7,
                        "reason": "fallback reason",
                    },
                }
            },
        })

        judge = _extract_audit_context(report)["debate_judge_result"]

        self.assertEqual(judge["verdict"], "FALSE_POSITIVE_LIKELY")
        self.assertEqual(judge["confidence"], 0.9)
        self.assertEqual(judge["reason"], "fallback reason")


if __name__ == "__main__":
    unittest.main()
