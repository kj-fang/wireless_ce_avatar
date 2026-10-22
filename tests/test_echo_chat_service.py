import unittest
from unittest.mock import patch

from services import echo_chat_service as echo


class EchoPromptTests(unittest.TestCase):
    def test_keyword_prompt_includes_issue_context_and_json_contract(self):
        prompt = echo.build_echo_keyword_question(
            "Wi-Fi disconnect", "Disconnects after roaming", "10/14/2026-11:10:56",
        )

        self.assertIn("Title: Wi-Fi disconnect", prompt)
        self.assertIn("Issue time: 10/14/2026-11:10:56", prompt)
        self.assertIn("Issue description:\nDisconnects after roaming", prompt)
        self.assertIn('{"keywords":["...","..."]}', prompt)
        self.assertIn("You may infer plausible terms", prompt)
        self.assertIn("Do not search or use Wiki pages", prompt)
        self.assertIn("Prefer specific technical terms over broad generic words", prompt)
        self.assertIn("wildcards.\n\nTitle:", prompt)
        self.assertNotIn("confidence_score", prompt)

    def test_filter_gap_prompt_contains_avatar_filters_and_filtered_log(self):
        prompt = echo.build_filter_gap_question(
            title="Scan failure",
            description="Cannot scan 6 GHz",
            issue_time="10/16/2026-10:00:00",
            issue_time_window_minutes=5,
            report={"markdown_summary": "Avatar saw no scan request"},
            skill_filters=[{"skill_name": "Roaming", "keywords": ["ROAM_START"],
                            "exclusive": ["DEBUG_NOISE"]}],
            filtered_logs="AVATAR_FILTERED_LOG_SENTINEL",
        )

        self.assertIn("ROAM_START", prompt)
        self.assertIn("DEBUG_NOISE", prompt)
        self.assertIn("AVATAR_FILTERED_LOG_SENTINEL", prompt)
        self.assertIn('"additional_keywords"', prompt)
        self.assertIn('"missing_time_ranges"', prompt)
        self.assertIn("+/-5 minutes", prompt)

    def test_gap_parser_deduplicates_existing_keywords_without_log_validation(self):
        import json

        answer = json.dumps({
            "additional_keywords": ["ROAM_START", "AUTH_FAIL", "AUTH_FAIL"],
            "missing_time_ranges": [
                {"start": "10/16/2026-10:01:00", "end": "10/16/2026-10:03:00"},
                {"start": "bad", "end": "time"},
            ],
        })
        parsed = echo.parse_echo_filter_gaps(answer, ["ROAM_START"])

        self.assertTrue(parsed["parse_success"])
        self.assertEqual(parsed["additional_keywords"], ["AUTH_FAIL"])
        self.assertEqual(len(parsed["missing_time_ranges"]), 1)

    def test_final_prompt_contains_selected_keywords_and_filtered_evidence(self):
        prompt = echo.build_echo_log_analysis_question(
            "Disconnect", "Link drops", "11:10:56", ["AUTH_FAIL"], "AUTH_FAIL status=2",
        )

        self.assertIn('Selected keywords: ["AUTH_FAIL"]', prompt)
        self.assertIn("AUTH_FAIL status=2", prompt)
        self.assertIn("## Echo Keyword Evidence", prompt)
        self.assertIn("Do not include a confidence score", prompt)
        sections = ("# Executive Summary", "| Aspect | Finding |", "## Timeline",
                "## Recommendations", "## Echo Keyword Evidence")
        self.assertEqual([prompt.index(section) for section in sections],
                 sorted(prompt.index(section) for section in sections))
        self.assertIn("- T-Ns: Trigger Event (The Source)", prompt)
        self.assertIn("- T+0s: Physical Failure begins", prompt)
        self.assertIn("- T+Ns: Final Termination", prompt)

    def test_gap_prompt_contains_filtered_evidence_and_active_filters(self):
        prompt = echo.build_filter_gap_question(
            title="Scan failure",
            description="Cannot scan 6 GHz",
            issue_time="10/16/2026-10:00:00",
            issue_time_window_minutes=5,
            report={"markdown_summary": "Avatar report"},
            skill_filters=[{"skill_name": "Connectivity", "keywords": ["SCAN_START"],
                            "exclusive": ["DEBUG_NOISE"]}],
            filtered_logs="AVATAR_FILTERED_LOG",
        )
        self.assertIn("AVATAR_FILTERED_LOG", prompt)
        self.assertIn("SCAN_START", prompt)
        self.assertIn("DEBUG_NOISE", prompt)
        self.assertIn("missing_time_ranges", prompt)

    def test_gap_parser_deduplicates_existing_keywords_and_parses_ranges(self):
        import json
        answer = json.dumps({
            "additional_keywords": ["SCAN_START", "AUTH_FAIL", "AUTH_FAIL"],
            "missing_time_ranges": [{
                "start": "10/16/2026-10:01:00",
                "end": "10/16/2026-10:03:00",
            }],
        })
        parsed = echo.parse_echo_filter_gaps(answer, ["SCAN_START"])
        self.assertTrue(parsed["parse_success"])
        self.assertEqual(parsed["additional_keywords"], ["AUTH_FAIL"])
        self.assertEqual(len(parsed["missing_time_ranges"]), 1)


