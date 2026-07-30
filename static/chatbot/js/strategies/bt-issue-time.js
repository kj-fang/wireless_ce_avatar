    class BtIssueTimeStrategy {
        constructor(profile) {
            this.profile = Object.freeze({ ...(profile || {}) });
        }
    }

    BtIssueTimeStrategy.prototype._applyCapturedTimesToSidebar = function (times, mode) {
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
    };

    BtIssueTimeStrategy.prototype._itEscHtml = function (s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
        ));
    };

    BtIssueTimeStrategy.prototype._populateEitRow = function (node, t) {
        if (!t) return;
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

    BtIssueTimeStrategy.prototype._prefillAutoTimes = function (list) {
        if (!Array.isArray(list) || list.length === 0) return false;
        const rows = list.map(_parseCanonicalToRow).filter(Boolean);
        if (rows.length === 0) return false;
        clearAllExtraIssueTimes();
        _writeTimeToPrimary(rows[0]);
        for (let i = 1; i < rows.length; i++) addExtraIssueTimeRow(rows[i]);
        return true;
    };

    BtIssueTimeStrategy.prototype._setAiTimeSummary = function (data, bestIdx) {
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
        const onlyBest = !data.user_explicit && sugg.length > 1;
        if (sugg.length) {
            const lis = sugg.map((s, i) => {
                const conf = s.confidence ? ` <em>(${_itEscHtml(s.confidence)})</em>` : '';
                const reason = s.reason ? ' — ' + _itEscHtml(s.reason) : '';
                // Mark the auto-selected best candidate so the user can see
                // which one was pre-filled below (the rest are alternatives).
                const isBest = onlyBest && i === bestIdx;
                const tag = isBest
                    ? ' <strong style="color:#15803d;">✓ selected</strong>'
                    : (onlyBest ? ' <span style="color:#94a3b8;">(alternative)</span>' : '');
                return `<li><code>${_itEscHtml(s.issue_time)}</code>${conf}${tag}${reason}</li>`;
            }).join('');
            parts.push(`<ul style="margin:6px 0 0 16px;padding:0;font-size:0.72rem;line-height:1.45;">${lis}</ul>`);
        } else {
            parts.push('<em>No specific time found — edit the row below or add one.</em>');
        }
        if (onlyBest) {
            parts.push('<div style="margin-top:6px;">Only the <strong>best</strong> time is pre-filled below. '
                + 'Add an alternative with “＋ Add another time” if needed, then choose '
                + '<strong>Replace</strong> or <strong>Append</strong>.</div>');
        } else {
            parts.push('<div style="margin-top:6px;">Edit if needed, then choose <strong>Replace</strong> or <strong>Append</strong>.</div>');
        }
        summary.innerHTML = parts.join(' ');
    };

    BtIssueTimeStrategy.prototype._writeTimeToPrimary = function (t) {
        if (!t) return;
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

    BtIssueTimeStrategy.prototype.addExtraIssueTimeRow = function (t) {
        const tpl  = document.getElementById('eit-row-template');
        const list = document.getElementById('extra-issue-times');
        const node = tpl.content.firstElementChild.cloneNode(true);
        _populateEitRow(node, t);
        list.appendChild(node);
        return node;
    };

    BtIssueTimeStrategy.prototype.addTimeCaptureRow = function (t) {
        // Popup uses the COMPACT one-row template (slimmer than the
        // sidebar's labeled two-row version) so 2-5 detected times
        // stack neatly inside the modal.
        const tpl = document.getElementById('eit-row-template-compact');
        const list = document.getElementById('time-capture-list');
        const node = tpl.content.firstElementChild.cloneNode(true);
        _populateEitRow(node, t);
        list.appendChild(node);
    };

    BtIssueTimeStrategy.prototype.applyTimeCapture = function (send) {
        const list = document.getElementById('time-capture-list');
        const times = [];
        list.querySelectorAll('.eit-row').forEach((row) => {
            const t = _readEitRow(row);
            if (t.hh != null && t.mm != null && t.ss != null) times.push(t);
        });
        const modeEl = document.querySelector('input[name="tc-mode"]:checked');
        const mode = modeEl ? modeEl.value : 'replace';
        closeTimeCaptureModal();
        // Applying = confirming → the time(s) become user-owned (no extra tick).
        if (times.length > 0) _applyCapturedTimesToSidebar(times, mode);
        // If this apply came from the AI-suggest flow, surface the refine picker
        // using the AI's own nearest-error data (no extra event-log fetch).
        if (window.__aiNearestError) {
            const origTime = getIssueTimeString() || window.__aiBestTimeStr || '';
            if (origTime) showRefinePickerFromData(origTime, window.__aiNearestError);
        }
        window.__aiNearestError = null;
        window.__aiBestTimeStr = '';
        if (send) {
            // Fire analysis now with the description in the chat box. The
            // skip-once guard stops sendMessage from re-opening this popup.
            window.__skipTimeCaptureOnce = true;
            sendMessage();
            window.__skipTimeCaptureOnce = false;
        }
    };

    BtIssueTimeStrategy.prototype.clearAllExtraIssueTimes = function () {
        const list = document.getElementById('extra-issue-times');
        if (list) list.innerHTML = '';
    };

    BtIssueTimeStrategy.prototype.confirmLlmTimeConsent = async function () {
        closeLlmTimeConsent();
        const btn = document.getElementById('ai-time-btn');
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '⏳ Analyzing…'; }
        try {
            // Forward the System Event Log dropdowns so the AI weighs the
            // SAME Warn+Err selection the user sees on this page as a
            // high-priority anchor for the issue time.
            const srcSel = document.getElementById('evtPopupSourceFilter');
            const lvlSel = document.getElementById('evtPopupLevelFilter');
            const res = await fetch('/bt_chatbot/suggest_issue_times', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    text: _llmTimeConsentText,
                    source_filter: srcSel ? srcSel.value : 'all',
                    level_filter: lvlSel ? lvlSel.value : 'warning_error',
                }),
            });
            const data = await res.json();
            if (!data.success) {
                if (typeof showToast === 'function') {
                    showToast({ message: '✘ ' + (data.error || 'AI time analysis failed.'), ttlMs: 6000 });
                }
                return;
            }
            const sugg = data.suggestions || [];
            // For AI suggestions, pre-select ONLY the single best candidate so
            // the user isn't forced to prune a long auto-filled list. The user's
            // own explicit times (user_explicit) are NOT narrowed — all of them
            // are intentional, so we keep the original behaviour there.
            const bestIdx = _pickBestSuggestionIndex(sugg);
            const chosen = (data.user_explicit || sugg.length <= 1)
                ? sugg
                : (bestIdx >= 0 ? [sugg[bestIdx]] : []);
            // Funnel into the existing capture popup (handles replace/append).
            const detected = chosen.map((s) => ({
                month: s.month, day: s.day, year: s.year,
                hh: s.hh, mm: s.mm, ss: s.ss, ms: s.ms,
            }));
            openTimeCaptureModal(detected);
            // Link the refine picker to the SAME events the AI used: stash the
            // best suggestion's nearest system Error/Critical (computed server-
            // side in the suggest response) so applyTimeCapture() can show
            // "Found a nearby system error" WITHOUT a second /parse_event_log
            // fetch. Set AFTER openTimeCaptureModal (which clears the stash).
            const _bestSugg = (!data.user_explicit && bestIdx >= 0) ? sugg[bestIdx]
                : (sugg.length === 1 ? sugg[0] : null);
            if (_bestSugg && _bestSugg.nearest_error) {
                window.__aiNearestError = _bestSugg.nearest_error;
                window.__aiBestTimeStr = _bestSugg.issue_time || '';
            }
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

    BtIssueTimeStrategy.prototype.getAllIssueTimes = function () {
        const out = [];
        // An unconfirmed auto-filled time does not count.
        const primary = issueTimeAwaitingConfirm ? '' : getIssueTimeString();
        if (primary) out.push(primary);
        document.querySelectorAll('#extra-issue-times .eit-row').forEach((row) => {
            const s = _formatEitRowString(_readEitRow(row));
            if (s) out.push(s);
        });
        return out;
    };

    BtIssueTimeStrategy.prototype.getAllIssueTimesParsed = function () {
        const out = [];
        // While an auto-filled set awaits confirmation, none of it counts
        // (primary OR the auto-added extras) until the user ticks / edits.
        if (issueTimeAwaitingConfirm) return out;
        const primary = _readPrimaryIssueTime();
        if (primary) out.push(primary);
        document.querySelectorAll('#extra-issue-times .eit-row').forEach((row) => {
            const t = _readEitRow(row);
            if (t.hh != null && t.mm != null && t.ss != null) out.push(t);
        });
        return out;
    };

    BtIssueTimeStrategy.prototype.onCalendarPicked = function () {
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

    BtIssueTimeStrategy.prototype.onUserInputChange = function (el) {
        autoResize(el);
        // Once there's a description, clear the "describe first" AI hint.
        const nodesc = document.getElementById('ai-time-nodesc');
        if (nodesc && el.value.trim()) nodesc.style.display = 'none';
        validateUserInput();
    };

    BtIssueTimeStrategy.prototype.openTimeCaptureModal = function (detectedTimes) {
        const list = document.getElementById('time-capture-list');
        list.innerHTML = '';
        // Clear any AI refine linkage by default; the AI-suggest flow re-sets it
        // AFTER this call. This keeps typed-token captures (which also open this
        // modal) from inheriting a stale nearest-error and showing the picker.
        window.__aiNearestError = null;
        window.__aiBestTimeStr = '';
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
            summary.innerHTML = `Found ${n} time(s) in your text. Edit any field, add more with “+ Add another time”, or remove any you don’t want.${dateHint}`;
        }

        if (n === 0) {
            addTimeCaptureRow(refDate ? { ...refDate } : undefined);
        } else {
            enrichedTimes.forEach((t) => addTimeCaptureRow(t));
        }
        const modal = document.getElementById('time-capture-modal');
        modal.classList.add('open');
        modal.setAttribute('aria-hidden', 'false');
    };

    BtIssueTimeStrategy.prototype.refreshIssueTimeDateOptional = function () {
        const noDate = (window.__logHasDate === false);
        const hint = document.getElementById('it-nodate-hint');
        if (hint) hint.style.display = noDate ? 'block' : 'none';
        ['it-month', 'it-day', 'it-year'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.style.opacity = noDate ? '0.55' : '';
        });
        if (typeof updateIssueTimeDisplay === 'function') updateIssueTimeDisplay();
        if (typeof validateUserInput === 'function') validateUserInput();
    };

    BtIssueTimeStrategy.prototype.removeEitRow = function (btn) {
        const row = btn.closest('.eit-row');
        if (row) row.remove();
    };

    BtIssueTimeStrategy.prototype.updateIssueTimeDisplay = function () {
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
                display.style.color = '#2563eb';
                display.textContent = `\u2192 ${composed}`;
            } else if (anyFilled) {
                display.style.color = '#888';
                display.textContent = '\u2192 (incomplete)';
            } else {
                display.textContent = '';
            }
        }
        if (errEl) {
            if (firstError) {
                errEl.textContent = '\u26a0 ' + firstError;
                errEl.style.display = 'block';
            } else {
                errEl.style.display = 'none';
            }
        }
    };

    window.createIssueTimeStrategy = (profile) => new BtIssueTimeStrategy(profile);
