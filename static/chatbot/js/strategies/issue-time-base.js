    /**
     * Shared issue-time strategy.
     *
     * The BT and Wi-Fi agents drive the same issue-time sidebar and capture
     * popup; they differ only in a handful of extension points (Wi-Fi supports
     * date-less logs and multi-select, BT refines against the system event
     * log). Every such difference is a hook below — profile subclasses override
     * hooks, never the flow itself.
     */
    class IssueTimeStrategyBase {
        constructor(profile) {
            this.profile = Object.freeze({ ...(profile || {}) });
            this.api = this.profile.api
                || (window.CHATBOT && window.CHATBOT.api)
                || '';
        }
    }

    // ---- profile hooks (all no-ops / identity by default) ----------------

    IssueTimeStrategyBase.prototype._normalizeRowTime = function (t) { return t; };
    IssueTimeStrategyBase.prototype._onExtraRowsChanged = function () {};
    IssueTimeStrategyBase.prototype._afterApplyCapturedTimes = function () {};
    IssueTimeStrategyBase.prototype._afterPrefillAutoTimes = function () {};
    IssueTimeStrategyBase.prototype._afterCaptureRowAdded = function (node) {};
    IssueTimeStrategyBase.prototype._shouldIncludeCaptureRow = function (row) { return true; };
    IssueTimeStrategyBase.prototype._afterApplyTimeCapture = function (times, send) {};
    IssueTimeStrategyBase.prototype._extrasEnabled = function () { return true; };
    IssueTimeStrategyBase.prototype._beforeOpenCaptureModal = function () {};
    IssueTimeStrategyBase.prototype._captureSummaryExtraHint = function (n) { return ''; };
    IssueTimeStrategyBase.prototype._afterCaptureRowsRendered = function (list) {};
    IssueTimeStrategyBase.prototype._suggestRequestBody = function (text) { return { text }; };
    IssueTimeStrategyBase.prototype._selectSuggestions = function (data, sugg) {
        return { chosen: sugg, bestIdx: -1 };
    };
    IssueTimeStrategyBase.prototype._afterSuggestionsOpened = function (data, sugg, bestIdx) {};
    IssueTimeStrategyBase.prototype._aiSuggestionTag = function (idx, bestIdx, sugg, data) { return ''; };
    IssueTimeStrategyBase.prototype._aiSummaryFooter = function (data, sugg) {
        return ['<div style="margin-top:6px;">Edit if needed, then choose <strong>Replace</strong> or <strong>Append</strong>.</div>'];
    };
    IssueTimeStrategyBase.prototype._applyDateFieldState = function (noDate) {};
    IssueTimeStrategyBase.prototype._afterDisplayUpdate = function () {};

    // ---- shared flow ------------------------------------------------------

    IssueTimeStrategyBase.prototype._applyCapturedTimesToSidebar = function (times, mode) {
        if (!Array.isArray(times) || times.length === 0) return;
        if (mode === 'replace') {
            clearIssueTime();
            clearAllExtraIssueTimes();
        }
        // Times applied via the capture popup (typed tokens or AI suggestions)
        // are an explicit user choice — they count without the confirm tick.
        issueTimeAwaitingConfirm = false;
        setIssueTimeConfirmUI(false);
        // First time goes into the primary picker IF empty; otherwise
        // it gets added as an extra row (keeps the existing primary).
        const primaryIsEmpty = !getIssueTimeString();
        let startIdx = 0;
        if (primaryIsEmpty) {
            _writeTimeToPrimary(times[0]);
            startIdx = 1;
        }
        for (let i = startIdx; i < times.length; i++) {
            addExtraIssueTimeRow(times[i]);
        }
        this._afterApplyCapturedTimes();
    };

    IssueTimeStrategyBase.prototype._itEscHtml = function (s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
        ));
    };

    IssueTimeStrategyBase.prototype._populateEitRow = function (node, t) {
        if (!t) return;
        t = this._normalizeRowTime(t);
        const set = (sel, v) => {
            const el = node.querySelector(sel);
            if (el && v != null && v !== '') el.value = String(v);
        };
        set('.eit-month', t.month);
        set('.eit-day',   t.day);
        set('.eit-year',  t.year);
        set('.eit-hour',  t.hh);
        set('.eit-min',   t.mm);
        set('.eit-sec',   t.ss);
        set('.eit-ms',    t.ms);
    };

    IssueTimeStrategyBase.prototype._prefillAutoTimes = function (list) {
        if (!Array.isArray(list) || list.length === 0) return false;
        const rows = list.map(_parseCanonicalToRow).filter(Boolean);
        if (rows.length === 0) return false;
        clearAllExtraIssueTimes();
        _writeTimeToPrimary(rows[0]);
        for (let i = 1; i < rows.length; i++) addExtraIssueTimeRow(rows[i]);
        this._afterPrefillAutoTimes();
        return true;
    };

    IssueTimeStrategyBase.prototype._setAiTimeSummary = function (data, bestIdx) {
        const title = document.getElementById('tc-title');
        if (title) title.textContent = data.user_explicit
            ? 'Issue time(s) from your message'
            : 'AI-suggested issue time(s)';
        const summary = document.getElementById('tc-summary');
        if (!summary) return;
        const parts = [];
        if (data.user_explicit) {
            parts.push('<strong>Using the explicit time(s) from your message</strong> (AI not used).');
        } else if (data.interpretation) {
            parts.push('<strong>AI read:</strong> ' + _itEscHtml(data.interpretation));
        }
        const sugg = data.suggestions || [];
        if (sugg.length) {
            const lis = sugg.map((s, i) => {
                const conf = s.confidence ? ` <em>(${_itEscHtml(s.confidence)})</em>` : '';
                const reason = s.reason ? ' — ' + _itEscHtml(s.reason) : '';
                const tag = this._aiSuggestionTag(i, bestIdx, sugg, data);
                return `<li><code>${_itEscHtml(s.issue_time)}</code>${conf}${tag}${reason}</li>`;
            }).join('');
            parts.push(`<ul style="margin:6px 0 0 16px;padding:0;font-size:0.72rem;line-height:1.45;">${lis}</ul>`);
        } else {
            parts.push('<em>No specific time found — edit the row below or add one.</em>');
        }
        this._aiSummaryFooter(data, sugg).forEach((p) => parts.push(p));
        summary.innerHTML = parts.join(' ');
    };

    IssueTimeStrategyBase.prototype._writeTimeToPrimary = function (t) {
        if (!t) return;
        t = this._normalizeRowTime(t);
        const setIf = (id, v) => {
            const el = document.getElementById(id);
            if (el && v != null) el.value = String(v);
        };
        setIf('it-month', t.month);
        setIf('it-day',   t.day);
        setIf('it-year',  t.year);
        setIf('it-hour',  t.hh);
        setIf('it-min',   t.mm);
        setIf('it-sec',   t.ss);
        setIf('it-ms',    t.ms);
        // Refresh the validation + display readout. Direct .value
        // assignment doesn't fire input events, so the existing live
        // helpers won't run on their own.
        if (typeof updateIssueTimeDisplay === 'function') updateIssueTimeDisplay();
        if (typeof checkIssueTime === 'function') checkIssueTime();
        if (typeof validateUserInput === 'function') validateUserInput();
    };

    IssueTimeStrategyBase.prototype.addExtraIssueTimeRow = function (t) {
        const tpl  = document.getElementById('eit-row-template');
        const list = document.getElementById('extra-issue-times');
        const node = tpl.content.firstElementChild.cloneNode(true);
        _populateEitRow(node, t);
        list.appendChild(node);
        this._onExtraRowsChanged();
        return node;
    };

    IssueTimeStrategyBase.prototype.addTimeCaptureRow = function (t) {
        // Popup uses the COMPACT one-row template (slimmer than the
        // sidebar's labeled two-row version) so 2-5 detected times
        // stack neatly inside the modal.
        const tpl = document.getElementById('eit-row-template-compact');
        const list = document.getElementById('time-capture-list');
        const node = tpl.content.firstElementChild.cloneNode(true);
        _populateEitRow(node, t);
        list.appendChild(node);
        this._afterCaptureRowAdded(node);
    };

    IssueTimeStrategyBase.prototype.applyTimeCapture = function (send) {
        const list = document.getElementById('time-capture-list');
        const times = [];
        list.querySelectorAll('.eit-row').forEach((row) => {
            if (!this._shouldIncludeCaptureRow(row)) return;
            const t = _readEitRow(row);
            if (t.hh != null && t.mm != null && t.ss != null) times.push(t);
        });
        const modeEl = document.querySelector('input[name="tc-mode"]:checked');
        const mode = modeEl ? modeEl.value : 'replace';
        closeTimeCaptureModal();
        // Applying = confirming → the time(s) become user-owned (no extra tick).
        if (times.length > 0) _applyCapturedTimesToSidebar(times, mode);
        this._afterApplyTimeCapture(times, send);
        if (send) {
            // Fire analysis now with the description in the chat box. The
            // skip-once guard stops sendMessage from re-opening this popup.
            window.__skipTimeCaptureOnce = true;
            sendMessage();
            window.__skipTimeCaptureOnce = false;
        }
    };

    IssueTimeStrategyBase.prototype.clearAllExtraIssueTimes = function () {
        const list = document.getElementById('extra-issue-times');
        if (list) list.innerHTML = '';
        this._onExtraRowsChanged();
    };

    IssueTimeStrategyBase.prototype.confirmLlmTimeConsent = async function () {
        closeLlmTimeConsent();
        const btn = document.getElementById('ai-time-btn');
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '⏳ Analyzing…'; }
        try {
            const res = await fetch(`${this.api}/suggest_issue_times`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(this._suggestRequestBody(_llmTimeConsentText)),
            });
            const data = await res.json();
            if (!data.success) {
                if (typeof showToast === 'function') {
                    showToast({ message: '✘ ' + (data.error || 'AI time analysis failed.'), ttlMs: 6000 });
                }
                return;
            }
            const sugg = data.suggestions || [];
            const { chosen, bestIdx } = this._selectSuggestions(data, sugg);
            // Funnel into the existing capture popup (handles replace/append).
            const detected = chosen.map((s) => ({
                month: s.month, day: s.day, year: s.year,
                hh: s.hh, mm: s.mm, ss: s.ss, ms: s.ms,
            }));
            openTimeCaptureModal(detected);
            // Runs AFTER openTimeCaptureModal, which resets any refine linkage.
            this._afterSuggestionsOpened(data, sugg, bestIdx);
            _setAiTimeSummary(data, bestIdx);
            if (sugg.length === 0 && typeof showToast === 'function') {
                showToast({ message: 'ℹ️ ' + (data.message || "AI couldn't pin down a time — please fill it in."), ttlMs: 6000 });
            }
        } catch (e) {
            if (typeof showToast === 'function') {
                showToast({ message: '✘ AI time analysis error: ' + e.message, ttlMs: 6000 });
            }
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = orig; }
        }
    };

    IssueTimeStrategyBase.prototype.getAllIssueTimes = function () {
        const out = [];
        // An unconfirmed auto-filled time does not count.
        const primary = issueTimeAwaitingConfirm ? '' : getIssueTimeString();
        if (primary) out.push(primary);
        if (!this._extrasEnabled()) return out;
        document.querySelectorAll('#extra-issue-times .eit-row').forEach((row) => {
            const s = _formatEitRowString(_readEitRow(row));
            if (s) out.push(s);
        });
        return out;
    };

    IssueTimeStrategyBase.prototype.getAllIssueTimesParsed = function () {
        const out = [];
        // While an auto-filled set awaits confirmation, none of it counts
        // (primary OR the auto-added extras) until the user ticks / edits.
        if (issueTimeAwaitingConfirm) return out;
        const primary = _readPrimaryIssueTime();
        if (primary) out.push(primary);
        if (!this._extrasEnabled()) return out;
        document.querySelectorAll('#extra-issue-times .eit-row').forEach((row) => {
            const t = _readEitRow(row);
            if (t.hh != null && t.mm != null && t.ss != null) out.push(t);
        });
        return out;
    };

    IssueTimeStrategyBase.prototype.onCalendarPicked = function () {
        const picker = document.getElementById('issue-time-picker');
        if (!picker || !picker.value) return;
        // picker.value: "YYYY-MM-DDTHH:MM" or "YYYY-MM-DDTHH:MM:SS"
        const [datePart, timePart] = picker.value.split('T');
        const [yyyy, mm, dd] = datePart.split('-');
        const tp = (timePart || '').split(':');
        const hh = tp[0] || '00', mi = tp[1] || '00', ss = tp[2] || '00';
        const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
        setVal('it-month', String(parseInt(mm, 10)));
        setVal('it-day',   String(parseInt(dd, 10)));
        setVal('it-year',  String(parseInt(yyyy, 10)));
        setVal('it-hour',  String(parseInt(hh, 10)));
        setVal('it-min',   String(parseInt(mi, 10)));
        setVal('it-sec',   String(parseInt(ss, 10)));
        // Keep ms as-is (the native picker doesn't capture milliseconds)
        updateIssueTimeDisplay();
        validateUserInput();
    };

    IssueTimeStrategyBase.prototype.onUserInputChange = function (el) {
        autoResize(el);
        // Once there's a description, clear the "describe first" AI hint.
        const nodesc = document.getElementById('ai-time-nodesc');
        if (nodesc && el.value.trim()) nodesc.style.display = 'none';
        validateUserInput();
    };

    IssueTimeStrategyBase.prototype.openTimeCaptureModal = function (detectedTimes) {
        const list = document.getElementById('time-capture-list');
        list.innerHTML = '';
        this._beforeOpenCaptureModal();
        // Reset the title to the default; the AI flow relabels it afterwards.
        const _tcTitle = document.getElementById('tc-title');
        if (_tcTitle) _tcTitle.textContent = 'Detected issue time(s) in your message';
        const summary = document.getElementById('tc-summary');
        const n = (detectedTimes || []).length;

        // For every detected time that's missing MM/DD/YYYY (the user's
        // text only contained "HH:MM:SS"), borrow the date from a
        // reference source: the sidebar primary picker if filled, else
        // the log's auto-detected timestamp captured at /set_log time.
        const refDate = _getReferenceDateForCapture();
        const enrichedTimes = (detectedTimes || []).map((t) => {
            if (!refDate) return t;
            return {
                ...t,
                month: t.month != null ? t.month : refDate.month,
                day:   t.day   != null ? t.day   : refDate.day,
                year:  t.year  != null ? t.year  : refDate.year,
            };
        });

        if (summary) {
            const dateHint = refDate
                ? ` &middot; MM/DD/YYYY pre-filled from the loaded log.`
                : '';
            const pickHint = this._captureSummaryExtraHint(n);
            summary.innerHTML = `Found ${n} time(s) in your text. Edit any field, add more with “+ Add another time”, or remove any you don’t want.${dateHint}${pickHint}`;
        }

        if (n === 0) {
            addTimeCaptureRow(refDate ? { ...refDate } : undefined);
        } else {
            enrichedTimes.forEach((t) => addTimeCaptureRow(t));
        }
        this._afterCaptureRowsRendered(list);

        const modal = document.getElementById('time-capture-modal');
        modal.classList.add('open');
        modal.setAttribute('aria-hidden', 'false');
    };

    IssueTimeStrategyBase.prototype.refreshIssueTimeDateOptional = function () {
        const noDate = (window.__logHasDate === false);
        const hint = document.getElementById('it-nodate-hint');
        if (hint) hint.style.display = noDate ? 'block' : 'none';
        this._applyDateFieldState(noDate);
        if (typeof updateIssueTimeDisplay === 'function') updateIssueTimeDisplay();
        if (typeof validateUserInput === 'function') validateUserInput();
    };

    IssueTimeStrategyBase.prototype.removeEitRow = function (btn) {
        const row = btn.closest('.eit-row');
        if (row) row.remove();
        // Refresh sidebar count (no-op if the removed row was in the popup).
        this._onExtraRowsChanged();
    };

    IssueTimeStrategyBase.prototype.updateIssueTimeDisplay = function () {
        const display = document.getElementById('issue-time-display');
        const errEl   = document.getElementById('issue-time-error');

        // Validate each filled field; mark invalid ones in red
        let anyFilled = false;
        let firstError = '';
        ['it-month','it-day','it-year','it-hour','it-min','it-sec','it-ms'].forEach(id => {
            const el = document.getElementById(id);
            if (!el) return;
            if (el.value === '') {
                el.classList.remove('it-invalid');
                return;
            }
            anyFilled = true;
            const v = parseInt(el.value, 10);
            const ok = partInRange(id, v);
            markFieldValid(id, ok);
            if (!ok && !firstError) {
                const [lo, hi] = IT_RANGES[id];
                firstError = `${id.replace('it-','').toUpperCase()} must be ${lo}\u2013${hi}.`;
            }
        });

        const composed = getIssueTimeString();
        if (display) {
            if (composed) {
                display.style.color = 'var(--accent)';
                display.textContent = `\u2192 ${composed}`;
            } else if (anyFilled) {
                display.style.color = '#888';
                display.textContent = '\u2192 (incomplete)';
            } else {
                display.textContent = '';
            }
        }
        this._afterDisplayUpdate();
        if (errEl) {
            if (firstError) {
                errEl.textContent = '\u26a0 ' + firstError;
                errEl.style.display = 'block';
            } else {
                errEl.style.display = 'none';
            }
        }
    };
