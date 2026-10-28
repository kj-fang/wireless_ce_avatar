    // Bluetooth chat runtime. The whole set_log / auto-analyze flow lives in
    // chat-runtime-base.js; only the issue-time policy below is BT-specific.
    class BtChatRuntimeStrategy extends ChatRuntimeStrategyBase {}

    BtChatRuntimeStrategy.prototype._applyIssueTimeOnLoad = function (data, ctx) {
        // Pre-fill the sidebar Issue Time with the log's resolved time (its
        // last parseable timestamp when no case context supplies one). On
        // ROTATION (a DIFFERENT log replaced the previous one) the old value
        // belonged to the previous log, so wipe it and refresh to the new log's
        // time — clearing first so a stale value can't survive when the new log
        // has no detectable time. On a normal (non-rotated) load we only fill
        // when empty, so we never clobber a value the user typed or that AI
        // suggest / session context already placed.
        //
        // skipIssueTimeAutofill: set when restoring a draft / resumed live
        // session — the caller has ALREADY put the user's saved (possibly
        // newly-edited) issue time into the sidebar, so don't touch it at all.
        if (ctx.skipIssueTimeAutofill) return;

        if (data.rotated) {
            clearAllIssueTimes();
            if (data.issue_time) setIssueTimeFromString(data.issue_time);
        } else if (data.issue_time && !getIssueTimeString()) {
            setIssueTimeFromString(data.issue_time);
        }

        // If an issue time is now filled, auto-refine it against the closest
        // system event error.
        if (getIssueTimeString()) {
            refineIssueTimeFromEventLog();
        }
    };

    BtChatRuntimeStrategy.prototype._onAutoIssueTimeApplied = function () {
        // Carried over from the previous page → show it, but require the user
        // to tick the confirm box before it counts.
        issueTimeAwaitingConfirm = true;
        setIssueTimeConfirmUI(true);
    };

    BtChatRuntimeStrategy.prototype._afterAutoPrefill = function () {
        // Auto-refine: cross-reference the issue time with system event errors
        // and snap to the closest error timestamp (if within 10 min).
        refineIssueTimeFromEventLog();
    };

    window.createChatRuntimeStrategy =
        (profile) => new BtChatRuntimeStrategy(profile);
