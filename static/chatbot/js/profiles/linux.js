    let isAutoFillMode = false;  // true after auto_run prefill; requires a timestamp before sending
    let firstRoundSent = false;  // true after the user successfully sends the first question
    // True when the primary Issue Time was auto-filled from the PREVIOUS PAGE
    // and the user hasn't confirmed it yet. While true the primary time is
    // shown in the fields but does NOT count as a usable issue time (the user
    // must tick the confirm checkbox, edit a field, or apply an AI suggestion).
    let issueTimeAwaitingConfirm = false;

    // Show / hide the inline "Log ends at: HH:MM:SS.mmm" link that sits under
    // the no-date hint. Visible only when:
    //   * the loaded log is time-only (dmesg without -T / some journalctl
    //     formats), AND
    //   * the backend successfully extracted a last timestamp.
    // Lets time-only-log users see the log's last event time immediately on
    // upload (instead of having to send-without-time first and click the
    // modal button to discover it).
    function refreshLogLastTimeHint() {
        const wrap = document.getElementById('it-nodate-lasttime-wrap');
        const link = document.getElementById('it-nodate-lasttime');
        if (!wrap || !link) return;
        const noDate = (window.__logHasDate === false);
        const t = window.__logLastTime || '';
        if (noDate && t) {
            link.textContent = t;
            wrap.style.display = '';
        } else {
            link.textContent = '';
            wrap.style.display = 'none';
        }
    }

    // Click handler for the inline "Log ends at" link: drops the timestamp
    // into the sidebar Issue Time fields. Treated as an explicit user choice
    // (markIssueTimeUserOwned, no awaiting-confirm tick).
    function useLogLastTimeFromHint(ev) {
        if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
        const t = window.__logLastTime || '';
        if (!t) return;
        if (typeof setIssueTimeFromString === 'function' && setIssueTimeFromString(t)) {
            if (typeof markIssueTimeUserOwned === 'function') markIssueTimeUserOwned();
            if (typeof updateIssueTimeDisplay === 'function') updateIssueTimeDisplay();
            if (typeof validateUserInput === 'function') validateUserInput();
        }
    }

    // ── Confirm modal (used when sending without Issue Time) ──────
    let _confirmModalResolver = null;

    document.addEventListener('DOMContentLoaded', function () {
        // Show first-round gates (e.g. "load a log file") immediately.
        validateUserInput();
        tryAutoAnalyzeOnLoad();
    });

    // ── Pick / multi-mode helpers (capture popup) ──────────────────
    // Single-pick mode (master ☐ OFF, the default): clicking a row's
    // checkbox unticks every other row (radio-like). The user can never
    // end up with zero picked — unticking the only ticked row re-ticks it.
    // Multi-pick mode (master ☐ ON): rows toggle independently.
    function _onRowPickToggle(cb) {
        const list = document.getElementById('time-capture-list');
        const master = document.getElementById('tc-multi');
        const multi = !!(master && master.checked);
        if (!multi) {
            if (cb.checked) {
                list.querySelectorAll('.eit-pick').forEach((other) => {
                    if (other !== cb) other.checked = false;
                });
            } else {
                // Don't allow unticking the last picked row.
                const anyOther = Array.from(list.querySelectorAll('.eit-pick'))
                    .some((o) => o !== cb && o.checked);
                if (!anyOther) cb.checked = true;
            }
        }
        _refreshRowHighlights();
    }

    function _onMasterMultiToggle(masterCb) {
        const list = document.getElementById('time-capture-list');
        if (!masterCb.checked) {
            // Switching back to single-pick: keep only the FIRST currently
            // ticked row, untick the rest. If none was ticked, tick the
            // very first row so something always gets applied.
            let kept = false;
            list.querySelectorAll('.eit-pick').forEach((cb) => {
                if (cb.checked) {
                    if (kept) cb.checked = false;
                    else kept = true;
                }
            });
            if (!kept) {
                const first = list.querySelector('.eit-pick');
                if (first) first.checked = true;
            }
        }
        _refreshRowHighlights();
    }

    function _refreshRowHighlights() {
        document.querySelectorAll('#time-capture-list .eit-row').forEach((row) => {
            const cb = row.querySelector('.eit-pick');
            row.classList.toggle('eit-row-picked', !!(cb && cb.checked));
        });
    }

    // ---- Shared row helpers ----
    // For time-only logs (window.__logHasDate === false), strip the
    // month/day/year before writing them into ANY row (popup or sidebar
    // extra). Backend make_suggestion() always carries m/d/y — even when
    // the LLM was instructed to return time-only — so without this gate,
    // a placeholder date silently leaks back into the picker and then
    // out through getIssueTimeString as a full "MM/DD/YYYY-HH:MM:SS"
    // for a log that has no date. User-typed dates go through DOM
    // directly and bypass this helper, so manual overrides still work.
    function _stripDateIfNoDateLog(t) {
        if (!t || window.__logHasDate !== false) return t;
        return { ...t, month: null, day: null, year: null };
    }
    // True when the sidebar "Use multiple issue times" master is ticked.
    // When false, the sidebar extras are hidden AND excluded from the
    // accessors below — only the primary picker counts.
    function _useMultiIssueTimes() {
        const m = document.getElementById('use-multi-issue');
        return !!(m && m.checked);
    }

    // Show/hide the extras list + add button based on the master state.
    // Existing rows are kept in the DOM (so toggling off → on restores
    // them exactly); only their visibility flips.
    function _refreshSidebarMultiVisibility() {
        const on = _useMultiIssueTimes();
        const list = document.getElementById('extra-issue-times');
        const btn  = document.getElementById('add-extra-issue-btn');
        if (list) list.style.display = on ? '' : 'none';
        if (btn)  btn.style.display  = on ? '' : 'none';
    }

    function _onSidebarMultiToggle(/* cb */) {
        _refreshSidebarMultiVisibility();
        _updateMultiToggleCount();
        // Refresh the validation display in case a previously-hidden extra
        // changed the "has any issue time" verdict.
        if (typeof validateUserInput === 'function') validateUserInput();
    }

    // Update the sub-text on the master toggle so the user knows hidden
    // extras exist (e.g. AI auto-fill found 3 times → primary visible + 2
    // hidden; the toggle now says "2 additional time(s) available").
    function _updateMultiToggleCount() {
        const sub = document.querySelector('.sidebar-multi-toggle .smt-sub');
        if (!sub) return;
        const n  = document.querySelectorAll('#extra-issue-times .eit-row').length;
        const on = _useMultiIssueTimes();
        if (on) {
            sub.textContent = n > 0
                ? `On — ${n} extra row(s) also used.`
                : `On — add rows with "+ Add another issue time".`;
            sub.style.color = '';
        } else if (n > 0) {
            sub.innerHTML = `<strong style="color:#1d4ed8;">${n} more available</strong> — tick to use.`;
        } else {
            sub.textContent = `Off — only the most-likely time. Tick to add more.`;
            sub.style.color = '';
        }
    }

    // ── AI issue-time assistant ──────────────────────────────────────
    // Button in the Issue Time sidebar. Reads the chat-input description +
    // (server-side) a rough browse of the loaded log, asks the user to
    // consent, then calls /suggest_issue_times. Suggestions are funneled
    // into the SAME capture popup (replace/append confirm) used for typed
    // time tokens, so applying them is identical to the existing flow.
    let _llmTimeConsentText = '';
