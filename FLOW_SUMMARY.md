# Log Chatbot — Entry Flow Summary

**Last updated:** 2026-06-16  
**App:** `wireless_ce_avatar` (Flask + SocketIO)

---

## Overview

There are four ways to reach the LLM analysis (`POST /log_chatbot/chat`).  
They differ in how much context is available, whether the analysis is automatic, and where the output JSON is saved.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Entry Points                                                               │
│                                                                             │
│  [1] Web UI (Salesforce)  → case lookup → download → download_result       │
│  [2a] index.html Upload   → native picker → upload_local_analysis          │
│  [2b] Chatbot Browse      → browse dialog → set_log (already-parsed only)  │
│  [3] SendTo               → right-click file → open_local_analysis         │
│  [4] CLI --auto-llm       → open_local_analysis + auto_llm=1 flag          │
└─────────────────────────────────────────────────────────────────────────────┘
```

All paths ultimately call `POST /log_chatbot/chat` with `use_tools=true`, which runs `WifiLogAgentSystem.chat()` in a background thread and streams results via SSE.

---

## Common Final Leg (all flows)

Once the agent's log path is set and context is primed, every flow ends here:

```
/log_chatbot/  (log_chatbot_routes.py → index())
  → renders log_chatbot.html
  → JS: tryAutoAnalyzeOnLoad()
      → if auto_run=analyze_all: clicks green "Analyze All" button
      → if auto_send=1: auto-submits (no user interaction)
      → else: waits for user to confirm issue time

  → sendMessage() → POST /log_chatbot/chat
      → chat() in log_chatbot_routes.py
          → _get_or_create_agent()        ← gets/creates WifiLogAgentSystem for session
          → issue_time parsed from sidebar data
          → _extract_issue_context()      ← reads session: case_context, ai_ips_analysis, classification
          → use_tools=True branch:
              → run_chat_with_tools() in background Thread
                  → WifiLogAgentSystem.chat()
                      → N reasoning steps (fetch_filtered_logs, lookup_assert_code, ...)
                      → LLM calls submit_final_report tool
                      → normalise recommended_actions/involved_skills to list
                  → step_queue.put(("done", result))
              → event_stream() generator (SSE)
                  → on "done":
                      → feedback_service.record_turn()   ← persist to sidecar JSON
                      → if result.type == "report":
                          → write llm_report_<ts>.json   ← auto-save to disk
                      → yield SSE done event
  → JS receives SSE "done" event
      → appendReport(result.data)         ← renders report card in UI
```

---

## Flow 1 — Web UI (Salesforce case)

**Trigger:** User opens the app in a browser, enters a case number, selects attachments, and clicks "Wi-Fi Analysis Agent" in download_result.

```
GET / → render_case_form()
  → renders index.html (Tab 1: case number input)

POST / → handle_case_submission()
  → CaseService.process_case(case_context)        ← Salesforce API fetch
  → session.clear()
  → session["case_context"] = case_context.to_session()
  → session["prompt_file_path"] = CaseService.load_case_summary_prompt(wifi_or_bt)
  → session["latest_etl_llm"] = False
  → redirect → GET /select_attachments

GET /select_attachments → render_select_attachments_form()
  → CaseContext.from_session(session["case_context"])
  → renders select_attachments.html

POST /select_attachments → handle_select_attachments_submission()
  → session["selected_files"] = [checked items from attachment_list]
  → _prime_issue_ai_cache(case_context)
      → organize_issue_context() via LLM             ← token-frugal pre-pass
      → session["_issue_ai_quick"] = {clean_description, issue_times}
  → session["bsod"] = (action == "bsod")
  → session["latest_etl_llm"] = (action in ["latest_etl_llm", "analysis"])
  → session["_run_analysis_requested"] = True  (if Run Analysis)
  → redirect → GET /download_attachments

GET /download_attachments → render_download_attachments_form()
  → CaseContext.from_session(session["case_context"])
  → _resolve_download_path(case_context, is_bsod)
  → session["download_path"] = download_path
  → renders attachment_download_progress.html
      → JS: attachment_download_service.download_attachments() (via SocketIO)
      → after download: JS redirect → GET /download_result

