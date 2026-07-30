    class WifiIssueTimeStrategy {
        constructor(profile) {
            this.profile = Object.freeze({ ...(profile || {}) });
        }
    }

    WifiIssueTimeStrategy.prototype._applyCapturedTimesToSidebar = function (times, mode) {
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
        _updateMultiToggleCount();
    };

    WifiIssueTimeStrategy.prototype._itEscHtml = function (s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
        ));
    };

    WifiIssueTimeStrategy.prototype._populateEitRow = function (node, t) {
        if (!t) return;
        t = _stripDateIfNoDateLog(t);
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

    WifiIssueTimeStrategy.prototype._prefillAutoTimes = function (list) {
        if (!Array.isArray(list) || list.length === 0) return false;
        const rows = list.map(_parseCanonicalToRow).filter(Boolean);
        if (rows.length === 0) return false;
        clearAllExtraIssueTimes();
        _writeTimeToPrimary(rows[0]);
        for (let i = 1; i < rows.length; i++) addExtraIssueTimeRow(rows[i]);
        // AI auto-pass — do NOT auto-tick the sidebar master. Extras stay
        // hidden; the count appears in the toggle's sub-text so the user
        // knows they can opt in.
        _updateMultiToggleCount();
        return true;
    };

    WifiIssueTimeStrategy.prototype._setAiTimeSummary = function (data) {
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
            const lis = sugg.map((s) => {
                const conf = s.confidence ? ` <em>(${_itEscHtml(s.confidence)})</em>` : '';
                const reason = s.reason ? ' — ' + _itEscHtml(s.reason) : '';
                return `<li><code>${_itEscHtml(s.issue_time)}</code>${conf}${reason}</li>`;
            }).join('');
            parts.push(`<ul style="margin:6px 0 0 16px;padding:0;font-size:0.72rem;line-height:1.45;">${lis}</ul>`);
        } else {
            parts.push('<em>No specific time found — edit the row below or add one.</em>');
        }
        if (sugg.length > 1) {
            parts.push('<div style="margin-top:6px;">The <strong>most likely</strong> one is auto-selected (highlighted); tick <em>Allow multiple issue times</em> to apply more.</div>');
        }
        parts.push('<div style="margin-top:6px;">Edit if needed, then choose <strong>Replace</strong> or <strong>Append</strong>.</div>');
        summary.innerHTML = parts.join(' ');
    };

    WifiIssueTimeStrategy.prototype._writeTimeToPrimary = function (t) {
        if (!t) return;
        // Same guard as _populateEitRow: never auto-write a date into the
        // sidebar primary picker for time-only logs.
        t = _stripDateIfNoDateLog(t);
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

    WifiIssueTimeStrategy.prototype.addExtraIssueTimeRow = function (t) {
        const tpl  = document.getElementById('eit-row-template');
        const list = document.getElementById('extra-issue-times');
        const node = tpl.content.firstElementChild.cloneNode(true);
        _populateEitRow(node, t);
        list.appendChild(node);
        if (typeof _updateMultiToggleCount === 'function') _updateMultiToggleCount();
        return node;
    };

    WifiIssueTimeStrategy.prototype.addTimeCaptureRow = function (t) {
        // Popup uses the COMPACT one-row template (slimmer than the
        // sidebar's labeled two-row version) so 2-5 detected times
        // stack neatly inside the modal.
        const tpl = document.getElementById('eit-row-template-compact');
        const list = document.getElementById('time-capture-list');
        const node = tpl.content.firstElementChild.cloneNode(true);
        _populateEitRow(node, t);
        list.appendChild(node);
        // Newly added rows start UNTICKED — they're visible but not applied
        // unless the user explicitly ticks them (in either single- or
        // multi-pick mode). The initial most-likely tick is set in
        // openTimeCaptureModal AFTER all rows are added.
        const cb = node.querySelector('.eit-pick');
        if (cb) cb.checked = false;
    };

    WifiIssueTimeStrategy.prototype.applyTimeCapture = function (send) {
        const list = document.getElementById('time-capture-list');
        const times = [];
        // Only PICKED rows are applied. Single-pick mode (master off) means
        // exactly one row is picked; multi-pick mode lets the user tick
        // several. Unpicked rows stay visible but are ignored here.
        list.querySelectorAll('.eit-row').forEach((row) => {
            const cb = row.querySelector('.eit-pick');
            if (!cb || !cb.checked) return;
            const t = _readEitRow(row);
            if (t.hh != null && t.mm != null && t.ss != null) times.push(t);
        });
        const modeEl = document.querySelector('input[name="tc-mode"]:checked');
        const mode = modeEl ? modeEl.value : 'replace';
        closeTimeCaptureModal();
        // Applying = confirming → the time(s) become user-owned (no extra tick).
        if (times.length > 0) _applyCapturedTimesToSidebar(times, mode);
        // Confirm-only path (send=false) — guard the user's NEXT manual Send
        // against immediately re-opening this same modal. Their chat message
        // probably still contains the time token(s) we just absorbed into the
        // sidebar; without this guard the detection would re-fire on Send
        // and trap the user in a loop unless they edit the timestamp out of
        // the message. The send=true path below sets and clears its own
        // skip-once around sendMessage().
        if (!send && times.length > 0) {
            window.__skipTimeCaptureOnce = true;
        }
        if (send) {
            // Fire analysis now with the description in the chat box. The
            // skip-once guard stops sendMessage from re-opening this popup.
            window.__skipTimeCaptureOnce = true;
            sendMessage();
            window.__skipTimeCaptureOnce = false;
        }
    };

    WifiIssueTimeStrategy.prototype.clearAllExtraIssueTimes = function () {
        const list = document.getElementById('extra-issue-times');
        if (list) list.innerHTML = '';
        if (typeof _updateMultiToggleCount === 'function') _updateMultiToggleCount();
    };

    WifiIssueTimeStrategy.prototype.confirmLlmTimeConsent = async function () {
        closeLlmTimeConsent();
        const btn = document.getElementById('ai-time-btn');
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '⏳ Analyzing…'; }
        try {
            const res = await fetch('/log_chatbot/suggest_issue_times', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text: _llmTimeConsentText }),
            });
            const data = await res.json();
            if (!data.success) {
                if (typeof showToast === 'function') {
                    showToast({ message: '✘ ' + (data.error || 'AI time analysis failed.'), ttlMs: 6000 });
                }
                return;
            }
            const sugg = data.suggestions || [];
            // Funnel into the existing capture popup (handles replace/append).
            const detected = sugg.map((s) => ({
                month: s.month, day: s.day, year: s.year,
                hh: s.hh, mm: s.mm, ss: s.ss, ms: s.ms,
            }));
            openTimeCaptureModal(detected);
            _setAiTimeSummary(data);
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

    WifiIssueTimeStrategy.prototype.getAllIssueTimes = function () {
        const out = [];
        // An unconfirmed auto-filled time does not count.
        const primary = issueTimeAwaitingConfirm ? '' : getIssueTimeString();
        if (primary) out.push(primary);
        if (!_useMultiIssueTimes()) return out;
        document.querySelectorAll('#extra-issue-times .eit-row').forEach((row) => {
            const s = _formatEitRowString(_readEitRow(row));
            if (s) out.push(s);
        });
        return out;
    };

    WifiIssueTimeStrategy.prototype.getAllIssueTimesParsed = function () {
        const out = [];
        // While an auto-filled set awaits confirmation, none of it counts
        // (primary OR the auto-added extras) until the user ticks / edits.
        if (issueTimeAwaitingConfirm) return out;
        const primary = _readPrimaryIssueTime();
        if (primary) out.push(primary);
        if (!_useMultiIssueTimes()) return out;
        document.querySelectorAll('#extra-issue-times .eit-row').forEach((row) => {
            const t = _readEitRow(row);
            if (t.hh != null && t.mm != null && t.ss != null) out.push(t);
        });
        return out;
    };

    WifiIssueTimeStrategy.prototype.onCalendarPicked = function () {
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

    WifiIssueTimeStrategy.prototype.onUserInputChange = function (el) {
        autoResize(el);
        // Once there's a description, clear the "describe first" AI hint.
        const nodesc = document.getElementById('ai-time-nodesc');
        if (nodesc && el.value.trim()) nodesc.style.display = 'none';
        validateUserInput();
    };

    WifiIssueTimeStrategy.prototype.openTimeCaptureModal = function (detectedTimes) {
        const list = document.getElementById('time-capture-list');
        list.innerHTML = '';
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
            const pickHint = n > 1
                ? ` The <strong>most likely</strong> one is auto-selected (highlighted); tick <em>Allow multiple issue times</em> to apply more.`
                : '';
            summary.innerHTML = `Found ${n} time(s) in your text. Edit any field, add more with “+ Add another time”, or remove any you don’t want.${dateHint}${pickHint}`;
        }

        if (n === 0) {
            addTimeCaptureRow(refDate ? { ...refDate } : undefined);
        } else {
            enrichedTimes.forEach((t) => addTimeCaptureRow(t));
        }

        // Default UI state: single-pick mode (master OFF), row 0 ticked as
        // the most-likely candidate, others unticked. The user re-picks by
        // clicking another row, or enables multi-pick via the master ☐.
        const master = document.getElementById('tc-multi');
        if (master) master.checked = false;
        const rows = list.querySelectorAll('.eit-row');
        rows.forEach((row, idx) => {
            const cb = row.querySelector('.eit-pick');
            if (cb) cb.checked = (idx === 0);
        });
        _refreshRowHighlights();

        const modal = document.getElementById('time-capture-modal');
        modal.classList.add('open');
        modal.setAttribute('aria-hidden', 'false');
    };

    WifiIssueTimeStrategy.prototype.refreshIssueTimeDateOptional = function () {
        const noDate = (window.__logHasDate === false);
        const hint = document.getElementById('it-nodate-hint');
        if (hint) hint.style.display = noDate ? 'block' : 'none';
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
        if (typeof updateIssueTimeDisplay === 'function') updateIssueTimeDisplay();
        if (typeof validateUserInput === 'function') validateUserInput();
    };

    WifiIssueTimeStrategy.prototype.removeEitRow = function (btn) {
        const row = btn.closest('.eit-row');
        if (row) row.remove();
        // Refresh sidebar count (no-op if the removed row was in the popup).
        if (typeof _updateMultiToggleCount === 'function') _updateMultiToggleCount();
    };

    WifiIssueTimeStrategy.prototype.updateIssueTimeDisplay = function () {
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
                display.style.color = '#0071c5';
                display.textContent = `\u2192 ${composed}`;
            } else if (anyFilled) {
                display.style.color = '#888';
                display.textContent = '\u2192 (incomplete)';
            } else {
                display.textContent = '';
            }
        }
        // Refresh the "Customer wall clock" annotation row whenever the
        // composed value changes. populateCustomerAnnotation() recomputes the
        // conversion live from the customer IANA zone (DST-aware) or, as a
        // fallback, the label's fixed UTC/GMT offset — so it tracks every edit
        // to the picker without consulting the backend annotation map.
        if (typeof populateCustomerAnnotation === 'function') {
            populateCustomerAnnotation();
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

    window.createIssueTimeStrategy = (profile) => new WifiIssueTimeStrategy(profile);
