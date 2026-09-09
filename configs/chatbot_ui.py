"""UI configuration for the BT and Wi-Fi log chatbots.

Both agents render the same shell (templates/chatbot/page.html); everything that
legitimately differs between them is data, not duplicated markup. Keeping it here
means a route only has to say which domain it is rendering.
"""

_COMMON_FEATURES = {
    "sidebar_toggle": False,
    "issue_time": False,
    "feedback": False,
    "history": False,
    "skill_editor": False,
    # Importing extra skills from a YAML file. Narrower than skill_editor:
    # NW offers the import button without the full editor panel, which is
    # what main shipped too.
    "skill_append": False,
    "sleepstudy": False,
}

BT_FEEDBACK_CONCLUSIONS = [
    ["DRIVER_INSTALLATION", "Driver Installation"],
    ["FIRMWARE_CRASH", "Firmware crash (UMAC / LMAC)"],
    ["DRIVER_INIT_FAILURE", "Driver / device initialization failure"],
    ["LE_AUDIO_FAILURE", "MIC and Audio of LE Audio connection failure"],
    ["CLASSIC_AUDIO_FAILURE", "MIC and Audio of Classic Audio connection failure"],
    ["AUDIO_NOISE", "Audio Noise"],
    ["WAKE_RESUME_DELAY", "Sx Wake/Resume delay"],
    ["MSFT_AUDIO_BT", "MSFT Audio / Bluetooth problem"],
    ["BIOS_DSM_UEFI", "BIOS DSM / UEFI configuration"],
    ["REGULATORY_SETTING", "Regulatory setting"],
    ["RF_SIGNAL_ISSUE", "RF / Signal issue"],
    ["HW_DESIGN_NOISE", "HW design or noise problem"],
]

WIFI_FEEDBACK_CONCLUSIONS = [
    ["OS_INITIATED", "OS-initiated (TASK_DISCONNECT / user/policy)"],
    ["RF_INTERFERENCE", "RF / signal issue (weak coverage, missed beacons)"],
    ["AP_KICK", "AP-initiated (Deauth without missed beacons)"],
    ["FIRMWARE_CRASH", "Firmware crash (UMAC / LMAC / TCM)"],
    ["MCC_MISMATCH", "MCC / regulatory mismatch"],
    ["DRIVER_INIT_FAILURE", "Driver / device initialization failure"],
    ["AUTH_FAILURE", "Authentication failure"],
    ["ASSOC_FAILURE", "Association failure"],
    ["HANDSHAKE_FAILURE", "EAPOL / 4-way handshake failure"],
    ["WAKE_RESUME_DELAY", "Wake / resume delay"],
    ["BIOS_CONFIG_ISSUE", "BIOS DSM / UEFI misconfiguration"],
    ["ROAMING_DECISION", "Roaming decision / AP selection"],
    ["SOFTAP_START_FAILURE", "SoftAP / hosted network start failure"],
    ["P2P_CONNECT_FAILURE", "P2P / Wi-Fi Direct connection failure"],
]

BT_UI = {
    "domain": "bt",
    "api": "/bt_chatbot",
    "title": "Bluetooth Chatbot",
    "back_url": "/bt_chatbot/back_to_avatar",
    "input_placeholder": (
        "Describe the issue (e.g. '6G Weak Signal disconnected'). "
        "Pick the issue time on the left."
    ),
    "accent": "#2563eb",
    "accent_dark": "#1e40af",
    "accent_light": "#3b82f6",
    "accent_soft": "#eef2ff",
    "accent_border": "#e0e7ff",
    "accent_hover": "#eaf4ff",
    "accent_rgb": "37,99,235",
    "event_sources": [
        ["pci_bt", "pci_bt"],
        ["usb_bt", "usb_bt"],
        ["pci_bt_wifi", "pci+wifi"],
        ["usb_bt_wifi", "usb+wifi"],
        ["all", "All"],
    ],
    "event_source_default": "pci_bt",
    "feedback_domain": "bt",
    "feedback_conclusions": BT_FEEDBACK_CONCLUSIONS,
    "allow_modified_yaml_upload": True,
    "history_reset_before_set_log": False,
    "stylesheets": [
        "/static/chatbot/css/full-agent.css",
    ],
    "runtime_strategy_script": "/static/chatbot/js/strategies/bt-chat-runtime.js",
    "profile_script": "/static/chatbot/js/profiles/bt.js",
    "template_parts": {
        "sidebar": "chatbot/profiles/bt/_sidebar.html",
        "runtime": "chatbot/features/_issue_time_runtime.html",
    },
    "issue_time": {
        "strategy_script": "/static/chatbot/js/strategies/bt-issue-time.js",
        "allow_time_only": False,
        "customer_timezone": False,
        "event_refinement": True,
        "multi_select": False,
        "prompt_title": "Send without Issue Time?",
        "prompt_body": (
            "Without a timestamp, the agent will analyse the entire log "
            "instead of focusing on the moment the issue occurred. This may "
            "take longer and produce less precise results."
        ),
    },
    "features": {
        **_COMMON_FEATURES,
        "sidebar_toggle": True,
        "issue_time": True,
        "feedback": True,
        "history": True,
        "skill_editor": True,
        "skill_append": True,
    },
}