GET /download_result → render_download_result_form()
  → CaseContext.from_session(session["case_context"])
  → app_config.get_download_results(case_nbr)        ← {wifi, ddd, bt, fw} file dicts
  → get_issue_time_from_selected_files()             ← parse time from selected file names
  → filter_folders_by_time()                        ← filter ETL folders to issue window
  → get_auto_analysis_etl(wifi_dict, ddd_dict)       ← pick latest ETL (newest by number)
  → pick_etl_by_ai_time(file_dicts, llm_issue_time)  ← AI-time override if available
  → session.pop("_run_analysis_requested")
  → sendto_auto_llm = bool(session.get("sendto_auto_llm"))
  → renders download_result.html (with sendto_auto_llm, llm_issue_time, auto_analysis_etl)

  [User clicks "Wi-Fi Analysis Agent" button OR auto_analysis_etl fires]
  → JS: startLogParser(button, etlPathEncoded)
      → fetch GET /analysis_etl/process_etl_path?etl_path=...  (ETL parse, async)
      → wait for Socket.IO "wpp_complete" event
  → JS: handleWppComplete()
      → POST /log_chatbot/prepare  { etl_path }
          → prepare() in log_chatbot_routes.py
              → log_path = etl_path + ".log"
              → _extract_issue_context()              ← case_context + ai_ips_analysis + classification
              → app_config.last_analyzed_log_path = log_path
              → _get_or_create_agent(skip_prime=True)
              → agent.current_log_path = log_path
              → agent.reset_conversation()
              → agent.prime_with_context(**ctx)       ← inject case context into history
              → read_log_time_range(log_path)
              → _issue_context_organized()            ← cache organized context in session
              → _ensure_feedback_conversation_id(rotate=True)
              → feedback_service.ensure_conversation()
              → returns {"success": True}
      → JS redirect → /log_chatbot/?auto_run=analyze_all
          [+ &auto_send=1  if SENDTO_AUTO_LLM=true]

  → [Common Final Leg]
```

| Property | Value |
|---|---|
| **Context** | Full Salesforce: `case_nbr`, `subject`, `description`, `attachment_time`, `classification`, `_issue_ai_quick` |
| **Issue time source** | `_prime_issue_ai_cache` → `_issue_ai_quick.issue_times[0]` → sidebar prefill |
| **Auto-submit** | ❌ User confirms issue time unless `sendto_auto_llm=True` |
| **`sendto_auto_llm`** | `False` (unless CLI triggered this Salesforce flow, unlikely) |
| **JSON report saved to** | Folder of `current_log_path` (analysis workspace `.txt`) |

---

## Flow 2a — index.html "Local File Upload" tab

**Trigger:** User opens the app homepage, clicks the **"Local File Upload"** tab, picks a file, and clicks Submit.

```
GET / → render_case_form()
  → renders index.html  (Tab 2: Local File Upload)

  [User clicks upload bar]
  → JS: openLocalFileChooser()
      → POST /log_parser/pick_local_analysis_file
          → pick_local_analysis_file() in log_parser_routes.py
              → native Windows file dialog (tkinter)
              → session["picked_local_analysis_path"] = picked_path  ← security bind
              → returns { success, source_path, filename }
      → JS: selectedFiles = [{ file, sourcePath }]

  [User clicks Submit]
  → JS: submitLocalUpload()
      → POST /log_parser/upload_local_analysis  { source_path }
          → upload_local_analysis() in log_parser_routes.py
              → validate source_path == session["picked_local_analysis_path"]  ← CSRF guard
              → _is_allowed_local_analysis_filename()
              → _process_local_analysis(source_path, ...)
                  → session["download_path"] = source_dir
                  → session["uploaded_source_path"] = source_path
                  → session["local_in_place"] = True
                  → session["classification"] = {Unclassified, 0}

                  ┌──────────────────────────────────────────────────────┐
                  │  Branch by file extension:                           │
                  │                                                      │
                  │  .dmp / is_bsod                                      │
                  │    → copy to shared BSOD folder                      │
                  │    → session["bsod"] = True                          │
                  │    → return url_for("main.download_result_bsod")     │
                  │                                                      │
                  │  .zip / .7z / .rar                                   │
                  │    → attachment_decompose.process_single_zip()       │
                  │    → session["case_context"] = CaseContext(local_*)  │
                  │    → app_config.set_download_results(local_case_nbr) │
                  │    → if sendto_report_path set:                       │
                  │        session["latest_etl_llm"] = True              │
                  │    → return url_for("main.download_result")           │
                  │                                                      │
                  │  .log                                                 │
                  │    → app_config.last_analyzed_log_path = file_path   │
                  │    → return url_for("log_chatbot.index",              │
                  │                     auto_run="analyze_all",           │
                  │                     auto_send="0"|"1")               │
                  │                                                      │
                  │  .hci.txt (BT decoded)                               │
                  │    → app_config.last_analyzed_log_path = file_path   │
                  │    → return url_for("bt_chatbot.index", ...)         │
                  │                                                      │
                  │  BT .etl (ibtpci-*.etl / ibtusb-*.etl)              │
                  │    → bt_decode_hci_via_folder(source_dir, file_path) │
                  │    → session["latest_etl_path"] = hci_path           │
                  │    → return url_for("bt_chatbot.index", ...)         │
                  │                                                      │
                  │  .etl / .etl.N  (WiFi ETL)                          │
                  │    → wpp_ddd_parser_run(file_path)                   │
                  │    → app_config.last_analyzed_log_path = etl + .log  │
                  │    → return url_for("log_chatbot.index",              │
                  │                     auto_run="analyze_all",           │
                  │                     auto_send="0"|"1")               │
                  └──────────────────────────────────────────────────────┘

              → returns { success, redirect, use_chatbot?, etl_path?, log_path?, is_bt? }

  [Client-side routing by response flags]

  IF use_chatbot + etl_path:
    → POST /log_chatbot/prepare  { etl_path }      ← same as Flow 1 prepare step
    → JS redirect /log_chatbot/?auto_run=analyze_all

  IF use_chatbot + log_path (non-BT):
    → POST /log_chatbot/set_log  { log_path }
        → set_log() in log_chatbot_routes.py
            → agent.current_log_path = log_path
            → agent.reset_conversation()
            → _extract_issue_context()
            → agent.prime_with_context(**ctx)
            → session["chatbot_log_path"] = log_path
            → _ensure_feedback_conversation_id(rotate=True)
            → feedback_service.ensure_conversation()
            → read_log_time_range() → returns { log_last_time, ... }
    → JS redirect /log_chatbot/?auto_run=analyze_all

  IF use_chatbot + log_path (BT):
    → POST /bt_chatbot/set_log  { log_path }
    → JS redirect /bt_chatbot/?auto_run=analyze_all

  IF redirect (zip/dmp):
    → JS window.location.href = redirect   (→ /download_result or /download_result_bsod)
    → then same path as Flow 1 from download_result onward

  → [Common Final Leg]
