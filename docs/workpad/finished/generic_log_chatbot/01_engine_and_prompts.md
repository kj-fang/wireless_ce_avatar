# Sub-task 01: Engine class, capability policy, and default prompts

Status: open. Folder: [README.md](README.md). Progress: [PROGRESS.md](PROGRESS.md).
Depends on: none.

## Goal

Today only four profiles (`wifi`, `bt`, `nw`, `linux`) exist at the engine layer — there is no
`GenericLogAgentSystem`, no `GENERIC_AGENT_POLICY`, and no default prompt/report/issue-time text
for a `generic` profile. This sub-task adds all of that as a profile derived from first
principles — not by copying Linux's content and relabeling it. `GenericLogAgentSystem` is a
*sibling* of `LinuxLogAgentSystem`, not built on top of it; both independently subclass
`WifiLogAgentSystem`, the one shared engine every profile (Wi-Fi, BT, NW, Linux) already sits on
(see Note below on why that base class is unavoidable and not itself a "copy from Linux").

Every value chosen for this profile is justified on its own terms, not inherited by analogy to
Linux. Where that reasoning happens to land on the same value Linux uses (see the per-field notes
below), it is called out explicitly as independent, not copied.

**Note — why `WifiLogAgentSystem` is the base class, not a "pure" clean-room class:**
`WifiLogAgentSystem` (`services/chatbot/engine/system.py:467`) is, despite its name, the one real
shared engine implementation — the 300-570 line preprocessing/tool-loop code every profile uses.
Its own class docstring states the reason no profile reimplements this: "The shared engine owns
the algorithms; profiles describe which optional behavior is active. This prevents a second copy
of the 300-570 line preprocessing/tool-loop methods from drifting out of sync"
(`system.py:148-150`). BT and NW subclass it for the exact same reason Linux does. A truly
from-scratch `GenericLogAgentSystem` that reimplemented the tool loop itself would reintroduce the
drift risk this architecture exists to prevent, for no behavioral gain — so this plan does not
attempt that. What *does* get decided independently, below, is every flag and policy value on top
of that shared base.

## Where

- `services/chatbot/engine/generic.py` (new file) — `GenericLogAgentSystem(WifiLogAgentSystem)`.
  Field-by-field, decided independently (not copied from `linux.py`):
  - `DRIVER_ADD_MARKER: list = []`, `RESET_MARKER: list = []` — the base class's defaults
    (`system.py:545-546`) are literal Wi-Fi driver log strings ("os issued driver device add" /
    "got command (m1 message) task_dot11_reset"). A log of unknown/arbitrary format cannot be
    assumed to contain these, or any other fixed marker pair — there is no marker string that is
    valid for "any log," so this resolves to empty, the same conclusion BT and Linux each reached
    independently for their own log families (`bluetooth.py:8-14`, `linux.py` module docstring).
  - `SCOPE_FULL_LOG_WHEN_EMPTY = True` — follows directly from the above: with no markers,
    Segment1 can never bound anything, so the only sane fallback when the issue-time window is
    also empty is to scope the whole log rather than return nothing.
  - `CAPABILITY_POLICY = GENERIC_AGENT_POLICY` (new policy, see below — not a reuse of
    `LINUX_AGENT_POLICY`).
  - Module docstring should say explicitly that this is an independent, domain-agnostic profile,
    not "Linux without the Linux label."