# Full Wi-Fi analysis agent used by /log_chatbot.  It shares the optional
# issue-time, feedback, history and skill-editor features with BT, while its
# API and domain data remain Wi-Fi-specific.
LOG_CHATBOT_UI = {
    "domain": "wifi",
    "api": "/log_chatbot",
    "title": "Wi-Fi Analysis Agent",
    # Route through the teardown endpoint, not straight to "/": reloading the
    # chatbot after leaving must not show the previous run's log, chat or context.
    "back_url": "/log_chatbot/back_to_avatar",
    "input_placeholder": "Describe the issue and ask a question about the log.",
    "accent": "#0071c5",
    "accent_dark": "#005a9e",
    "accent_light": "#00a3e0",
    "accent_soft": "#e6f0fa",
    "accent_border": "#e4eaf2",
    "accent_hover": "#eaf4ff",
    "accent_rgb": "0,113,197",
    "event_sources": [
        ["wifi", "Wi-Fi"],
        ["pci_bt_wifi", "pci+wifi"],
        ["usb_bt_wifi", "usb+wifi"],
        ["all", "All"],
    ],
    "event_source_default": "wifi",
    "feedback_domain": "",
    "feedback_conclusions": WIFI_FEEDBACK_CONCLUSIONS,
    "allow_modified_yaml_upload": False,
    "history_reset_before_set_log": True,
    "stylesheets": ["/static/chatbot/css/full-agent.css"],
    "runtime_strategy_script": "/static/chatbot/js/strategies/wifi-chat-runtime.js",
    "profile_script": "/static/chatbot/js/profiles/wifi.js",
    "template_parts": {
        "sidebar": "chatbot/profiles/wifi/_sidebar.html",
        "runtime": "chatbot/features/_issue_time_runtime.html",
    },
    "issue_time": {
        "strategy_script": "/static/chatbot/js/strategies/wifi-issue-time.js",
        "allow_time_only": True,
        "customer_timezone": True,
        "event_refinement": False,
        "multi_select": True,
        # Wi-Fi requires a time anchor: log-scoped Segment2 is the only
        # supported analysis mode, so the copy asks for one rather than
        # offering to analyse the whole file.
        "prompt_title": "Issue Time needed",
        "prompt_body": (
            "An issue-time anchor lets the agent scope the analysis to a "
            "narrow window of log lines around the event, instead of scanning "
            "the entire file — faster turns and more focused answers."
        ),
    },
    "features": {
        **_COMMON_FEATURES,
        "sidebar_toggle": True,
        "issue_time": True,
        "feedback": True,
        "history": True,
        "skill_editor": True,
        "skill_append": True,
    },
}

# Network Experience. `domain` stays "wifi" on purpose: NW analyses Wi-Fi
# logs and its agent introduces itself as a Wi-Fi diagnostic detective, so
# that field describes the subject matter, not the profile. The profile is
# identified by which dict is used (and by GATHER_DOMAIN = "nw" in the route
# module, which is what actually gets persisted).
NW_UI = {
    "domain": "wifi",
    "api": "/nw_analysis",
    "title": "Wi-Fi Log Chatbot",
    "back_url": "/nw_analysis/back_to_avatar",
    "input_placeholder": "Ask a question about the log…",
    "accent": "#0071c5",
    "accent_dark": "#005a9e",
    "accent_light": "#00a3e0",
    "accent_soft": "#e6f0fa",
    "accent_border": "#e4eaf2",
    "accent_hover": "#eaf4ff",
    "accent_rgb": "0,113,197",
    "event_sources": [
        ["wifi", "Wi-Fi"],
        ["pci_bt_wifi", "pci+wifi"],
        ["usb_bt_wifi", "usb+wifi"],
        ["all", "All"],
    ],
    "event_source_default": "wifi",
    "feedback_domain": "",
    "feedback_conclusions": WIFI_FEEDBACK_CONCLUSIONS,
    "allow_modified_yaml_upload": False,
    "history_reset_before_set_log": True,
    # Shares the full agent's chat-step / log-input styling so the NW page
    # renders processing steps exactly like the Wi-Fi and BT agents.
    "stylesheets": ["/static/chatbot/css/full-agent.css"],
    "runtime_strategy_script": "/static/chatbot/js/strategies/nw-chat-runtime.js",
    "profile_script": "/static/chatbot/js/profiles/nw.js",
    "template_parts": {
        "sidebar": "chatbot/profiles/nw/_sidebar.html",
        "runtime": "",
    },
    "issue_time": {
        # NW has no issue-time picker (features.issue_time is False and
        # template_parts.runtime is empty); the block is kept so every profile
        # exposes the same shape.
        "strategy_script": "",
        "allow_time_only": False,
        "customer_timezone": False,
        "event_refinement": False,
        "multi_select": False,
        "prompt_title": "",
        "prompt_body": "",
    },
    "features": {
        **_COMMON_FEATURES,
        "skill_append": True,
        "sleepstudy": True,
    },
}