```

| Property | Value |
|---|---|
| **Context** | `classification={Unclassified}`; no Salesforce context |
| **Issue time source** | Log file's last timestamp (via `read_log_time_range` in `set_log`) |
| **Auto-submit** | ❌ `auto_run=analyze_all` fires green button, but pauses for issue time confirmation |
| **`sendto_auto_llm`** | `False` |
| **JSON report saved to** | Folder of `current_log_path` |
| **Allowed file types** | `.zip`, `.7z`, `.rar`, `.etl`, `.etl.N`, `.log`, `.hci.txt`, `.dmp` |

---

## Flow 2b — Browse button inside `/log_chatbot/`

**Trigger:** User navigates directly to `/log_chatbot/` and uses the Browse button to pick an already-parsed `.log` or `.txt` file.

```
GET /log_chatbot/ → index() in log_chatbot_routes.py
  → app_config.last_analyzed_log_path or ""   ← pre-fill suggested log
  → _extract_issue_context()                  ← session may be empty
  → renders log_chatbot.html

  [User clicks Browse]
  → JS: fetch GET /log_chatbot/browse
      → browse() in log_chatbot_routes.py
          → tkinter file dialog (filters: *.log, *.txt)
          → returns { success, path }

  [User clicks "Set Log"]
  → JS: POST /log_chatbot/set_log  { log_path }
      → set_log() in log_chatbot_routes.py
          → _get_or_create_agent()
          → agent.current_log_path = log_path
          → agent.reset_conversation()
          → _extract_issue_context()
          → agent.prime_with_context(**ctx)
          → session["chatbot_log_path"] = log_path
          → read_log_time_range(log_path) → returns { log_last_time, log_first_time }
          → returns { success, log_last_time, ... }

  → user manually types a message → POST /log_chatbot/chat
  → [Common Final Leg]