- `services/chatbot/engine/system.py` (~line 220, immediately after `LINUX_AGENT_POLICY`) — add:
  ```python
  GENERIC_AGENT_POLICY = AgentCapabilityPolicy(
      profile="generic",
      disabled_tools=frozenset({"lookup_assert_code", "softAP_supported_channel"}),
      log_in_decode_host_clock=False,
  )
  ```
  `disabled_tools` reached independently, not copied: the entire tool registry
  (`tool_execution.py`) defines exactly four tools total —
  `fetch_filtered_logs`, `lookup_assert_code`, `softAP_supported_channel`, `submit_final_report`.
  `fetch_filtered_logs` and `submit_final_report` are domain-agnostic core tools every profile
  needs. `lookup_assert_code` (Wi-Fi/BT firmware assert-code lookup) and
  `softAP_supported_channel` (Wi-Fi SoftAP channel check) are the only two tools that exist in the
  whole registry tied to a specific subsystem — neither has any meaning for a log of unknown
  domain, so disabling both is the only coherent choice, independent of what Linux or BT disable.
  There is no third tool a from-scratch read of the registry would additionally disable or keep.
  `log_in_decode_host_clock=False` reached independently: this flag only matters when a log is
  known to be written in a specific decode host's clock (Wi-Fi's ETL `wpp_ddd` flow,
  `system.py:181-184`) — a generic log of unknown provenance cannot assume that, so it must read
  timestamps as-is, the same way Linux and BT do for the same underlying reason (their logs come
  straight off the customer's machine with no decode step).
- `services/chatbot/engine/speclet_defaults.py`:
  - After the `--- Linux (linux_chatbot) ---` section (ends ~line 208), add a
    `--- Generic (generic_chatbot) ---` section defining `GENERIC_PROMPT`, `GENERIC_REPORT`,
    `GENERIC_SUGGEST_TIME`. Structure each the same shape as `LINUX_PROMPT`/`LINUX_REPORT`
    (lines 155-208) and `LINUX_SUGGEST_TIME` (lines 292-301), but with all Wi-Fi/Linux-specific
    vocabulary ("iwlwifi", "mac80211", "deauthenticating", "kernel version", etc.) replaced by
    domain-neutral language (e.g. "the most relevant skill", "the event that started the
    problem", "quote the log lines that support this").
  - Add to the `DEFAULTS` dict (~line 304-319): `"generic_prompt": GENERIC_PROMPT`,
    `"generic_report": GENERIC_REPORT`, `"generic_suggest_time": GENERIC_SUGGEST_TIME`.
- `utils/speclets_utils.py:76` — `PROFILES = ("wifi", "bt", "nw", "linux")` → add `"generic"`,
  i.e. `PROFILES = ("wifi", "bt", "nw", "linux", "generic")`. This is the only line in that module
  gating which profiles get the editable-prompt lifecycle (`load_into_cache()`,
  `get_speclet()`, the prompt-editor's document list at line 690) — everything else is already
  keyed generically off `PROFILES`/`DEFAULTS`, so no other change is needed there.
- Issue-time focus: do NOT add a `GENERIC_WIFI_FOCUS`-style regex bias in `utils/issue_time_ai.py`.
  The route profile in sub-task 04 will pass `issue_time_focus=None`, which is already the
  existing "generic digest" default documented on `AgentRouteProfile.issue_time_focus`
  (`blueprints/chatbot/chatbot_routes.py:222-224`).

## Steps

1. Write `services/chatbot/engine/generic.py` (new file).
2. Add `GENERIC_AGENT_POLICY` to `services/chatbot/engine/system.py`.
3. Write `GENERIC_PROMPT`/`GENERIC_REPORT`/`GENERIC_SUGGEST_TIME` in `speclet_defaults.py` and add
   their `DEFAULTS` entries.
4. Add `"generic"` to `PROFILES` in `utils/speclets_utils.py`.

## Validate

No existing automated test exercises `speclet_defaults.py` or the engine subclasses directly —
validate manually:

- `python -c "from services.chatbot.engine.generic import GenericLogAgentSystem; print(GenericLogAgentSystem.CAPABILITY_POLICY)"`
  prints a policy with `profile='generic'`.
- `python -c "from services.chatbot.engine.speclet_defaults import DEFAULTS; print(sorted(k for k in DEFAULTS if k.startswith('generic')))"`
  prints `['generic_prompt', 'generic_report', 'generic_suggest_time']`.
- `python -c "from utils.speclets_utils import PROFILES; assert 'generic' in PROFILES; print(PROFILES)"`
  succeeds.
- Read `GENERIC_PROMPT`/`GENERIC_REPORT` back and confirm no Wi-Fi/Linux-specific terms
  (iwlwifi, mac80211, wpa_supplicant, kernel, NetworkManager, deauth) appear.

## Done when

The three checks above pass, no existing `wifi`/`bt`/`nw`/`linux` behavior changed (diff review:
only additions, no edits to existing `LINUX_*`/`WIFI_*`/`BT_*`/`NW_*` names), and the PROGRESS.md
row for 01 is updated.
