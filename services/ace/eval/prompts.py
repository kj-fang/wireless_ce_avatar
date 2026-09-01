"""
Judge prompt + conclusion-tag inference table for the eval harness.
"""

# ---------------------------------------------------------------------------
# Conclusion-tag inference.
#
# submit_final_report has no conclusion_tag field, so deterministic tag
# scoring infers it from the report text. Conservative by design: a tag is
# only inferred when one of its keywords appears AND no other tag's keywords
# do (ambiguity -> "" -> the tag component simply drops out of the composite
# score). Keywords are matched case-insensitively as substrings.
# ---------------------------------------------------------------------------

TAG_KEYWORDS: dict[str, list[str]] = {
    "OS_INITIATED":        ["os-initiated", "os initiated", "initiated by the os",
                            "windows initiated", "user initiated disconnect"],
    "RF_INTERFERENCE":     ["rf interference", "interference", "noisy environment",
                            "poor rssi", "weak signal", "signal strength degrad"],
    "AP_KICK":             ["deauth from ap", "ap-initiated disconnect", "ap initiated",
                            "ap kicked", "disassoc from ap", "kicked by the ap",
                            "deauthenticated by the ap"],
    "FIRMWARE_CRASH":      ["firmware crash", "fw crash", "fw assert", "firmware assert",
                            "umac assert", "lmac assert", "microcode"],
    "MCC_MISMATCH":        ["mcc mismatch", "country code mismatch", "regulatory mismatch",
                            "mcc update"],
    "DRIVER_INIT_FAILURE": ["driver init fail", "driver initialization fail",
                            "adapter init fail", "failed to initialize the driver"],
    "AUTH_FAILURE":        ["authentication failure", "auth failure", "auth timeout",
                            "authentication timeout", "802.1x fail"],
    "ASSOC_FAILURE":       ["association failure", "assoc failure", "association timeout",
                            "assoc reject"],
    "HANDSHAKE_FAILURE":   ["handshake failure", "4-way handshake", "eapol timeout",
                            "key exchange fail"],
    "WAKE_RESUME_DELAY":   ["resume delay", "wake delay", "after resume", "s3 resume",
                            "s4 resume", "slow to reconnect after wake",
                            "delay after wake"],
    "BIOS_CONFIG_ISSUE":   ["bios config", "bios setting", "dsm table", "uefi variable",
                            "ppag table", "bios block"],
    "ROAMING_DECISION":    ["roaming decision", "roam decision", "roamed to", "roam to a",
                            "roaming event", "roam trigger"],
    # SoftAP / P2P keywords name the FAILURE, never the feature. A bare
    # "softap" / "p2p" co-occurs with a root-cause phrase in almost every real
    # report (and "IE_P2P" alone appears in ordinary STA scan logs), which
    # would make two families match and silently drop the tag component.
    "SOFTAP_START_FAILURE": ["softap failed to start", "softap start failure",
                             "hosted network failed to start",
                             "hotspot failed to start"],
    "P2P_CONNECT_FAILURE":  ["go negotiation fail", "group owner negotiation fail",
                             "p2p negotiation fail", "p2p connection fail",
                             "wi-fi direct connection fail", "wfd connection fail",
                             "p2p invitation fail"],
    # "OTHER" deliberately has no keywords — it is the fallback the scorer
    # never infers (ambiguity yields "" instead).
}


# ---------------------------------------------------------------------------
# Judge prompt (blind A/B).
#
# The harness randomizes which of before/after lands in slot A vs B, so the
# judge cannot systematically favor "the new one". Ground truth, when
# present, is authoritative; when absent, the recorded (human-voted) report
# is provided as a weak reference and agreement between A and B must yield a
# tie.
# ---------------------------------------------------------------------------

JUDGE_PROMPT = """\
You are a senior Intel Wi-Fi triage reviewer. Two AI diagnoses of the SAME
customer case are shown below as Report A and Report B (their order is
random). Score how well each matches the ground truth and decide the winner.

Judging rules, in priority order:
 1. Factual agreement with the GROUND TRUTH block (when present, it came
    from the human engineer who resolved the case and is authoritative).
 2. Evidence quality: does the report cite concrete log lines consistent
    with the ground-truth evidence? Fabricated or vague evidence is a fault.
 3. Actionability of the recommendations.
Rules you must follow:
 - Verbosity is NOT quality. A short correct diagnosis beats a long wrong one.
 - Never reward a confidently wrong conclusion.
 - If the two reports reach the SAME conclusion, the winner is "tie" unless
   one has clearly better evidence.
 - If the GROUND TRUTH block is empty AND both reports broadly agree,
   output "tie".
 - root_cause_match_*: 0.0 = contradicts ground truth, 0.5 = partially
   consistent, 1.0 = same root cause (wording may differ).
 - inferred_tag_*: the conclusion tag each report implies, chosen from:
   {tag_universe}
   Use "" if unclear.

=== CASE CONTEXT ===
{case_context}

=== GROUND TRUTH (authoritative when non-empty) ===
correct_root_cause:     {correct_root_cause}
correct_conclusion_tag: {correct_conclusion_tag}
correct_skill:          {correct_skill}
evidence_log_lines:
{evidence_log_lines}

=== RECORDED HUMAN-VOTED REPORT (weak reference; vote={vote}) ===
{baseline_excerpt}

=== REPORT A ===
{report_a}

=== REPORT B ===
{report_b}

Output ONLY a valid JSON object (no markdown, no code fences):

{{
  "winner": "A|B|tie",
  "root_cause_match_a": 0.0,
  "root_cause_match_b": 0.0,
  "evidence_quality_a": 0.0,
  "evidence_quality_b": 0.0,
  "inferred_tag_a": "",
  "inferred_tag_b": "",
  "rationale": "<2-4 sentences citing the concrete differences>"
}}
"""


def fill_judge_prompt(**fields) -> str:
    return JUDGE_PROMPT.format(**fields)