```

| Property | Value |
|---|---|
| **Context** | Whatever is already in session (often empty if navigated directly) |
| **Issue time source** | Log file's last timestamp (from `read_log_time_range` in `set_log`) |
| **Auto-submit** | ❌ User types manually; no auto_run |
| **`sendto_auto_llm`** | `False` |
| **JSON report saved to** | Folder of `current_log_path` |
| **Allowed file types** | `.log`, `.txt` (already-parsed; no unzip or ETL step) |

---

## Flow 3 — SendTo (Windows right-click, no `--auto-llm`)

**Trigger:** User right-clicks a file in Explorer and selects "Send To → IntelAvatar". No `--auto-llm` flag.

```
app.py __main__
  → argparse: --sendto-token, <file>, [--report r], [--json j]
  → _build_startup_path(input_paths, sendto_token, report, json_path, auto_llm=False)
      → builds URL: /log_parser/open_local_analysis?token=...&path=...
                    [&report=...] [&json=...]
                    (NO &auto_llm=1)
  → DriverManager.navigate(url)   ← open in browser

GET /log_parser/open_local_analysis → open_local_analysis() in log_parser_routes.py
  → validate request.remote_addr in (127.0.0.1, ::1)
  → hmac.compare_digest(token, app_config.sendto_token)
  → os.path.abspath(source_path)
  → _is_allowed_local_analysis_filename(original_name)
  → report_path = os.path.abspath(request.args.get("report"))
  → json_path   = os.path.abspath(request.args.get("json"))
  → session["sendto_pending_path"]  = source_path
  → session["sendto_report_path"]   = report_path
  → session["sendto_json_path"]     = json_path
  → session["sendto_auto_llm"]      = False   ← (auto_llm arg was not set)
  → renders sendto_transmission.html

  [Browser connects to Socket.IO namespace /sendto-progress]
  → socket event triggers background thread:
      → _process_local_analysis(source_path, ...)   ← same function as Flow 2a
          [progress streamed to browser via socketio.emit("progress", ...)]
          → same file-type branching as Flow 2a (see above)

  [non-zip result]
  → _process_local_analysis returns url_for("log_chatbot.index",
                                             auto_run="analyze_all", auto_send="0")
  → _sendto_session_store[token] = (time.time(), dict(session))  ← stash session
  → background thread emits socket event: redirect_url?_st=<token>
  → JS: window.location.href = redirect_url?_st=<token>

  → before_app_request hook: _pickup_sendto_session()
      → reads _st token from URL query string
      → _sendto_session_store.pop(token)
      → session.update(stored_session_data)  ← restore session from stash

  GET /log_chatbot/?auto_run=analyze_all&auto_send=0
  → index() → renders log_chatbot.html
  → JS: tryAutoAnalyzeOnLoad()
      → autoRun=true: auto-clicks green "Analyze All"
      → autoSend=false: STOPS at issue-time confirmation modal
      → user must confirm → sendMessage()
  → [Common Final Leg]

  [zip result] → redirect /download_result?_st=<token>
  → _pickup_sendto_session() restores session (sendto_auto_llm=False)
  → render_download_result_form() → download_result.html
      → JS: const SENDTO_AUTO_LLM = false
  → user manually clicks "Wi-Fi Analysis Agent"
  → [same as Flow 1 from download_result onward]
```

| Property | Value |
|---|---|
| **Context** | `sendto_report_path`, `sendto_json_path` in session; no Salesforce context |
| **Issue time source** | `window.__logLastTime` (log last timestamp) — shown in sidebar, **requires user confirmation** |
| **Auto-submit** | ❌ Pauses at issue-time modal |
| **`sendto_auto_llm`** | `False` |
| **JSON report saved to** | Folder of `sendto_report_path` if set, else folder of `current_log_path` |

---

## Flow 4 — CLI `--auto-llm` (fully automated)

**Trigger:** Command line with `--auto-llm`. Designed for scripted / CI-style pipelines — zero user interaction.

```
app.py __main__
  → argparse: --sendto-token, <file>, --report r, --json j, --auto-llm
  → _build_startup_path(..., auto_llm=True)
      → URL: /log_parser/open_local_analysis?token=...&path=...
             &report=...&json=...&auto_llm=1    ← key difference from Flow 3
  → DriverManager.navigate(url)

