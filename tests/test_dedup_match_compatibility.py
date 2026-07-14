import unittest

from app import PipelineReport, _extract_audit_context
from audit_agent import DeterministicAuditChecks


class DedupMatchCompatibilityTest(unittest.TestCase):
    def test_accepts_current_dedup_match_shape(self):
        report = PipelineReport.model_validate({
            "report_id": "RPT-TEST",
            "workflow_status": "DROPPED",
            "input": {"raw_report_txt": "test"},
            "agent_results": {
                "dedup": {
                    "verdict": "POSSIBLE_DUPLICATE",
                    "matches": [{
                        "report_id": "h1_3702718",
                        "title": "Existing similar report",
                        "url": "https://example.test/report",
                        "semantic_similarity": 0.852637,
                        "best_similarity": 0.852637,
                        "exact_source_report_id_match": True,
                    }],
                }
            },
        })

        context = _extract_audit_context(report)
        match = context["dedup_result"]["matches"][0]

        self.assertEqual(match["similarity"], 0.852637)
        self.assertEqual(match["semantic_similarity"], 0.852637)
        self.assertIsNone(match["same_root_cause"])
        self.assertEqual(match["report_id"], "h1_3702718")
        findings = DeterministicAuditChecks().run(context)
        self.assertNotIn("INVALID_DEDUP_VERDICT", {finding.code for finding in findings})

    def test_preserves_legacy_dedup_match_shape(self):
        report = PipelineReport.model_validate({
            "report_id": "RPT-LEGACY",
            "workflow_status": "DEBATED",
            "input": {"raw_report_txt": "test"},
            "agent_results": {
                "dedup": {
                    "matches": [{
                        "title": "Legacy report",
                        "similarity": 0.91,
                        "same_root_cause": True,
                    }]
                }
            },
        })

        match = _extract_audit_context(report)["dedup_result"]["matches"][0]
        self.assertEqual(match["similarity"], 0.91)
        self.assertTrue(match["same_root_cause"])

    def test_dropped_workflow_accepts_missing_debate_as_skipped(self):
        report = PipelineReport.model_validate({
            "report_id": "RPT-DROPPED",
            "workflow_status": "DROPPED",
            "input": {"raw_report_txt": "test"},
            "agent_results": {"dedup": {"verdict": "DUPLICATE"}},
        })

        findings = DeterministicAuditChecks().run(_extract_audit_context(report))

        self.assertNotIn(
            "INCOMPLETE_JUDGE_STAGE", {finding.code for finding in findings}
        )

    def test_non_dropped_workflow_still_requires_debate(self):
        report = PipelineReport.model_validate({
            "report_id": "RPT-RUNNING",
            "workflow_status": "RUNNING",
            "input": {"raw_report_txt": "test"},
            "agent_results": {},
        })

        findings = DeterministicAuditChecks().run(_extract_audit_context(report))

        self.assertIn(
            "INCOMPLETE_JUDGE_STAGE", {finding.code for finding in findings}
        )

    def test_legacy_no_duplicate_verdict_is_normalized(self):
        report = PipelineReport.model_validate({
            "report_id": "RPT-LEGACY-ENUM",
            "workflow_status": "DROPPED",
            "input": {"raw_report_txt": "test"},
            "agent_results": {
                "dedup": {"verdict": "NO_DUPLICATE_FOUND"}
            },
        })

        context = _extract_audit_context(report)
        self.assertEqual(context["dedup_result"]["verdict"], "NO_MATCH")
        findings = DeterministicAuditChecks().run(context)
        self.assertNotIn(
            "INVALID_DEDUP_VERDICT", {finding.code for finding in findings}
        )

    def test_not_duplicate_alias_is_normalized(self):
        report = PipelineReport.model_validate({
            "report_id": "RPT-LEGACY-NOT-DUPLICATE",
            "workflow_status": "DROPPED",
            "input": {"raw_report_txt": "test"},
            "agent_results": {"dedup": {"verdict": "NOT_DUPLICATE"}},
        })

        context = _extract_audit_context(report)
        self.assertEqual(context["dedup_result"]["verdict"], "NO_MATCH")


if __name__ == "__main__":
    unittest.main()
