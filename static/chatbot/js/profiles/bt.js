    let isAutoFillMode = false;  // true after auto_run prefill; requires a timestamp before sending
    let firstRoundSent = false;  // true after the user successfully sends the first question
    // True when the primary Issue Time was auto-filled from the PREVIOUS PAGE
    // and the user hasn't confirmed it yet. While true the primary time is
    // shown in the fields but does NOT count as a usable issue time (the user
    // must tick the confirm checkbox, edit a field, or apply an AI suggestion).
    let issueTimeAwaitingConfirm = false;

    // Accepted time formats (must match backend _extract_disconnect_time):
    //   MM/DD/YYYY-HH:MM:SS(.mmm)
    //   MM/DD/YYYY HH:MM:SS(.mmm)
    //   YYYY-MM-DD HH:MM:SS
    //   YYYY/MM/DD HH:MM:SS


    /** Pad number to 2 digits */

    // Field id -> [min, max] valid range

    // Which Issue Time fields are REQUIRED. For a loaded log that has no date
    // (e.g. DDD/tracefmt logs), the date is optional — only the time is needed,
    // and Segment2 matches by time-of-day.
    // Reflect the date-optional state in the UI when the loaded log has no date.


    /** Auto-advance focus when a field reaches its expected length / max. */

    /**
     * Apply context-aware clamping to the Issue Time fields. Called after
     * the user finishes typing into a field. Currently:
     *   - Year is capped at the current calendar year (issues can't be in
     *     the future).
     *   - Day is capped at the real number of days in the chosen
     *     month/year (e.g. 2/31 → 2/28 or 2/29 on leap years; 4/31 → 4/30).
     */

    /** Days in a given month (1-12), accounting for leap years. */


    /**
     * Run all semantic checks on the Issue Time fields. Returns:
     *   { complete, valid, message, invalidIds:Set, anyFilled }
     * `valid` requires complete + consistent (real calendar day,
     * not in the future).
     */

    /**
     * Read all sidebar Issue Time fields and return a canonical
     * "MM/DD/YYYY-HH:MM:SS(.mmm)" string. Returns '' if the fields are
     * incomplete OR fail any semantic check (calendar day, future time).
     */

    /**
     * Parse a "MM/DD/YYYY-HH:MM:SS(.mmm)" or "YYYY-MM-DD HH:MM:SS"
     * style string and populate the sidebar fields.
     */


    // "🗑️ Clear" button: wipe EVERYTHING in one click — the primary picker
    // AND every extra issue-time row.


    /** Open the hidden native datetime-local picker (calendar popup). */

    /** Called when the user picks a date/time from the native calendar. */


    /**
     * Strip any timestamp fragments from the description so the sidebar
     * Issue Time field is the single source of truth for the time.
     */

    /**
     * Strip a leading "at around" / "at" / "around" / "on" connector
     * (and surrounding whitespace/punctuation) from the description so
     * that auto-prefilled text doesn't leave a trailing "at around".
     */

    /**
     * Validate the inputs.  Returns true if the message can be sent.
     * Time validation now lives entirely in the sidebar Issue Time
     * picker.  The chat textarea only carries the problem description.
     *
     * @param {boolean} submitting - true when called from an explicit
     *   send/submit attempt. Only then do we surface the red
     *   "Please describe the issue" banner; otherwise the empty-desc
     *   state silently keeps the Send button disabled without nagging
     *   the user while they are still composing or just clicked into
     *   the page.
     */


    // ── Auto-filled issue-time confirmation gate ─────────────────────
    // Show/hide the "tick to use this auto-detected time" row and reset
    // its checkbox. Called when a previous-page time is auto-filled (show)
    // and whenever the time becomes user-owned: manual edit, clear, or an
    // applied AI suggestion (hide).
    function onIssueTimeConfirmToggle() {
        const cb = document.getElementById('it-confirm-autofill');
        issueTimeAwaitingConfirm = !(cb && cb.checked);
        updateIssueTimeDisplay();
        validateUserInput();
    }
    // Called from manual-edit / clear / AI-apply paths: the time is now
    // user-owned, so drop the pending-confirmation state.

    // ── Confirm modal (used when sending without Issue Time) ──────
    let _confirmModalResolver = null;




    // AI-suggest option inside the no-issue-time confirm modal: cancel this
    // send attempt, then open the AI suggestion flow. It reads the description
    // already typed in the chat box, asks for consent, infers time(s) from the
    // description + a rough browse of the log, and drops them into the capture
    // popup. After applying a suggested time the user can click Send again.

    // "Use log's last time" in the no-issue-time prompt: set the issue time to
    // the log's last timestamp (an explicit choice → counts immediately), then
    // re-send. Falls back to analysing without a time if the log has no
    // readable timestamp.



    // ── Copy log path to clipboard ──────────────────────────────────

    // ── Browse for log file (server-side native dialog) ────────────

    // ── Set log file ───────────────────────────────────────────────

    // ── Send chat message ──────────────────────────────────────────

    // Insert a small "Incident N/M at <time>" chip into the chat — used
    // before each multi-time analysis iteration so the user can scan
    // which result belongs to which issue time.


    // === Auto-load log and trigger first Analyze-All on page load ===


    /**
     * Show a two-button picker letting the user choose between the original
     * issue time and the closest Error/Critical event time from the system
     * event log.  Called after the issue time is set during auto-analyze.
     */
    // Stash used by pickRefineOption() to know the two candidate times.
    let _refineOriginalTime = '';
    let _refineErrorTime   = '';

    async function refineIssueTimeFromEventLog() {
        const currentTime = getIssueTimeString();
        if (!currentTime) return;

        try {
            // Use the event log source filter (pci_bt / usb_bt) to only
            // match errors from the relevant BT driver source.
            const srcSel = document.getElementById('evtPopupSourceFilter');
            const srcGroup = srcSel ? srcSel.value : '';
            const result = await window.findClosestEventError(currentTime, srcGroup);
            showRefinePickerFromData(currentTime, result);
        } catch (e) {
            // Fetch/parse failed — still show picker, just disable error option.
            showRefinePickerFromData(currentTime, null, '(event log unavailable)');
            console.warn('[Refine-Picker] Failed to find closest event error:', e);
        }
    }

    /**
     * Render the refine picker from already-resolved data — NO event-log fetch.
     * Shared by both entry points:
     *   1) refineIssueTimeFromEventLog()  → data from findClosestEventError()
     *   2) the AI-suggest flow            → data reused from the suggest_issue_times
     *      response (`nearest_error`), so the same events the AI weighed drive
     *      the picker without a second /parse_event_log round-trip.
     * @param {string} originalTime  current/applied issue time string
     * @param {object|null} errInfo  {formatted_time, diff_seconds, source, event_id} or null
     * @param {string} [noneLabel]   label to show when no usable error is given
     */
    function showRefinePickerFromData(originalTime, errInfo, noneLabel) {
        if (!originalTime) return;
        _refineOriginalTime = originalTime;
        _refineErrorTime    = '';

        const origLabel = document.getElementById('refine-original-label');
        const errLabel  = document.getElementById('refine-error-label');
        const errBtn    = document.getElementById('refine-pick-error');
        if (origLabel) origLabel.textContent = `⏱ ${originalTime}`;

        if (errInfo && errInfo.formatted_time && errInfo.diff_seconds <= 600
            && errInfo.formatted_time !== originalTime) {
            _refineErrorTime = errInfo.formatted_time;
            if (errLabel) errLabel.textContent =
                `⏱ ${errInfo.formatted_time}  (${errInfo.source}, ID ${errInfo.event_id}, Δ${Number(errInfo.diff_seconds).toFixed(0)}s)`;
            if (errBtn) { errBtn.disabled = false; errBtn.style.opacity = '1'; }
            console.log(`[Refine-Picker] Closest event error: ${errInfo.formatted_time} ` +
                `(${errInfo.source}, ID: ${errInfo.event_id}, diff: ${Number(errInfo.diff_seconds).toFixed(0)}s)`);
        } else {
            if (errLabel) errLabel.textContent = noneLabel || '(no close error found)';
            if (errBtn) { errBtn.disabled = true; errBtn.style.opacity = '0.5'; }
            console.log('[Refine-Picker] No close error found — showing picker with original only.');
        }

        const picker = document.getElementById('it-refine-picker');
        if (picker) picker.style.display = 'block';
    }

    /**
     * Handler for the refine picker buttons.
     * @param {'original'|'error'} choice
     */
    function pickRefineOption(choice) {
        const timeStr = (choice === 'error') ? _refineErrorTime : _refineOriginalTime;
        if (timeStr) {
            setIssueTimeFromString(timeStr);
            markIssueTimeUserOwned();
            validateUserInput();
        }
        // Highlight the selected button, dim the other
        const btnOrig = document.getElementById('refine-pick-original');
        const btnErr  = document.getElementById('refine-pick-error');
        if (btnOrig) btnOrig.style.border = (choice === 'original') ? '2px solid #2563eb' : '1px solid #cbd5e1';
        if (btnErr)  btnErr.style.border  = (choice === 'error')    ? '2px solid #2563eb' : '1px solid #cbd5e1';
    }

    document.addEventListener('DOMContentLoaded', function () {
        // Show first-round gates (e.g. "load a log file") immediately.
        validateUserInput();
        tryAutoAnalyzeOnLoad();
    });

    // ============================================================
    // Multi-issue-time support
    //   1. Pattern detection on the user's chat message
    //   2. Capture popup with editable rows
    //   3. Sidebar "extras" list with + / × buttons
    //   4. getAllIssueTimes() — primary picker + extras combined
    // ============================================================

    // ── Issue-time capture window (±N min) ───────────────────────────
    // Sidebar control deciding how wide the Segment2 log slice is around
    // the issue time. Valid range is 0 .. log-span (set from /set_log's
    // log_span_minutes); falls back to 120 when the span is unknown.
    // Read by sendMessage and forwarded to /chat.
    // Re-apply the max attribute + readouts whenever the log (and thus
    // the span) changes, or the user edits the field.

    // RFC4122 v4 UUID — used for parent_message_id (multi-incident
    // co-firing key). crypto.randomUUID() is available in all modern
    // browsers; this fallback is purely defensive for old environments.

    // Match HH:MM:SS or HH:MM:SS.mmm anywhere in the text. The leading
    // \b is a word boundary — between a non-word char (e.g. "." or
    // space) and a digit. That's what lets enumerated inputs like
    // "1.17:36:13" pick up the "17:36:13" portion: the regex engine
    // anchors the match at the `.→1` transition, not at the leading
    // "1". (Earlier comment versions described this as a "negative
    // lookbehind" — that was inaccurate; there is no lookbehind in
    // the pattern, just `\b`.) Hours / minutes / seconds are bounds-
    // checked AFTER the regex match so things like "99:88:77" don't
    // slip through.

    // ---- Time-capture popup ----

    // Pull MM/DD/YYYY from the sidebar primary picker first (the user's
    // current explicit choice). Fall back to the log's auto-detected
    // timestamp cached at /set_log. Returns null if neither is available.

    // Extract the date portion of a "MM/DD/YYYY-HH:MM:SS(.mmm)" or
    // "MM/DD/YYYY HH:MM:SS(.mmm)" or "YYYY-MM-DD HH:MM:SS(.mmm)" string.
    // Returns {month, day, year} or null.



    // send=false → "✓ Confirm time(s)": just put the reviewed time(s) into the
    //              sidebar (user reviews, then sends manually later).
    // send=true  → "Apply & send to agent": apply the time(s) AND immediately
    //              send the description already in the chat box for analysis.

    // ---- Sidebar extras list ----



    // ---- Shared row helpers ----
    // Render an eit-row's fields into the same string format that
    // getIssueTimeString() emits for the primary picker, so the agent
    // sees a uniform "at around MM/DD/YYYY HH:MM:SS.mmm" suffix.

    // Combined accessor: primary picker first, then each filled extra row.

    // Same idea but returns the PARSED row objects ({month, day, year,
    // hh, mm, ss, ms}) rather than formatted strings — used by the
    // multi-time chain in sendMessage so it can re-inject each row into
    // _formatEitRowString on its own turn.

    // Read the sidebar primary picker fields and return a row object,
    // or null when the time portion (HH:MM:SS) isn't fully filled.

    // Apply captured times to the sidebar. Mode: 'replace' clears
    // existing first; 'append' keeps existing entries.

    // Parse a canonical "MM/DD/YYYY-HH:MM:SS(.mmm)" (or bare "HH:MM:SS(.mmm)")
    // string into the row object the sidebar pickers consume.

    // Pre-fill the sidebar from a list of canonical time strings: first goes
    // into the primary picker, the rest become extra rows. Used for the
    // LLM-organized multi-time auto-fill (which stays pending confirmation).


    // ── AI issue-time assistant ──────────────────────────────────────
    // Button in the Issue Time sidebar. Reads the chat-input description +
    // (server-side) a rough browse of the loaded log, asks the user to
    // consent, then calls /suggest_issue_times. Suggestions are funneled
    // into the SAME capture popup (replace/append confirm) used for typed
    // time tokens, so applying them is identical to the existing flow.
    let _llmTimeConsentText = '';





    // Choose the index of the single most suitable AI suggestion.
    // Ranking: higher confidence wins (high > medium > low); ties break by
    // the backend's order, which is already "best first". Returns -1 for an
    // empty list.
    function _pickBestSuggestionIndex(sugg) {
        if (!sugg || sugg.length === 0) return -1;
        const rank = { high: 0, medium: 1, low: 2 };
        let bestIdx = 0;
        let bestRank = rank[String(sugg[0].confidence || '').toLowerCase()] ?? 1;
        for (let i = 1; i < sugg.length; i++) {
            const r = rank[String(sugg[i].confidence || '').toLowerCase()] ?? 1;
            if (r < bestRank) { bestRank = r; bestIdx = i; }
        }
        return bestIdx;
    }

    // Relabel + repopulate the capture popup's title/summary for the AI
    // flow. Called AFTER openTimeCaptureModal (which sets its own defaults).