GET /log_parser/open_local_analysis?...&auto_llm=1
  → open_local_analysis()   ← same as Flow 3, but:
  → session["sendto_auto_llm"] = True    ← flag stored

  → renders sendto_transmission.html
  → Socket.IO background thread → _process_local_analysis()
      → _auto_send = "1"  (because sendto_auto_llm=True)

  [non-zip result]
  → returns url_for("log_chatbot.index", auto_run="analyze_all", auto_send="1")
  → stash session → emit redirect with _st token

  GET /log_chatbot/?auto_run=analyze_all&auto_send=1
  → _pickup_sendto_session() restores session (sendto_auto_llm=True)
  → index() → renders log_chatbot.html
  → JS: tryAutoAnalyzeOnLoad()
      → autoRun=true, autoSend=true
      → setLog() fires → POST /log_chatbot/set_log
          → set_log() returns { log_last_time }
          → window.__logLastTime = log_last_time
      → setTimeout(400ms):
          → setIssueTimeFromString(window.__logLastTime)  ← log's last timestamp
          → markIssueTimeUserOwned()                      ← clears confirm gate
          → setTimeout(100ms) → sendMessage()             ← auto-submit, no user click
  → POST /log_chatbot/chat
  → [Common Final Leg → report saved to folder of sendto_report_path]

  [zip result]
  → redirect /download_result?_st=<token>
  → _pickup_sendto_session() restores session (sendto_auto_llm=True)
  → render_download_result_form()
      → sendto_auto_llm=True passed to template
      → JS: const SENDTO_AUTO_LLM = true
      → auto_analysis_etl fires → startLogParser()
          → GET /analysis_etl/process_etl_path?etl_path=...
          → wait for Socket.IO "wpp_complete"
      → handleWppComplete()
          → POST /log_chatbot/prepare { etl_path }
          → JS redirect /log_chatbot/?auto_run=analyze_all&auto_send=1
              (SENDTO_AUTO_LLM appends &auto_send=1)
  → same auto-submit path as non-zip above
  → [Common Final Leg → report saved to folder of sendto_report_path]
```

| Property | Value |
|---|---|
| **Context** | `sendto_report_path`, `sendto_json_path`; no Salesforce context |
| **Issue time source** | `window.__logLastTime` — accepted automatically, **no modal** |
| **Auto-submit** | ✅ Fully hands-free |
| **`sendto_auto_llm`** | `True` |
| **JSON report saved to** | Folder of `sendto_report_path` (next to `--report` file) |

---

## Auto-saved JSON Report (`llm_report_*.json`)

Saved at the end of the **Common Final Leg** whenever `result.type == "report"`.

**Save logic (in `event_stream()`, `log_chatbot_routes.py`):**
```python
_save_sendto_report_path  # captured from session BEFORE generator runs (request context)
_save_log_path            # captured from agent.current_log_path BEFORE generator runs

if _save_sendto_report_path:
    _report_dir = os.path.dirname(_save_sendto_report_path)   # Flow 3 / 4
elif _save_log_path:
    _report_dir = os.path.dirname(_save_log_path)             # Flow 1 / 2a / 2b

