# Sub-task 03: Chat domain row and UI config

Status: open. Folder: [README.md](README.md). Progress: [PROGRESS.md](PROGRESS.md).
Depends on: none.

## Goal

Register `generic` as a known chat-history/feedback domain (so it never silently files under the
Wi-Fi default — see the warning mechanism in `configs/chat_domains.py:120-140`), and give the
Generic profile its own UI config dict. Also correct the Linux profile's displayed title, which is
the only edit to existing Linux data this workpad makes.

## Where

- `configs/chat_domains.py`:
  - `DOMAINS` tuple (~line 70, after the `linux` row) — add:
    ```python
    ChatDomain(
        key="generic",
        aliases=("generic",),
        history_folder="generic_history",
        history_prefix="generic-",
        feedback_prefix="generic_",
    ),
    ```
  - `AGENT_DOMAINS` (line 82) — add `"generic"`: `frozenset({"wifi", "nw", "bt", "linux", "generic"})`.
  - `CASE_TECHNOLOGY_AGENTS` (line 102) — leave unchanged (do NOT add `"generic"`): a
    generic-agent session says nothing about case technology, same documented reasoning as for
    `linux` (lines 91-101).
- `configs/chatbot_ui.py`:
  - `LINUX_UI` (line 248) — change `"title": "Generic Log Analysis Agent"` → `"title": "Linux Log
    Chatbot"` (line 255). Trim the comment block above it (lines 251-254, "What this agent is
    good at comes from its prompt and its skills... so the page does not name one domain in its
    chrome") since it will now openly be Linux-branded — replace with a short note that this is
    the Linux-Wi-Fi-driver-flavored profile, distinct from the new Generic one. No other field in
    `LINUX_UI` changes.
  - Add a new `GENERIC_UI` dict (new, placed after `LINUX_UI`), modeled on it:
    - `"domain": "generic"`, `"api": "/generic_chatbot"`,
      `"title": "Generic Log Analysis Agent"` (the title vacated from `LINUX_UI`),
      `"back_url": "/generic_chatbot/back_to_avatar"`,
      `"input_placeholder": "Describe the issue and ask a question about the log."`.
    - Pick accent colors not already used by another profile (Wi-Fi blue `#0071c5`, BT's own,
      Linux green `#15803d`) — e.g. a neutral slate (`"accent": "#475569"`,
      `"accent_dark": "#334155"`, `"accent_light": "#64748b"`, with matching
      `accent_soft`/`accent_border`/`accent_hover`/`accent_rgb`) to visually signal "unbranded".
    - `"event_sources"`, `"event_source_default"`, `"windows_event_log": False`,
      `"feedback_domain": "generic"`, `"feedback_conclusions": WIFI_FEEDBACK_CONCLUSIONS`
      (same placeholder Linux uses today — see `LINUX_UI`, line 273),
      `"allow_modified_yaml_upload": False`, `"history_reset_before_set_log": True`.
    - Reuse Linux's scripts verbatim (Linux itself reuses Wi-Fi's — no new JS/CSS needed):
      `"stylesheets": ["/static/chatbot/css/full-agent.css"]`,
      `"runtime_strategy_script": "/static/chatbot/js/strategies/wifi-chat-runtime.js"`,
      `"profile_script": "/static/chatbot/js/profiles/wifi.js"`,
      `"template_parts": {"sidebar": "chatbot/profiles/_sidebar_customer_clock.html", "runtime": "chatbot/features/_issue_time_runtime.html"}`.
    - `"issue_time"` block: same shape as Linux's (lines 283-300) — `"allow_time_only": True`,
      `"customer_timezone": False` (no decode step), `"event_refinement": False`,
      `"multi_select": True`, with `prompt_title`/`prompt_body` text generalized away from
      Linux-specific wording if Linux's happens to be generic enough already (check first).
    - `"features"` block: same as Linux's (lines 301-313) — `sidebar_toggle`, `issue_time`,
      `feedback`, `history`, `skill_editor`, `skill_append`, `prompt_editor` all `True`.

## Steps

1. Add the `generic` row to `DOMAINS` and `"generic"` to `AGENT_DOMAINS` in `chat_domains.py`.
2. Retitle `LINUX_UI` in `chatbot_ui.py`.
3. Add `GENERIC_UI` to `chatbot_ui.py`.

## Validate

- `python -c "from configs.chat_domains import normalize, history_folder, feedback_prefix; assert normalize('generic') == 'generic'; print(history_folder('generic'), feedback_prefix('generic'))"`
  prints `generic_history generic_`.
- `python -c "from configs.chat_domains import is_agent_domain; assert is_agent_domain('generic')"`
  succeeds.
- `python -c "from configs.chatbot_ui import LINUX_UI, GENERIC_UI; assert LINUX_UI['title'] == 'Linux Log Chatbot'; assert GENERIC_UI['title'] == 'Generic Log Analysis Agent'; assert GENERIC_UI['domain'] == 'generic'"`
  succeeds.
- Confirm no other `DOMAINS`/`AGENT_DOMAINS`/`CASE_TECHNOLOGY_AGENTS` entries were altered besides
  the one addition.

## Done when

The checks above pass and the PROGRESS.md row for 03 is updated.