class EchoParsingTests(unittest.TestCase):
    def test_keyword_parser_unwraps_json_and_drops_timestamps(self):
        answer = '{"keywords" : ["AUTH_FAIL", "11:10:56", "AUTH_FAIL"]}'

        parsed = echo.parse_keywords_from_text(answer)

        self.assertEqual(parsed["keywords"], [{"keyword": "AUTH_FAIL"}])

    def test_keyword_parser_accepts_fenced_json(self):
        parsed = echo.parse_keywords_from_text(
            'Suggested terms:\n```json\n{"keywords":["ROAM_START"]}\n```'
        )

        self.assertEqual(parsed["keywords"], [{"keyword": "ROAM_START"}])

    def test_report_parser_unwraps_mcp_answer_and_omits_confidence(self):
        import json

        inner = json.dumps({
            "root_cause_summary": "Driver reset",
            "recommended_actions": ["Collect a firmware trace"],
            "involved_skills": ["Echo"],
            "markdown_summary": "# Executive Summary\n\n## PHASE 1\nEvidence",
            "confidence_score": 94,
        })
        parsed = echo.parse_echo_report(json.dumps({"answer": inner}))

        self.assertEqual(parsed["root_cause_summary"], "Driver reset")
        self.assertEqual(parsed["recommended_actions"], ["Collect a firmware trace"])
        self.assertTrue(parsed["markdown_summary"].startswith("# Executive Summary"))
        self.assertNotIn("confidence_score", parsed)

    def test_report_parser_unwraps_plain_markdown_answer(self):
        markdown = "# Executive Summary\n\n## PHASE 1\nEvidence"

        parsed = echo.parse_echo_report('{"answer": ' + repr(markdown).replace("'", '"') + "}")

        self.assertEqual(parsed["markdown_summary"], markdown)
        self.assertEqual(parsed["analysis_source"], "echo")

    def test_report_parser_preserves_unstructured_echo_text(self):
        answer = "# Executive Summary\nEcho's free-form answer"

        parsed = echo.parse_echo_report(answer)

        self.assertEqual(parsed["markdown_summary"], answer)

    def test_report_parser_extracts_labeled_fields_from_wrapped_answer(self):
        import json

        answer = json.dumps({"answer": (
            "root_cause_summary: Driver reset\n"
            "recommended_actions:\n- Collect a firmware trace\n"
            "markdown_summary:\n# Executive Summary\nDriver reset observed.\n"
            "## Timeline\n- T+0s: Reset"
        )})

        parsed = echo.parse_echo_report(answer)

        self.assertEqual(parsed["root_cause_summary"], "Driver reset")
        self.assertEqual(parsed["recommended_actions"], ["Collect a firmware trace"])
        self.assertTrue(parsed["markdown_summary"].startswith("# Executive Summary"))
        self.assertNotIn("{\"answer\"", parsed["markdown_summary"])