write llm_report_<YYYYMMDD_HHMMSS>.json
```

**JSON schema:**
```json
{
  "turn_id":          "<uuid>",
  "conversation_id":  "<uuid>",
  "user_message":     "Analyze all",
  "issue_time":       "06/11/2026 15:47:13",
  "report": {
    "root_cause_summary":  "...",
    "confidence_score":    85,
    "recommended_actions": ["...", "..."],
    "involved_skills":     ["...", "..."],
    "markdown_summary":    "..."
  },
  "log_path":   "C:\\...\\WiFi_20260611_154713.txt",
  "saved_at":   "2026-06-16T14:30:22.123456"
}
```

---

## Session Keys Reference

| Key | Set by | Read by | Notes |
|---|---|---|---|
| `case_context` | `handle_case_submission` | `_extract_issue_context`, `render_download_result_form` | Serialised `CaseContext`; heavy fields stashed on disk |
| `prompt_file_path` | `handle_case_submission` | `log_parser_service` | Path to case-type prompt file |
| `selected_files` | `handle_select_attachments_submission` | `render_download_result_form`, `_extract_issue_context` | List of `(name, name, metadata)` tuples |
| `latest_etl_llm` | `handle_select_attachments_submission` | `render_download_result_form → get_auto_analysis_etl` | Cleared on first read |
| `_run_analysis_requested` | `handle_select_attachments_submission` | `render_download_result_form` | Popped on read; survives flag-clearing |
| `_issue_ai_quick` | `_prime_issue_ai_cache` | `render_download_result_form` | `{clean_description, issue_times}` from LLM pre-pass |
| `ai_ips_analysis` | LLM analysis step | `_extract_issue_context` | Structured LLM-generated case summary |
| `classification` | LLM classify step | `_extract_issue_context` | `{issue_type, confidence, keywords_found}` |
| `_attachment_time_cache` | `_extract_issue_context` | `_extract_issue_context` | Avoids re-parsing attachment time per request |
| `_resolved_issue_time_cache` | `_resolved_issue_time_for` | `get_issue_context` route | Cache keyed by log_path |
| `chatbot_log_path` | `set_log` | `_get_or_create_agent` | Current loaded log file path |
| `chatbot_session_id` | `_get_or_create_agent` | `_get_or_create_agent`, `chat` | Ties Flask session to `_chatbot_instances` dict |
| `feedback_conversation_id` | `_ensure_feedback_conversation_id` | `chat`, `feedback_service` | Rotated on each `prepare` / `set_log` |
| `sendto_pending_path` | `open_local_analysis` | `_process_local_analysis` (SendTo thread) | Source file path for SendTo/CLI |
| `sendto_report_path` | `open_local_analysis` | `set_log`, save-report logic | Path to `--report` txt file |
| `sendto_json_path` | `open_local_analysis` | `set_log` | Path to `--json` jsonl file |
| `sendto_auto_llm` | `open_local_analysis` | `_process_local_analysis`, `download_result.html` | `True` only for `--auto-llm` CLI flag |
| `picked_local_analysis_path` | `pick_local_analysis_file` | `upload_local_analysis` | Security bind: prevents path substitution |
| `bsod` | `handle_case_submission`, `upload_local_analysis` | `render_download_result_form` | Routes to BSOD-specific result view |
| `download_path` | `handle_case_submission`, `_process_local_analysis` | `render_download_result_form` | Base directory for extracted files |


**Last updated:** 2026-06-16  
**App:** `wireless_ce_avatar` (Flask + SocketIO)

---

## Overview

There are four ways to reach the LLM analysis (`/log_chatbot/chat`).  
They differ in how much context is available, whether the analysis is automatic, and where the output JSON is saved.

```
┌──────────────────────────────────────────────────────────────────────┐
│  Entry Points                                                        │
│                                                                      │
│  [1] Web UI       → case from Salesforce → download_result → prepare │
│  [2] Local File   → browse dialog in chatbot UI → set_log            │
│  [3] SendTo       → right-click file → ETL parse → chatbot (manual)  │
│  [4] CLI --auto-llm → ETL parse → chatbot (fully automated)         │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Flow 1 — Web UI (Salesforce case)

**Trigger:** User opens the app in a browser, searches a case, downloads attachments, reaches `/download_result`, and clicks **"Wi-Fi Analysis Agent"**.

```
/ (index)
  → search case number
  → download attachments
  → /download_result
      → click "Wi-Fi Analysis Agent"
          → POST /log_chatbot/prepare  { etl_path }
              → derives   <etl_path>.log
              → primes agent with case context
          → JS redirects to /log_chatbot/
              → user types message → POST /log_chatbot/chat
```

| Property | Value |
|---|---|
| **Context** | Full Salesforce: `case_nbr`, `subject`, `description`, `attachment_time`, `classification` |
| **Issue time source** | Attachment gray-subtitle timestamp (parsed from `attachment_list`) |
| **Auto-submit** | ❌ User types manually |
| **`sendto_auto_llm`** | `False` |
| **JSON report saved to** | Folder of `current_log_path` (analysis workspace `.txt`) |

---

## Flow 2 — Local File

There are two sub-flows depending on the file type selected from `index.html`.

---

### Flow 2a — Upload from index.html "Local File Upload" tab (zip / etl / log)

**Trigger:** User opens the app homepage, switches to the **"Local File Upload"** tab, clicks the upload bar, picks a file, and clicks Submit.

