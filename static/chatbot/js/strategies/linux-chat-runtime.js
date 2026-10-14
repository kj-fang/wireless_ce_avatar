    // Linux Wi-Fi driver chat runtime. The whole set_log / auto-analyze flow
    // lives in chat-runtime-base.js; only the issue-time policy below is
    // Linux-specific.
    class LinuxChatRuntimeStrategy extends ChatRuntimeStrategyBase {}

    LinuxChatRuntimeStrategy.prototype._applyIssueTimeOnLoad = function (data, ctx) {
        // Priority chain:
        //   1. LLM-organized issue time(s) from the select-attachments pre-pass
        //      (cached server-side as _issue_ai_quick and re-aligned to the
        //      loaded log's date by realign_times_to_log). This is the user's
        //      "previous page" issue time — IPS case description → LLM.
        //   2. log_last_time, used when the LLM pre-pass produced nothing
        //      usable. dmesg / some journalctl captures carry no calendar
        //      date (allow_time_only), so this may be a time-only anchor.
        // Always runs (overrides any stale value left from a previously-loaded
        // log) so reloading a different log always shows that log's anchor.
        //
        // Skipped entirely when restoring a per-conversation draft: the caller
        // sets the saved draft's Issue Time right after setLog() resolves, and
        // this fire-and-forget fetch would otherwise resolve LATER and clobber
        // it with log_last_time.
        if (ctx.skipIssueTimeAutofill) return;

        (async () => {
            let chosen = '';
            try {
                const ctxRes = await fetch(`${this.api}/get_issue_context`);
                const ctxData = await ctxRes.json();
                const times = Array.isArray(ctxData && ctxData.issue_times)
                    ? ctxData.issue_times.filter(Boolean)
                    : [];
                if (times.length > 0) {
                    chosen = String(times[0] || '').trim();
                }
            } catch (_e) { /* best effort */ }
            if (!chosen && data.log_last_time) {
                chosen = data.log_last_time;
            }
            // A newer setLog() (or anything that re-loaded a log) started after
            // us → abandon, we'd be writing a stale time.
            if (ctx.logGen !== window.__setLogGen) return;
            if (chosen && typeof setIssueTimeFromString === 'function'
                    && setIssueTimeFromString(chosen)) {
                // Auto-detected time is applied immediately — Linux has no
                // event-log refinement step, so there's no confirm-gate here
                // (unlike BT's carried-over-time flow).
                if (typeof markIssueTimeUserOwned === 'function') {
                    markIssueTimeUserOwned();
                }
            }
        })();
    };

    window.createChatRuntimeStrategy =
        (profile) => new LinuxChatRuntimeStrategy(profile);