class EchoFilterTests(unittest.TestCase):
    def test_keyword_hit_count_counts_case_insensitive_raw_lines(self):
        hits = echo.count_keyword_hits(
            ["AUTH_FAIL status=1", "auth_fail status=2", "AUTH_OK"],
            ["AUTH_FAIL"],
        )
        self.assertEqual(hits["per_keyword"]["AUTH_FAIL"], 2)

    def test_filter_uses_only_selected_keywords_on_raw_lines(self):
        raw_lines = [
            "01 AUTH_FAIL status=2",
            "02 ROAM_START ap=aa",
            "03 auth_fail retry=1",
        ]

        result = echo.filter_raw_log_for_echo(raw_lines, ["AUTH_FAIL"])

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["per_keyword"]["AUTH_FAIL"], 2)
        self.assertIn("AUTH_FAIL", result["filtered_log"])
        self.assertNotIn("ROAM_START", result["filtered_log"])

    def test_filter_reports_zero_matches(self):
        result = echo.filter_raw_log_for_echo(["AUTH_FAIL status=2"], ["MISSING_EVENT"])

        self.assertEqual(result["total"], 0)
        self.assertIn("No matching log lines", result["filtered_log"])

    def test_filter_caps_payload_and_adds_warning(self):
        with patch.object(echo, "group_similar_logs", return_value=["MATCH " + "x" * 20000]):
            result = echo.filter_raw_log_for_echo(["MATCH event"], ["MATCH"])

        self.assertLessEqual(len(result["filtered_log"]), echo.MAX_ECHO_FILTER_CHARS)
        self.assertTrue(any("truncated" in warning for warning in result["warnings"]))

    def test_filter_keeps_late_issue_time_hits_before_early_hits(self):
        raw_lines = [
            f"10/15/2026-09:00:{index % 60:02d}.000 AUTH_FAIL early={index} " + "x" * 100
            for index in range(180)
        ] + ["10/15/2026-11:10:56.000 AUTH_FAIL near_issue"]

        result = echo.filter_raw_log_for_echo(
            raw_lines, ["AUTH_FAIL"], issue_time="10/15/2026-11:10:56"
        )

        self.assertEqual(result["total"], 181)
        self.assertLessEqual(len(result["filtered_log"]), echo.MAX_ECHO_FILTER_CHARS)
        self.assertIn("AUTH_FAIL near_issue", result["filtered_log"])
        self.assertTrue(any("issue time" in warning for warning in result["warnings"]))

    def test_filter_uses_time_of_day_for_time_only_logs(self):
        raw_lines = [f"09:00:{index % 60:02d} AUTH_FAIL early={index} " + "x" * 100
                     for index in range(180)]
        raw_lines.append("<23:59:58.000> AUTH_FAIL near_midnight")

        result = echo.filter_raw_log_for_echo(
            raw_lines, ["AUTH_FAIL"], issue_time="00:00:01"
        )

        self.assertIn("AUTH_FAIL near_midnight", result["filtered_log"])

    def test_filter_prefers_same_date_over_matching_clock_on_prior_day(self):
        raw_lines = [
            f"10/14/2026-11:10:56.000 AUTH_FAIL previous_day={index} " + "x" * 100
            for index in range(180)
        ] + ["10/15/2026-11:08:00.000 AUTH_FAIL same_day"]

        result = echo.filter_raw_log_for_echo(
            raw_lines, ["AUTH_FAIL"], issue_time="10/15/2026-11:10:56"
        )

        self.assertIn("AUTH_FAIL same_day", result["filtered_log"])

    def test_union_filter_combines_avatar_echo_keywords_and_times_then_excludes(self):
        raw = [
            "10/16/2026-10:00:00.000 AUTH_FAIL avatar_baseline",
            "10/16/2026-10:01:00.000 AUTH_FAIL DEBUG_NOISE baseline_excluded",
            "10/16/2026-10:02:00.000 ROAM_START echo_keyword_only",
            "10/16/2026-10:04:00.000 AUTH_FAIL issue_window_only",
            "10/16/2026-10:08:00.000 TX_GAP ROAM_START echo_keyword_and_range",
            "10/16/2026-10:08:30.000 HEARTBEAT echo_range_only",
            "10/16/2026-10:09:00.000 ROAM_START DEBUG_NOISE candidate_excluded",
            "10/16/2026-12:00:00.000 AUTH_FAIL outside_scope",
        ]
        result = echo.build_avatar_union_filter(
            raw,
            [{"skill_name": "Connectivity", "keywords": ["AUTH_FAIL"],
              "exclusive": ["DEBUG_NOISE"]}],
            ["ROAM_START"],
            [{"start": "10/16/2026-10:07:00", "end": "10/16/2026-10:09:30"}],
            "10/16/2026-10:00:00",
            baseline_lines=[raw[0], raw[1]],
        )
        for evidence in ("avatar_baseline", "echo_keyword_and_range"):
            self.assertIn(evidence, result["filtered_log"])
        for dropped in ("baseline_excluded", "echo_keyword_only", "issue_window_only",
                        "echo_range_only", "candidate_excluded", "outside_scope"):
            self.assertNotIn(dropped, result["filtered_log"])
        self.assertEqual(result["avatar_filtered_lines"], 1)
        self.assertEqual(result["union_raw_lines"], 2)
        self.assertEqual(result["new_raw_lines"], 1)
        self.assertEqual(result["exclusive_removed_lines"], 2)
        diagnostics = result["diagnostics"]
        self.assertEqual(diagnostics["baseline_before_exclusive"], 2)
        self.assertEqual(diagnostics["baseline_removed_by_exclusive"], 1)
        self.assertEqual(diagnostics["baseline_after_exclusive"], 1)
        self.assertEqual(diagnostics["echo_keyword_lines"], 3)
        self.assertEqual(diagnostics["echo_time_lines"], 3)
        self.assertEqual(diagnostics["echo_keyword_and_time_candidates"], 2)
        self.assertEqual(diagnostics["candidates_in_baseline"], 0)
        self.assertEqual(diagnostics["candidates_outside_baseline"], 2)
        self.assertEqual(diagnostics["candidates_removed_by_exclusive"], 1)
        self.assertEqual(diagnostics["new_lines_after_exclusive"], 1)
        self.assertEqual(diagnostics["new_lines_selected_for_avatar"], 1)
        echo_keyword = diagnostics["echo_keywords"][0]
        self.assertEqual(
            (echo_keyword["raw_matches"], echo_keyword["within_time"], echo_keyword["in_baseline"],
             echo_keyword["exclusive_removed"], echo_keyword["new_union"], echo_keyword["new_selected"]),
            (3, 2, 0, 1, 1, 1),
        )
        echo_range = diagnostics["echo_time_ranges"][0]
        self.assertEqual(
            (echo_range["time_matches"], echo_range["keyword_matches"], echo_range["in_baseline"],
             echo_range["exclusive_removed"], echo_range["new_union"], echo_range["new_selected"]),
            (3, 2, 0, 1, 1, 1),
        )

    def test_union_filter_clamps_echo_ranges_to_log_bounds(self):
        raw = ["10/16/2026-10:00:00.000 START", "10/16/2026-10:10:00.000 END"]
        result = echo.build_avatar_union_filter(
            [raw[0].replace("START", "MATCH START"), raw[1]],
            [], ["MATCH"],
            [{"start": "10/16/2026-09:00:00", "end": "10/16/2026-10:05:00"}],
            "",
        )
        self.assertEqual(result["union_raw_lines"], 1)
        self.assertEqual(result["applied_time_ranges"][0]["start"], "10/16/2026-10:00:00")

    def test_union_budget_prioritizes_original_avatar_lines(self):
        range_lines = [
            "10/16/2026-10:00:10.000 TX_GAP " + "A" * 120,
            "10/16/2026-10:00:20.000 TX_GAP " + "B" * 120,
        ]
        baseline = "10/16/2026-09:00:00.000 AUTH_FAIL original_baseline"
        result = echo.build_avatar_union_filter(
            range_lines + [baseline],
            [{"skill_name": "Connectivity", "keywords": ["AUTH_FAIL"], "exclusive": []}],
            ["TX_GAP"],
            [{"start": "10/16/2026-10:00:00", "end": "10/16/2026-10:00:59"}],
            "",
            baseline_lines=[baseline],
            max_chars=700,
        )
        self.assertEqual(result["sent_avatar_baseline_lines"], 1)
        self.assertEqual(result["omitted_avatar_baseline_lines"], 0)
        self.assertEqual(result["sent_new_raw_lines"], 1)
        self.assertIn("original_baseline", result["filtered_log"])

        baseline_only = echo.build_avatar_union_filter(
            range_lines + [baseline],
            [{"skill_name": "Connectivity", "keywords": ["AUTH_FAIL"], "exclusive": []}],
            ["TX_GAP"],
            [{"start": "10/16/2026-10:00:00", "end": "10/16/2026-10:00:59"}],
            "",
            baseline_lines=[baseline],
            max_chars=500,
        )
        self.assertEqual(baseline_only["new_raw_lines"], 2)
        self.assertEqual(baseline_only["sent_new_raw_lines"], 0)

    def test_avatar_union_default_budget_accepts_more_than_16k_chars(self):
        raw_lines = [
            f"10/16/2026-10:00:{index % 60:02d}.000 AUTH_FAIL event_id={index:04d} payload="
            + ("x" * 80)
            for index in range(300)
        ]
        result = echo.build_avatar_union_filter(
            raw_lines,
            [{"skill_name": "Connectivity", "keywords": ["AUTH_FAIL"], "exclusive": []}],
            [], [], "",
            baseline_lines=raw_lines,
        )
        self.assertEqual(echo.MAX_AVATAR_UNION_CHARS, 200_000)
        self.assertEqual(result["sent_raw_lines"], len(raw_lines))
        self.assertFalse(result["truncated"])

    def test_issue_window_and_avatar_keyword_do_not_add_lines_without_echo_match(self):
        raw = [
            "10/16/2026-09:00:00.000 AUTH_FAIL original_baseline",
            "10/16/2026-10:00:00.000 AUTH_FAIL exact_issue_time",
        ]
        result = echo.build_avatar_union_filter(
            raw,
            [{"skill_name": "Connectivity", "keywords": ["AUTH_FAIL"], "exclusive": []}],
            [], [],
            "10/16/2026-10:00:00.000",
            baseline_lines=[raw[0]],
        )
        self.assertIn("original_baseline", result["filtered_log"])
        self.assertNotIn("exact_issue_time", result["filtered_log"])
        self.assertEqual(result["avatar_filtered_lines"], 1)
        self.assertEqual(result["new_raw_lines"], 0)

    def test_time_match_alone_does_not_include_a_line_without_keywords(self):
        raw = [
            "10/16/2026-10:01:00.000 AUTH_FAIL original_baseline",
            "10/16/2026-10:02:00.000 HEARTBEAT only_time_match",
            "10/16/2026-10:20:00.000 ROAM_START only_keyword_match",
        ]
        result = echo.build_avatar_union_filter(
            raw,
            [{"skill_name": "Connectivity", "keywords": ["AUTH_FAIL"], "exclusive": []}],
            ["ROAM_START"],
            [{"start": "10/16/2026-10:00:00", "end": "10/16/2026-10:05:00"}],
            "10/16/2026-10:00:00",
            baseline_lines=[raw[0]],
        )
        self.assertIn("original_baseline", result["filtered_log"])
        self.assertNotIn("only_time_match", result["filtered_log"])
        self.assertNotIn("only_keyword_match", result["filtered_log"])
        self.assertEqual(result["new_raw_lines"], 0)

    def test_echo_keyword_without_time_range_adds_no_lines(self):
        raw = [
            "10/16/2026-09:00:00.000 AUTH_FAIL original_baseline",
            "10/16/2026-10:00:00.000 ROAM_START keyword_without_range",
        ]
        result = echo.build_avatar_union_filter(
            raw,
            [{"skill_name": "Connectivity", "keywords": ["AUTH_FAIL"], "exclusive": []}],
            ["ROAM_START"], [],
            "10/16/2026-10:00:00.000",
            baseline_lines=[raw[0]],
        )
        self.assertNotIn("keyword_without_range", result["filtered_log"])
        self.assertEqual(result["new_raw_lines"], 0)

    def test_baseline_is_exactly_the_supplied_lines_not_rebuilt_from_avatar_keywords(self):
        raw = [
            "10/16/2026-10:00:00.000 wl|ext_roam_fail already_seen",
            "10/16/2026-10:00:10.000 [E] wl|ext_roam_fail not_seen_by_avatar",
            "10/16/2026-10:01:00.000 other_line",
        ]
        result = echo.build_avatar_union_filter(
            raw,
            [{"skill_name": "Connectivity", "keywords": ["[E]", "wl|ext"], "exclusive": []}],
            ["roam_fail"],
            [{"start": "10/16/2026-10:00:00", "end": "10/16/2026-10:00:30"}],
            "",
            baseline_lines=[raw[0]],
        )
        self.assertEqual(result["avatar_filtered_lines"], 1)
        self.assertEqual(result["new_raw_lines"], 1)
        self.assertIn("not_seen_by_avatar", result["filtered_log"])
        echo_keyword = result["diagnostics"]["echo_keywords"][0]
        self.assertEqual((echo_keyword["within_time"], echo_keyword["in_baseline"]), (2, 1))


class EchoTransportTests(unittest.TestCase):
    def test_ask_echo_can_be_tested_without_network(self):
        async def fake_ask_async(question, url, timeout, use_file_search, box):
            self.assertEqual(question, "local test")
            self.assertTrue(use_file_search)
            box["answer"] = "mocked Echo answer"

        with patch.object(echo, "_ask_async", new=fake_ask_async):
            answer = echo.ask_echo("local test", use_file_search=True)

        self.assertEqual(answer, "mocked Echo answer")


if __name__ == "__main__":
    unittest.main()
