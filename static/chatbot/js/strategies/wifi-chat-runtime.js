    // Wi-Fi full-agent chat runtime. The whole set_log / auto-analyze flow
    // lives in chat-runtime-base.js; only the issue-time policy below is
    // Wi-Fi-specific.
    class WifiChatRuntimeStrategy extends ChatRuntimeStrategyBase {}

    WifiChatRuntimeStrategy.prototype._afterDateOptionalRefresh = function () {
        // Inline "Log ends at: …" affordance under the no-date hint. Visible
        // only for time-only logs that actually produced a last timestamp.
        if (typeof refreshLogLastTimeHint === 'function') refreshLogLastTimeHint();
    };

    WifiChatRuntimeStrategy.prototype._applyIssueTimeOnLoad = function (data, ctx) {
        // Priority chain:
        //   1. LLM-organized issue time(s) from the select-attachments pre-pass
        //      (cached server-side as _issue_ai_quick and re-aligned to the
        //      loaded log's date by realign_times_to_log). This is the user's
        //      "previous page" issue time — IPS case description → LLM.
        //   2. log_last_time (Wi-Fi: full datetime; DDD: time-only), used when
        //      the LLM pre-pass produced nothing usable.
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
                // Auto-detected time is applied immediately (the old
                // confirm-gate was removed).
                if (typeof markIssueTimeUserOwned === 'function') {
                    markIssueTimeUserOwned();
                }
            }
        })();
    };

    WifiChatRuntimeStrategy.prototype._onIssueContextLoaded = function (contextData) {
        // Customer-tz annotation map: ``{ "<log_frame_str>": "<customer_str>" }``
        // populated by determine_issue_time_frames on the server. The sidebar
        // surfaces it under the picker once an issue time is applied so the
        // engineer sees what time the customer would have seen on their own
        // wall clock.
        window.__customerAnnotations = contextData.customer_annotations || {};
        window.__customerTzLabel = contextData.customer_tz || '';
        window.__customerIana = contextData.customer_iana || '';
        if (typeof populateCustomerAnnotation === 'function') {
            populateCustomerAnnotation();
        }
    };

    window.createChatRuntimeStrategy =
        (profile) => new WifiChatRuntimeStrategy(profile);