```
/ (index.html) → "Local File Upload" tab
  → click upload bar
      → POST /log_parser/pick_local_analysis_file
          → native Windows file dialog
          → session['picked_local_analysis_path'] = <picked path>  (security bind)
          → returns { source_path, filename }
  → click Submit
      → POST /log_parser/upload_local_analysis  { source_path }
          → validates path matches session-bound path (CSRF guard)
          → calls _process_local_analysis(source_path, ...)
              ┌─────────────────────────────────────────────────────────────┐
              │ File type routing inside _process_local_analysis:          │
              │                                                             │
              │  .zip / .7z / .rar  → unzip → ETL parse via Socket.IO     │
              │                      → redirect /download_result           │
              │                                                             │
              │  .etl               → ETL parse via Socket.IO             │
              │                      → returns { use_chatbot, etl_path }  │
              │                                                             │
              │  .log               → returns { use_chatbot, log_path }   │
              │                                                             │
              │  .hci.txt (BT)      → returns { use_chatbot, is_bt,       │
              │                                  log_path }                │
              │                                                             │
              │  BT .etl            → returns { use_chatbot, is_bt,       │
              │                                  log_path: *.hci.txt }    │
              └─────────────────────────────────────────────────────────────┘

          [.etl path]
              → POST /log_chatbot/prepare  { etl_path }
                  → derives <etl_path>.log, primes agent
              → JS redirect /log_chatbot/?auto_run=analyze_all

          [.log path]
              → POST /log_chatbot/set_log  { log_path }
                  → agent.current_log_path = log_path
              → JS redirect /log_chatbot/?auto_run=analyze_all

          [.zip path]
              → JS redirect /download_result
              → user clicks "Wi-Fi Analysis Agent" → /log_chatbot/

  → /log_chatbot/ loads: green "Analyze All" button clicks automatically
  → user manually confirms issue time → POST /log_chatbot/chat
```

| Property | Value |
|---|---|
| **Context** | Whatever is in session from prior case load; often empty if navigated directly |
| **Issue time source** | Log file's last timestamp (fallback) or attachment_time if session has case context |
| **Auto-submit** | ❌ Green button auto-clicks but pauses at issue-time confirmation |
| **`sendto_auto_llm`** | `False` |
| **JSON report saved to** | Folder of `current_log_path` |
| **Allowed file types** | `.zip`, `.7z`, `.rar`, `.etl`, `.log`, `.hci.txt`, `.dmp` |

---

### Flow 2b — Browse button inside `/log_chatbot/` (log / txt only)

**Trigger:** User navigates directly to `/log_chatbot/` and clicks the Browse button to pick an already-parsed `.log` or `.txt` file.

```
/log_chatbot/
  → click Browse
      → GET /log_chatbot/browse
          → native Windows file dialog (filters: *.log, *.txt)
      → POST /log_chatbot/set_log  { log_path }
          → agent.current_log_path = log_path
          → prime_with_context (from session — may be empty)
  → user types message → POST /log_chatbot/chat
```

| Property | Value |
|---|---|
| **Context** | Whatever is in session (often empty if navigated directly) |
| **Issue time source** | Log file's last parseable timestamp (fallback in `prime_with_context`) |
| **Auto-submit** | ❌ User types manually |
| **`sendto_auto_llm`** | `False` |
| **JSON report saved to** | Folder of `current_log_path` |
| **Allowed file types** | `.log`, `.txt` (already-parsed files only — no unzip/ETL) |

---

## Flow 3 — SendTo (Windows right-click, no `--auto-llm`)

**Trigger:** User right-clicks a file in Explorer and selects "Send To → IntelAvatar". No `--auto-llm` flag.

```
app.py --sendto-token <token> <file> [--report r] [--json j]
  → _build_startup_path() → browser opens /log_parser/open_local_analysis
      → session set:
          sendto_pending_path  = <file>
          sendto_report_path   = <r>      (if --report provided)
          sendto_json_path     = <j>      (if --json provided)
          sendto_auto_llm      = False

  [non-zip file] → ETL parse via Socket.IO
      → redirect /log_chatbot/?auto_run=analyze_all&auto_send=0
          → page loads, green "Analyze All" button clicks automatically
          → ⚠️  STOPS at issue-time confirmation — user must confirm

  [.zip file] → /download_result
      → ETL auto-runs (if latest_etl_llm)
      → user clicks button manually → /log_chatbot/
```

