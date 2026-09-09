    class WifiIssueTimeStrategy extends IssueTimeStrategyBase {}

    // Wi-Fi logs may carry no date at all, so nothing ever writes MM/DD/YYYY
    // into a row or the primary picker for a time-only log.
    WifiIssueTimeStrategy.prototype._normalizeRowTime = function (t) {
        return _stripDateIfNoDateLog(t);
    };

    WifiIssueTimeStrategy.prototype._onExtraRowsChanged = function () {
        if (typeof _updateMultiToggleCount === 'function') _updateMultiToggleCount();
    };

    WifiIssueTimeStrategy.prototype._afterApplyCapturedTimes = function () {
        // If the user explicitly picked >1 time in the popup, reveal the
        // extras list so they can see what they applied (the sidebar
        // master is OFF by default for AI-only auto-fills). Single-pick
        // popups don't add extras, so the master stays where it was.
        const addedExtras = document.querySelectorAll('#extra-issue-times .eit-row').length > 0;
        const master = document.getElementById('use-multi-issue');
        if (addedExtras && master && !master.checked) {
            master.checked = true;
            _refreshSidebarMultiVisibility();
        }
        this._onExtraRowsChanged();
    };

    WifiIssueTimeStrategy.prototype._afterPrefillAutoTimes = function () {
        // AI auto-pass — do NOT auto-tick the sidebar master. Extras stay
        // hidden; the count appears in the toggle's sub-text so the user
        // knows they can opt in.
        this._onExtraRowsChanged();
    };

    WifiIssueTimeStrategy.prototype._afterCaptureRowAdded = function (node) {
        // Newly added rows start UNTICKED — they're visible but not applied
        // unless the user explicitly ticks them (in either single- or
        // multi-pick mode). The initial most-likely tick is set in
        // openTimeCaptureModal AFTER all rows are added.
        const cb = node.querySelector('.eit-pick');
        if (cb) cb.checked = false;
    };

    WifiIssueTimeStrategy.prototype._shouldIncludeCaptureRow = function (row) {
        // Only PICKED rows are applied. Single-pick mode (master off) means
        // exactly one row is picked; multi-pick mode lets the user tick
        // several. Unpicked rows stay visible but are ignored.
        const cb = row.querySelector('.eit-pick');
        return !!(cb && cb.checked);
    };

    WifiIssueTimeStrategy.prototype._afterApplyTimeCapture = function (times, send) {
        // Confirm-only path (send=false) — guard the user's NEXT manual Send
        // against immediately re-opening this same modal. Their chat message
        // probably still contains the time token(s) we just absorbed into the
        // sidebar; without this guard the detection would re-fire on Send
        // and trap the user in a loop unless they edit the timestamp out of
        // the message. The send=true path sets and clears its own skip-once.
        if (!send && times.length > 0) {
            window.__skipTimeCaptureOnce = true;
        }
    };

    WifiIssueTimeStrategy.prototype._extrasEnabled = function () {
        return _useMultiIssueTimes();
    };

    WifiIssueTimeStrategy.prototype._captureSummaryExtraHint = function (n) {
        return n > 1
            ? ` The <strong>most likely</strong> one is auto-selected (highlighted); tick <em>Allow multiple issue times</em> to apply more.`
            : '';
    };

    WifiIssueTimeStrategy.prototype._afterCaptureRowsRendered = function (list) {
        // Default UI state: single-pick mode (master OFF), row 0 ticked as
        // the most-likely candidate, others unticked. The user re-picks by
        // clicking another row, or enables multi-pick via the master ☐.
        const master = document.getElementById('tc-multi');
        if (master) master.checked = false;
        list.querySelectorAll('.eit-row').forEach((row, idx) => {
            const cb = row.querySelector('.eit-pick');
            if (cb) cb.checked = (idx === 0);
        });
        _refreshRowHighlights();
    };

    WifiIssueTimeStrategy.prototype._aiSummaryFooter = function (data, sugg) {
        const parts = [];
        if (sugg.length > 1) {
            parts.push('<div style="margin-top:6px;">The <strong>most likely</strong> one is auto-selected (highlighted); tick <em>Allow multiple issue times</em> to apply more.</div>');
        }
        parts.push('<div style="margin-top:6px;">Edit if needed, then choose <strong>Replace</strong> or <strong>Append</strong>.</div>');
        return parts;
    };

    WifiIssueTimeStrategy.prototype._applyDateFieldState = function (noDate) {
        // Date row is always visible — only the editability flips.
        const dateRow = document.getElementById('it-date-row');
        if (dateRow) dateRow.style.display = '';
        ['it-month', 'it-day', 'it-year'].forEach(id => {
            const el = document.getElementById(id);
            if (!el) return;
            el.disabled = noDate;
            if (noDate) {
                el.value = '';                       // clear any stale value
                el.style.background = '#f3f4f6';     // light grey fill
                el.style.color = '#9ca3af';          // dimmed text
                el.style.cursor = 'not-allowed';
                el.style.opacity = '';               // drop any older dim
            } else {
                el.style.background = '';
                el.style.color = '';
                el.style.cursor = '';
                el.style.opacity = '';
            }
        });
        // Mirror to date labels for consistency (so "MONTH/DAY/YEAR" reads as
        // a unit rather than a half-active row).
        document.querySelectorAll('#it-date-row .it-label').forEach(lbl => {
            lbl.style.color = noDate ? '#9ca3af' : '';
        });
    };

    WifiIssueTimeStrategy.prototype._afterDisplayUpdate = function () {
        // Refresh the "Customer wall clock" annotation row whenever the
        // composed value changes. populateCustomerAnnotation() recomputes the
        // conversion live from the customer IANA zone (DST-aware) or, as a
        // fallback, the label's fixed UTC/GMT offset — so it tracks every edit
        // to the picker without consulting the backend annotation map.
        if (typeof populateCustomerAnnotation === 'function') {
            populateCustomerAnnotation();
        }
    };

    window.createIssueTimeStrategy = (profile) => new WifiIssueTimeStrategy(profile);
