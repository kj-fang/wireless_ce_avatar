    let isAutoFillMode = false;  // true after auto_run prefill; requires a timestamp before sending
    let firstRoundSent = false;  // true after the user successfully sends the first question
    // True when the primary Issue Time was auto-filled from the PREVIOUS PAGE
    // and the user hasn't confirmed it yet. While true the primary time is
    // shown in the fields but does NOT count as a usable issue time (the user
    // must tick the confirm checkbox, edit a field, or apply an AI suggestion).
    let issueTimeAwaitingConfirm = false;

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
    // ── Confirm modal (used when sending without Issue Time) ──────
    let _confirmModalResolver = null;




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

