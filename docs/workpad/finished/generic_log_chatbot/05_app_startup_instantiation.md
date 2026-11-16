# Sub-task 05: App-startup agent instantiation

Status: open. Folder: [README.md](README.md). Progress: [PROGRESS.md](PROGRESS.md).
Depends on: 01, 02, 04.

## Goal

At boot, `configs/set_up_app.py` loads each profile's skills YAML and constructs its app-level
agent, storing it via `app_config.set_<profile>_chatbot_agent(...)`. Without this, `/generic_chatbot`
would mount successfully (sub-task 04) but every new session would get `agent_config_attr`
resolving to `None`, and — per the existing Linux comment at `set_up_app.py:550-553` — if this
step is skipped incorrectly the agent could silently clone Wi-Fi's skills instead and look for
markers a generic log will never contain.

## Where

- `configs/set_up_app.py`:
  - Imports (~lines 27-38, alongside the existing `linux_skills_yaml_utils` and
    `services.chatbot.engine.linux` imports) — add:
    ```python
    from utils.generic_skills_yaml_utils import (
        bundled_yaml_path as generic_bundled_yaml_path,
        current_active_yaml as generic_current_active_yaml,
        refresh_local_cloud_baseline as generic_refresh_local_cloud_baseline,
        set_active_source as generic_set_active_source,
    )
    from services.chatbot.engine.generic import GenericLogAgentSystem
    ```
    (match whichever specific names the Linux import block actually uses, including any not
    listed above if the real block differs).
  - After the Linux boot block (ends line 599), add a new block mirroring lines 534-598 exactly,
    with `linux` → `generic`, `Linux` → `Generic`, `🐧` → a different emoji (or none), and the
    fallback-warning text changed from "filter for Wi-Fi driver markers a Linux capture does not
    contain" to "filter for a specific domain's markers a generic log may not contain":
    ```python
    generic_skills = None
    generic_set_active_source("cloud")
    try:
        generic_refreshed_path, generic_refreshed_date = generic_refresh_local_cloud_baseline()
        ...
    except Exception as e:
        ...

    generic_chosen_yaml, generic_chosen_date, generic_chosen_source = generic_current_active_yaml()
    if generic_chosen_yaml is not None and generic_chosen_yaml.exists():
        try:
            generic_skills = load_skills_from_yaml(str(generic_chosen_yaml))
            ...
        except Exception as e:
            ...
    else:
        ...

    if llm_helper.client is not None:
        model = getattr(llm_helper, 'model', 'gpt-4.1')
        generic_chatbot_agent = GenericLogAgentSystem(
            client=llm_helper.client,
            model=model,
            skills=generic_skills if generic_skills else llm_helper.skills,
        )
        print(f"📄 Generic Chatbot Agent loaded (model={model})")
    else:
        generic_chatbot_agent = None
        print("⚠️  Generic Chatbot Agent skipped — LLM client not configured (no API key).")
    app_config.set_generic_chatbot_agent(generic_chatbot_agent)
    ```

## Steps

1. Add the new imports to `configs/set_up_app.py`.
2. Add the Generic boot block, placed directly after the Linux block.
3. Confirm `load_skills_from_yaml` (already imported for Linux/BT/Wi-Fi) is reused, not
   re-imported.

## Done when

- Starting the app prints a "Generic Chatbot Agent loaded" (or the skipped-variant) line, the
  same way Linux's does today, with no exceptions.
- `app_config.generic_chatbot_agent` is non-`None` after startup when an LLM client is configured
  (verify via a debugger/print, or by hitting `/generic_chatbot/` and confirming `/set_log` +
  `/chat` work rather than erroring on a `None` agent).
- The PROGRESS.md row for 05 is updated.

## Validate

No automated test covers `set_up_app.py`'s boot sequence (it is exercised implicitly by app
startup only) — validate by starting the app locally and checking the startup log lines, then
using `/generic_chatbot/` end-to-end (load a log, send one chat message, confirm a response comes
back referencing the `Whole_Log_Unfiltered`-equivalent skill).
