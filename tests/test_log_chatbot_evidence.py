"""Verify the modal payload is the compact skill-filter output for this turn."""

import ast
import hashlib
import json
import re
import unittest
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MethodType, SimpleNamespace
from typing import List, Optional, Tuple


def load_agent_methods():
    root = Path(__file__).resolve().parents[1]
    source = root / "services" / "log_chatbot_service.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    agent_class = next(node for node in tree.body
                       if isinstance(node, ast.ClassDef) and node.name == "WifiLogAgentSystem")
    names = {
        "chat", "_remember_turn_skill_logs", "_attach_summary_key_logs",
        "_get_filtered_log_lines", "fetch_filtered_logs", "_extract_lines_from_filtered_blob",
        "_strip_line_number_prefix", "_normalize_time_message", "_compact_original_skill_lines",
        "_merge_wrapped_lines_if_needed", "_build_skill_focus_payload",
        "_build_tools", "_chat_simple",
    }
    methods = [node for node in agent_class.body
               if isinstance(node, ast.FunctionDef) and node.name in names]

    preprocess = root / "utils" / "log_parser_preprocess.py"
    utility_tree = ast.parse(preprocess.read_text(encoding="utf-8-sig"))
    utilities = [node for node in utility_tree.body if isinstance(node, ast.FunctionDef)]
    namespace = {
        "json": json, "hashlib": hashlib, "re": re, "Path": Path,
        "datetime": datetime, "ET": ET, "List": List, "Optional": Optional,
        "Tuple": Tuple, "defaultdict": defaultdict,
    }
    exec(compile(ast.Module(body=utilities, type_ignores=[]), str(preprocess), "exec"), namespace)
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"), namespace)
    return {name: namespace[name] for name in names}


class CompactSkillLogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.methods = load_agent_methods()

    def setUp(self):
        self.raw_lines = [
            "17\t10/27/2026-10:11:12.123 disconnect reason=4\r\n",
            "18\t10/27/2026-10:11:13.123 disconnect noise\r\n",
            "19\t10/27/2026-10:11:14.123 recovery succeeded\r\n",
        ]
        self.agent = SimpleNamespace(
            cancel_event=SimpleNamespace(clear=lambda: None),
            _reset_turn_usage=lambda: None,
            _scoped_log_lines=self.raw_lines,
            _raw_log_cache=self.raw_lines,
            _raw_log_cache_path="case.log",
            current_log_path="",
            _ensure_raw_log_cache=lambda: None,
            _filter_cache_by_skill={},
            _assembled_entries_by_key={},
            _assembled_entries_no_ts={},
            _assembled_log_text="",
            _detail_cache={},
            _detail_query_seen=set(),
            _driver_init_count=0,
            _driver_init_lines=[],
            _issue_time_window_lines=[],
            _export_assembled_log_file=lambda: "",
            _merge_lines_into_assembled_log=lambda lines, skill: (len(lines), len(lines)),
            _accumulate_turn_usage=lambda usage: None,
            skills={"wifi": SimpleNamespace(
                name="Wi-Fi", description="Connectivity", keywords=["disconnect"],
                exclusive=["noise"], tat_path=None, expert_rules=""),
                "recovery": SimpleNamespace(
                name="Recovery", description="Recovery state", keywords=["recovery"],
                exclusive=[], tat_path=None, expert_rules=""),
            },
            MAX_SKILL_FOCUS_LINES=320,
            MAX_SKILL_FOCUS_CHARS=16500,
            MAX_RECENT_SKILL_HISTORY_LINES=100,
            client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.complete))),
            model="test-model",
            conversation_history=[],
            issue_context={},
            last_turn_usage={},
        )
        for name, method in self.methods.items():
            setattr(self.agent, name, MethodType(method, self.agent))

    def complete(self, **kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="A concise answer."))],
            usage=None,
        )

    def test_filter_and_exclusion_preserve_original_modal_lines(self):
        output = self.agent.fetch_filtered_logs("wifi")
        logs = self.agent._attach_summary_key_logs({"type": "report", "data": {}})
        self.assertIn("Skill-Focused Reasoning Payload", output)
        self.assertEqual(self.agent._turn_skill_log_lines["wifi"], [self.raw_lines[0]])
        self.assertEqual(logs["key_logs_basis"], "skill_filtered_compact")
        self.assertEqual(logs["key_logs_status"], "selected")
        self.assertIn("=== Wi-Fi ===\n" + self.raw_lines[0], logs["key_logs"])
        self.assertIn("10/27/2026-10:11:12.123", logs["key_logs"])
        self.assertIn("17\t", logs["key_logs"])
        self.assertNotIn("noise", logs["key_logs"])
        self.assertNotIn("[L", logs["key_logs"])
        self.assertNotIn("recovery succeeded", logs["key_logs"])

    def test_repeated_fetch_uses_cached_compact_lines(self):
        self.agent.fetch_filtered_logs("wifi")
        self.agent._turn_skill_log_lines = {}
        self.agent.fetch_filtered_logs("wifi")
        result = self.agent._attach_summary_key_logs({"type": "report", "data": {}})
        self.assertIn(self.raw_lines[0], result["key_logs"])
        self.assertNotIn("[L", result["key_logs"])

    def test_only_skills_fetched_in_this_turn_are_in_modal(self):
        self.agent.fetch_filtered_logs("wifi")
        self.agent.fetch_filtered_logs("recovery")
        first = self.agent._attach_summary_key_logs({"type": "report", "data": {}})
        self.assertIn("=== Wi-Fi ===", first["key_logs"])
        self.assertIn("=== Recovery ===", first["key_logs"])
        self.assertIn(self.raw_lines[0], first["key_logs"])
        self.assertIn(self.raw_lines[2], first["key_logs"])

        self.agent._turn_skill_log_lines = {}
        second = self.agent._attach_summary_key_logs({"type": "text", "data": "Follow-up"})
        self.assertEqual(second["key_logs"], "")
        self.assertEqual(second["key_logs_status"], "no_evidence")

    def test_chat_resets_collection_before_each_turn(self):
        self.agent._turn_skill_log_lines = {"old": ["stale entry"]}
        self.agent._chat_with_tools = lambda *args, **kwargs: {
            "type": "report", "data": {"root_cause_summary": "New result"}}
        result = self.agent.chat("Analyze", use_tools=True)
        self.assertEqual(result["key_logs"], "")
        self.assertEqual(self.agent._turn_skill_log_lines, {})

    def test_simple_chat_does_not_require_json_citations(self):
        result = self.agent.chat("Explain", use_tools=False)
        self.assertEqual(result["data"], "A concise answer.")
        self.assertEqual(result["key_logs"], "")
        self.assertEqual(result["key_logs_status"], "no_evidence")

    def test_report_tool_schema_has_no_citation_id_field(self):
        schema = next(tool["function"]["parameters"] for tool in self.agent._build_tools()
                      if tool["function"]["name"] == "submit_final_report")
        self.assertNotIn("supporting_log_ids", schema["properties"])
        self.assertNotIn("supporting_log_ids", schema["required"])

    def test_cached_results_from_unused_skills_are_not_included(self):
        self.agent._filter_cache_by_skill["recovery"] = {
            "lines": ["<10:11:14.123> stale recovery line"], "line_count": 1,
        }
        result = self.agent._attach_summary_key_logs({"type": "report", "data": {}})
        self.assertEqual(result["key_logs"], "")

    def test_compaction_keeps_original_first_and_last_rows_verbatim(self):
        rows = [
            "1\t10/27/2026-10:11:12.123 disconnect reason=4\r\n",
            "2\t10/27/2026-10:11:12.223 disconnect reason=5\r\n",
            "3\t10/27/2026-10:11:12.323 disconnect reason=6\r\n",
        ]
        self.assertEqual(self.agent._compact_original_skill_lines(rows), [rows[0], rows[2]])

    def test_time_only_wrapped_entry_is_restored_verbatim(self):
        original_lines = [
            "10:11:12.123 disconnect reason=4\r\n",
            "\tcontinuation field=kept\r\n",
        ]
        self.agent._raw_log_cache = list(original_lines)
        self.agent._raw_log_cache_path = "time-only.log"
        self.agent._log_has_date = lambda: False
        self.agent._merge_wrapped_lines_if_needed()
        self.agent._scoped_log_lines = self.agent._raw_log_cache
        self.agent.skills["wifi"].keywords = ["disconnect"]
        self.agent._filter_cache_by_skill = {}
        self.agent.fetch_filtered_logs("wifi")
        result = self.agent._attach_summary_key_logs({"type": "report", "data": {}})
        self.assertIn("".join(original_lines), result["key_logs"])

    def test_tat_include_regex_exclude_and_disabled_rules_feed_modal(self):
        rows = [
            "1\t10/27/2026-10:11:12.123 disconnect reason=4\r\n",
            "2\t10/27/2026-10:11:13.123 disconnect test-drop\r\n",
            "3\t10/27/2026-10:11:14.123 recovery succeeded\r\n",
            "4\t10/27/2026-10:11:15.123 disabled marker\r\n",
        ]
        self.agent._scoped_log_lines = rows
        with TemporaryDirectory() as folder:
            tat_path = Path(folder) / "skill.tat"
            tat_path.write_text(
                '<filters>'
                '<filter enabled="y" text="disconnect" />'
                '<filter enabled="y" regex="y" text="^.*recovery.*$" />'
                '<filter enabled="y" excluding="y" text="test-drop" />'
                '<filter enabled="n" text="disabled" />'
                '</filters>',
                encoding="utf-8",
            )
            self.agent.skills["wifi"].tat_path = str(tat_path)
            filtered = self.agent._get_filtered_log_lines("wifi", apply_output_limit=False)
            self.assertIn("disconnect reason=4", filtered)
            self.assertNotIn("1\tdisconnect", filtered)
            self.agent.fetch_filtered_logs("wifi")

        result = self.agent._attach_summary_key_logs({"type": "report", "data": {}})
        self.assertIn(rows[0], result["key_logs"])
        self.assertIn(rows[2], result["key_logs"])
        self.assertNotIn(rows[1], result["key_logs"])
        self.assertNotIn(rows[3], result["key_logs"])

    def test_invalid_tat_xml_returns_no_filter_instead_of_raising(self):
        with TemporaryDirectory() as folder:
            tat_path = Path(folder) / "broken.tat"
            tat_path.write_text("<filters>", encoding="utf-8")
            self.agent.skills["wifi"].tat_path = str(tat_path)
            result = self.agent._get_filtered_log_lines("wifi")
        self.assertEqual(result, "No enabled filters available for this skill.")


if __name__ == "__main__":
    unittest.main()