| Property | Value |
|---|---|
| **Context** | `sendto_report_path`, `sendto_json_path`; no Salesforce context |
| **Issue time source** | `window.__logLastTime` (log's last timestamp) — shown in sidebar, **requires user confirmation** |
| **Auto-submit** | ❌ Pauses at issue-time modal |
| **`sendto_auto_llm`** | `False` |
| **JSON report saved to** | Folder of `sendto_report_path` (if set), else folder of `current_log_path` |

---

## Flow 4 — CLI `--auto-llm` (fully automated, zero interaction)

**Trigger:** Command line invocation with `--auto-llm` flag. Designed for scripted / CI-style pipelines.

```
app.py --no-tray \
       --sendto-token <token> \
       <file> \
       --report <report.txt> \
       --json   <data.jsonl> \
       --auto-llm

  → _build_startup_path() appends &auto_llm=1 to startup URL
  → browser opens /log_parser/open_local_analysis?...&auto_llm=1
      → session set:
          sendto_pending_path  = <file>
          sendto_report_path   = <report.txt>
          sendto_json_path     = <data.jsonl>
          sendto_auto_llm      = True

  [non-zip] → ETL parse
      → redirect /log_chatbot/?auto_run=analyze_all&auto_send=1
          → tryAutoAnalyzeOnLoad():
              setIssueTimeFromString(window.__logLastTime)   ← log's last timestamp
              markIssueTimeUserOwned()                       ← clears confirm gate
              setTimeout → sendMessage()                     ← auto-sends, no click needed

  [.zip] → /download_result  (SENDTO_AUTO_LLM = true in JS)
      → ETL auto-runs → wpp_complete → handleWppComplete
      → redirect /log_chatbot/?auto_run=analyze_all&auto_send=1
          → same auto-submit path as above

  → POST /log_chatbot/chat (use_tools=true)
      → agent runs N reasoning steps (fetch_filtered_logs, etc.)
      → submit_final_report called by LLM
      → 💾 llm_report_<YYYYMMDD_HHMMSS>.json saved next to <report.txt>
```

| Property | Value |
|---|---|
| **Context** | `sendto_report_path`, `sendto_json_path`; no Salesforce context |
| **Issue time source** | `window.__logLastTime` — accepted automatically, no modal |
| **Auto-submit** | ✅ Fully hands-free |
| **`sendto_auto_llm`** | `True` |
| **JSON report saved to** | Folder of `sendto_report_path` (next to `--report` file) |

---

## Saved JSON report (`llm_report_*.json`)

Produced whenever `type == "report"` is returned from the agent (all 4 flows).

**Filename:** `llm_report_20260616_143022.json`

**Contents:**
```json
{
  "turn_id": "<uuid>",
  "conversation_id": "<uuid>",
  "user_message": "Analyze all",
  "issue_time": "06/11/2026 15:47:13",
  "report": {
    "root_cause_summary": "...",
    "confidence_score": 85,
    "recommended_actions": ["...", "..."],
    "involved_skills": ["...", "..."],
    "markdown_summary": "..."
  },
  "log_path": "C:\\...\\WiFi_20260611_154713.txt",
  "saved_at": "2026-06-16T14:30:22.123456"
}
```

**Save location priority:**
1. Folder of `sendto_report_path` — set when `--report` is passed (CLI / SendTo flows)
2. Folder of `current_log_path` — the parsed `.txt` log file (Web UI / Browse flows)

If neither resolves to a valid directory, the save is skipped with a console warning.

---

## Session keys relevant to log_chatbot

| Key | Set by | Used by | Notes |
|---|---|---|---|
| `sendto_report_path` | `open_local_analysis` | `set_log`, save-report logic | Path to `--report` txt file |
| `sendto_json_path` | `open_local_analysis` | `set_log` | Path to `--json` jsonl file |
| `sendto_auto_llm` | `open_local_analysis` | `_process_local_analysis`, `download_result.html` | `True` only for `--auto-llm` |
| `chatbot_log_path` | `set_log` | `_get_or_create_agent` | Current loaded log file path |
| `case_context` | Salesforce fetch | `_extract_issue_context` | Web UI only |
| `ai_ips_analysis` | LLM pre-pass | `_extract_issue_context` | Web UI only |
| `classification` | LLM pre-pass | `_extract_issue_context` | Web UI only |
| `_attachment_time_cache` | `_extract_issue_context` | `prime_with_context` | Avoids re-parsing per request |
