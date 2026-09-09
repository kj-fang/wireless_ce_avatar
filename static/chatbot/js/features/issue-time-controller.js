    const VALID_TIME_REGEXES = [
        /\b\d{1,2}\/\d{1,2}\/\d{4}[\s-]\d{1,2}:\d{2}:\d{2}(?:\.\d{1,3})?\b/,
        /\b\d{4}[-/]\d{1,2}[-/]\d{1,2}[\sT]\d{1,2}:\d{2}:\d{2}\b/
    ];

    // Shown as a small badge beside the "Issue Time" heading so the user can
    // tell an attachment's own timestamp apart from one the organizer parsed
    // out of the customer's description — they carry different confidence.
    const ISSUE_TIME_SOURCE = {
        ATTACHMENT:  'From selected attachment issue time',
        DESCRIPTION: 'From customer-provided issue time (description)',
    };

    function _setIssueTimeSourceTag(label) {
        const tag = document.getElementById('it-time-source-tag');
        if (!tag) return;
        tag.textContent = label || '';
        tag.style.display = label ? 'inline-block' : 'none';
    }
    window.ISSUE_TIME_SOURCE = ISSUE_TIME_SOURCE;
    window._setIssueTimeSourceTag = _setIssueTimeSourceTag;

    const IT_RANGES = {
        'it-month': [1, 12],
        'it-day':   [1, 31],
        'it-year':  [1970, 2999],
        'it-hour':  [0, 23],
        'it-min':   [0, 59],
        'it-sec':   [0, 59],
        'it-ms':    [0, 999],
    };

    const IT_REQUIRED = ['it-month', 'it-day', 'it-year', 'it-hour', 'it-min', 'it-sec'];

    const IT_PART_LABELS = {
        'it-month': 'Month', 'it-day': 'Day', 'it-year': 'Year',
        'it-hour': 'Hour',   'it-min': 'Minute', 'it-sec': 'Second',
        'it-ms':   'Millisecond',
    };

    class IssueTimeController {
        constructor(profile, strategy) {
            this.profile = Object.freeze({ ...(profile || {}) });
            this.strategy = strategy;
        }
    }

    IssueTimeController.prototype.inputHasValidTime = function (text) {
        return VALID_TIME_REGEXES.some(r => r.test(text));
    };

    IssueTimeController.prototype.pad2 = function (n) { return String(n).padStart(2, '0'); };

    IssueTimeController.prototype.pad3 = function (n) { return String(n).padStart(3, '0'); };

    IssueTimeController.prototype._itRequiredIds = function () {
        return (window.__logHasDate === false)
            ? ['it-hour', 'it-min', 'it-sec']
            : IT_REQUIRED;
    };

    IssueTimeController.prototype.getPart = function (id) {
        const el = document.getElementById(id);
        if (!el || el.value === '') return null;
        const n = parseInt(el.value, 10);
        return isNaN(n) ? null : n;
    };

    IssueTimeController.prototype.partInRange = function (id, val) {
        if (val === null) return false;
        const [lo, hi] = IT_RANGES[id];
        return val >= lo && val <= hi;
    };

    IssueTimeController.prototype.markFieldValid = function (id, ok) {
        const el = document.getElementById(id);
        if (!el) return;
        if (ok) el.classList.remove('it-invalid');
        else    el.classList.add('it-invalid');
    };

    IssueTimeController.prototype.onPartChange = function (id, max, nextId) {
        const el = document.getElementById(id);
        if (!el) return;
        let v = el.value;
        // Strip non-digits
        if (v && /\D/.test(v)) {
            v = v.replace(/\D/g, '');
            el.value = v;
        }
        // Clamp to max if user typed something larger
        if (v !== '' && max !== null) {
            const n = parseInt(v, 10);
            if (!isNaN(n) && n > max) {
                el.value = String(max);
                v = el.value;
            }
        }
        // Smart clamps that depend on other fields:
        //   - Year   → no later than the current calendar year
        //   - Day    → no more than the actual days in the chosen month
        clampIssueTimeFields(id);
        v = el.value;

        // Auto-advance
        const expectedLen = (id === 'it-year') ? 4 : (id === 'it-ms' ? 3 : 2);
        if (nextId && v.length >= expectedLen) {
            const nextEl = document.getElementById(nextId);
            if (nextEl && document.activeElement === el) nextEl.focus();
        }
        // A manual edit makes the time user-owned (no confirmation needed).
        markIssueTimeUserOwned();
        updateIssueTimeDisplay();
        validateUserInput();
    };

    IssueTimeController.prototype.clampIssueTimeFields = function (changedId) {
        const yearEl = document.getElementById('it-year');
        const monthEl = document.getElementById('it-month');
        const dayEl = document.getElementById('it-day');

        // Year: not in the future
        if (yearEl && yearEl.value !== '') {
            const yr = parseInt(yearEl.value, 10);
            const nowYr = new Date().getFullYear();
            if (!isNaN(yr) && yr > nowYr) {
                yearEl.value = String(nowYr);
            }
        }

        // Day: not greater than days-in-month for the entered month/year
        if (monthEl && monthEl.value !== '' && yearEl && yearEl.value !== '' &&
            dayEl && dayEl.value !== '') {
            const mo = parseInt(monthEl.value, 10);
            const yr = parseInt(yearEl.value, 10);
            const dy = parseInt(dayEl.value, 10);
            if (!isNaN(mo) && !isNaN(yr) && !isNaN(dy) && mo >= 1 && mo <= 12) {
                const maxDay = daysInMonth(yr, mo);
                if (dy > maxDay) dayEl.value = String(maxDay);
            }
        }
    };

    IssueTimeController.prototype.daysInMonth = function (year, month) {
        if (month < 1 || month > 12) return 31;
        if (month === 2) {
            const leap = (year % 4 === 0 && year % 100 !== 0) || (year % 400 === 0);
            return leap ? 29 : 28;
        }
        return [1,3,5,7,8,10,12].includes(month) ? 31 : 30;
    };

    IssueTimeController.prototype.checkIssueTime = function () {
        const ids = ['it-month','it-day','it-year','it-hour','it-min','it-sec','it-ms'];
        const filled = {};
        let anyFilled = false;
        let anyMissing = false;
        const invalidIds = new Set();

        const required = _itRequiredIds();
        ids.forEach(id => {
            const el = document.getElementById(id);
            if (!el || el.value === '') {
                if (required.includes(id)) anyMissing = true;
                return;
            }
            anyFilled = true;
            const n = parseInt(el.value, 10);
            filled[id] = isNaN(n) ? null : n;
        });

        // 1) Per-field range checks (e.g. month 1-12, hour 0-23)
        for (const id of ids) {
            if (!(id in filled)) continue;
            if (!partInRange(id, filled[id])) {
                invalidIds.add(id);
                const [lo, hi] = IT_RANGES[id];
                return {
                    complete: false, valid: false, anyFilled, invalidIds,
                    message: `${IT_PART_LABELS[id]} must be ${lo}\u2013${hi}.`,
                };
            }
        }

        // 2) Calendar-day sanity (April 31, Feb 30, leap year, etc.) —
        // run AS SOON AS Month / Day / Year are present, even if the
        // time fields are still empty, so the user gets immediate feedback.
        if ('it-month' in filled && 'it-day' in filled && 'it-year' in filled) {
            const yr = filled['it-year'], mo = filled['it-month'], dy = filled['it-day'];
            const maxDay = daysInMonth(yr, mo);
            if (dy > maxDay) {
                invalidIds.add('it-day');
                const monthName = ['January','February','March','April','May','June',
                                   'July','August','September','October','November','December'][mo - 1];
                const leapNote = (mo === 2 && dy === 29) ? ` (${yr} is not a leap year)` : '';
                return {
                    complete: false, valid: false, anyFilled, invalidIds,
                    message: `${monthName} ${yr} only has ${maxDay} days${leapNote}. Day cannot be ${dy}.`,
                };
            }
        }

        // 3) Missing required parts
        if (anyMissing) {
            return {
                complete: false, valid: false, anyFilled, invalidIds,
                message: anyFilled
                    ? 'Issue Time is incomplete \u2014 please fill in every field (Month, Day, Year, Hour, Minute, Second).'
                    : '',
            };
        }

        // 4) Future-time check (only when a full date is present)
        if ('it-year' in filled && 'it-month' in filled && 'it-day' in filled) {
            const yr = filled['it-year'], mo = filled['it-month'], dy = filled['it-day'];
            const ms = ('it-ms' in filled) ? filled['it-ms'] : 0;
            const candidate = new Date(yr, mo - 1, dy,
                                       filled['it-hour'], filled['it-min'],
                                       filled['it-sec'], ms);
            if (candidate.getTime() > Date.now()) {
                ['it-year','it-month','it-day','it-hour','it-min','it-sec']
                    .forEach(id => invalidIds.add(id));
                return {
                    complete: true, valid: false, anyFilled, invalidIds,
                    message: 'Issue Time cannot be in the future.',
                };
            }
        }

        return { complete: true, valid: true, anyFilled, invalidIds, message: '' };
    };

    IssueTimeController.prototype.getIssueTimeString = function () {
        const r = checkIssueTime();
        if (!r.valid) return '';

        const mo = getPart('it-month'), dy = getPart('it-day'), yr = getPart('it-year');
        const hh = pad2(getPart('it-hour'));
        const mi = pad2(getPart('it-min'));
        const ss = pad2(getPart('it-sec'));
        const ms = getPart('it-ms');
        // Full date present → "MM/DD/YYYY-HH:MM:SS"; otherwise (date-optional
        // log) emit a time-only "HH:MM:SS" string.
        let result = (mo !== null && dy !== null && yr !== null)
            ? `${pad2(mo)}/${pad2(dy)}/${String(yr)}-${hh}:${mi}:${ss}`
            : `${hh}:${mi}:${ss}`;
        if (ms !== null) result += '.' + pad3(ms);
        return result;
    };

    IssueTimeController.prototype.setIssueTimeFromString = function (timeStr) {
        if (!timeStr) return false;
        const m1 = timeStr.match(/(\d{1,2})\/(\d{1,2})\/(\d{4})[\s-](\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?/);
        const m2 = timeStr.match(/(\d{4})[-/](\d{1,2})[-/](\d{1,2})[\sT](\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?/);
        // Time-only: "HH:MM:SS" or "HH:MM:SS.mmm" — populates time fields, leaves date blank.
        const m3 = timeStr.match(/^(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?$/);
        let yyyy, mm, dd, hh, mi, ss, ms;
        const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
        if (m1) {
            [, mm, dd, yyyy, hh, mi, ss, ms] = m1;
            setVal('it-month', String(parseInt(mm, 10)));
            setVal('it-day',   String(parseInt(dd, 10)));
            setVal('it-year',  String(parseInt(yyyy, 10)));
            setVal('it-hour',  String(parseInt(hh, 10)));
            setVal('it-min',   String(parseInt(mi, 10)));
            setVal('it-sec',   String(parseInt(ss, 10)));
            setVal('it-ms',    ms ? String(parseInt(ms.padEnd(3, '0').slice(0, 3), 10)) : '');
        } else if (m2) {
            [, yyyy, mm, dd, hh, mi, ss, ms] = m2;
            setVal('it-month', String(parseInt(mm, 10)));
            setVal('it-day',   String(parseInt(dd, 10)));
            setVal('it-year',  String(parseInt(yyyy, 10)));
            setVal('it-hour',  String(parseInt(hh, 10)));
            setVal('it-min',   String(parseInt(mi, 10)));
            setVal('it-sec',   String(parseInt(ss, 10)));
            setVal('it-ms',    ms ? String(parseInt(ms.padEnd(3, '0').slice(0, 3), 10)) : '');
        } else if (m3) {
            [, hh, mi, ss, ms] = m3;
            setVal('it-hour', String(parseInt(hh, 10)));
            setVal('it-min',  String(parseInt(mi, 10)));
            setVal('it-sec',  String(parseInt(ss, 10)));
            setVal('it-ms',   ms ? String(parseInt(ms.padEnd(3, '0').slice(0, 3), 10)) : '');
        } else {
            return false;
        }
        updateIssueTimeDisplay();
        validateUserInput();
        return true;
    };

    IssueTimeController.prototype.clearIssueTime = function () {
        ['it-month','it-day','it-year','it-hour','it-min','it-sec','it-ms'].forEach(id => {
            const el = document.getElementById(id);
            if (el) { el.value = ''; el.classList.remove('it-invalid'); }
        });
        // Also clear the hidden native picker so it can't leak a stale value.
        const picker = document.getElementById('issue-time-picker');
        if (picker) picker.value = '';
        // Once the user explicitly clears, hide the unknown-time warning.
        const warn = document.getElementById('it-unknown-warn');
        if (warn) warn.style.display = 'none';
        // Clearing also drops any pending auto-fill confirmation.
        issueTimeAwaitingConfirm = false;
        setIssueTimeConfirmUI(false);
        updateIssueTimeDisplay();
        validateUserInput();
    };

    IssueTimeController.prototype.clearAllIssueTimes = function () {
        clearAllExtraIssueTimes();
        clearIssueTime();
        // The badge described where the CLEARED value came from; leaving it up
        // would label whatever the user types next as coming from that source.
        _setIssueTimeSourceTag('');
    };

    IssueTimeController.prototype.setIssueTimeNow = function () {
        const d = new Date();
        const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
        setVal('it-month', String(d.getMonth() + 1));
        setVal('it-day',   String(d.getDate()));
        setVal('it-year',  String(d.getFullYear()));
        setVal('it-hour',  String(d.getHours()));
        setVal('it-min',   String(d.getMinutes()));
        setVal('it-sec',   String(d.getSeconds()));
        setVal('it-ms',    String(d.getMilliseconds()));
        updateIssueTimeDisplay();
        validateUserInput();
    };

    IssueTimeController.prototype.openCalendarPicker = function () {
        const picker = document.getElementById('issue-time-picker');
        if (!picker) return;
        // Pre-fill the picker from current fields if all required parts exist
        const yyyy = getPart('it-year'), mm = getPart('it-month'), dd = getPart('it-day');
        const hh = getPart('it-hour'), mi = getPart('it-min'), ss = getPart('it-sec');
        if (yyyy && mm && dd && hh !== null && mi !== null && ss !== null) {
            picker.value = `${yyyy}-${pad2(mm)}-${pad2(dd)}T${pad2(hh)}:${pad2(mi)}:${pad2(ss)}`;
        }
        // Make the picker briefly focusable so showPicker / click works
        picker.style.pointerEvents = 'auto';
        try {
            if (typeof picker.showPicker === 'function') {
                picker.showPicker();
            } else {
                picker.focus();
                picker.click();
            }
        } catch (e) {
            picker.focus();
            picker.click();
        }
        setTimeout(() => { picker.style.pointerEvents = 'none'; }, 100);
    };

    IssueTimeController.prototype.stripTimestampsFromDesc = function (text) {
        let out = text;
        VALID_TIME_REGEXES.forEach(r => {
            out = out.replace(new RegExp(r.source, 'g'), ' ');
        });
        // Also remove obvious bare time fragments (e.g. "14:50:51.910").
        out = out.replace(/\b\d{1,2}:\d{2}:\d{2}(?:\.\d{1,3})?\b/g, ' ');
        return out.replace(/\s+/g, ' ').trim();
    };

    IssueTimeController.prototype.trimTrailingTimeConnector = function (text) {
        return text.replace(/[\s,;:.\-]*\b(at\s+around|at|around|on)\b[\s,;:.\-]*$/i, '').trim();
    };

    IssueTimeController.prototype.validateUserInput = function (submitting = false) {
        const text = document.getElementById('user-input').value || '';
        const noLogHint = document.getElementById('no-log-hint');
        const noDescHint = document.getElementById('no-desc-hint');
        const noTimeHint = document.getElementById('no-time-hint');
        const sendBtn = document.getElementById('send-btn');

        // Hide the (now legacy) "invalid time format" banner; we no longer
        // accept free-form time in the textarea.
        const timeHint = document.getElementById('time-hint');
        if (timeHint) timeHint.style.display = 'none';

        // Hard blockers (Send button stays disabled until resolved):
        //   - first round without a loaded log
        //   - empty description
        // Soft warning (Send still enabled, but a confirmation modal is
        // shown on click): first round with no Issue Time selected.
        let valid = true;
        let showNoLogHint = false;
        let showNoDescHint = false;
        let showNoTimeHint = false;

        const firstRound = !firstRoundSent;
        // An auto-filled (previous-page) time only counts once confirmed.
        const hasIssueTime = !!getIssueTimeString() && !issueTimeAwaitingConfirm;

        if (firstRound && !logLoaded) {
            valid = false;
            // Log-not-loaded is a structural blocker -> always surface.
            showNoLogHint = true;
        } else if (!text.trim()) {
            valid = false;
            // Empty description: only nag on explicit submit, not while
            // the user is still typing or has just landed on the page.
            if (submitting) showNoDescHint = true;
        }

        // Soft hint: missing time on first round (does not block send)
        if (valid && (firstRound || isAutoFillMode) && !hasIssueTime) {
            showNoTimeHint = true;
        }

        if (noLogHint) noLogHint.style.display = showNoLogHint ? 'block' : 'none';
        if (noDescHint) noDescHint.style.display = showNoDescHint ? 'block' : 'none';
        if (noTimeHint) {
            if (showNoTimeHint) {
                // Tailor the message: a time pending confirmation just needs a
                // tick; a genuinely empty Issue Time needs to be filled in.
                noTimeHint.innerHTML = issueTimeAwaitingConfirm
                    ? '<strong>⏰ Time(s) auto-detected from the previous page.</strong> Tick the checkbox on the left to use them — or edit the fields / use 🪄 AI suggest.'
                    : '<strong>⏰ Issue Time is missing or incomplete.</strong> For best results, fill in every field on the left (Month / Day / Year / Hour / Minute / Second). You can still send without a time — you\'ll be asked to confirm.';
                noTimeHint.style.display = 'block';
            } else {
                noTimeHint.style.display = 'none';
            }
        }
        // While an analysis streams the Send button is acting as Stop, and it
        // must stay clickable no matter what the user types in the meantime —
        // a disabled button swallows the click even though the .stopping style
        // still renders it as active. core.js re-derives the real disabled
        // state from this function once the stream ends.
        const streaming = typeof isChatStreaming === 'function' && isChatStreaming();
        if (sendBtn && !streaming) sendBtn.disabled = !valid;
        return valid;
    };

    IssueTimeController.prototype.setIssueTimeConfirmUI = function (show) {
        const cb = document.getElementById('it-confirm-autofill');
        if (cb) cb.checked = false;
        const row = document.getElementById('it-confirm-row');
        if (row) row.style.display = show ? 'block' : 'none';
    };

    IssueTimeController.prototype.markIssueTimeUserOwned = function () {
        // Manual edit / AI-apply: the value is the user's now, so the
        // provenance badge no longer describes it.
        _setIssueTimeSourceTag('');
        if (issueTimeAwaitingConfirm) {
            issueTimeAwaitingConfirm = false;
            setIssueTimeConfirmUI(false);
        }
    };

    IssueTimeController.prototype.confirmNoIssueTime = function () {
        return new Promise(resolve => {
            _confirmModalResolver = resolve;
            const overlay = document.getElementById('confirm-modal');
            if (!overlay) { resolve(true); return; }
            overlay.style.display = 'flex';
            const btn = document.getElementById('cm-confirm-btn');
            if (btn) setTimeout(() => btn.focus(), 30);
            document.addEventListener('keydown', _confirmModalKeyHandler);
        });
    };

    IssueTimeController.prototype.closeConfirmModal = function (result) {
        const overlay = document.getElementById('confirm-modal');
        if (overlay) overlay.style.display = 'none';
        document.removeEventListener('keydown', _confirmModalKeyHandler);
        if (_confirmModalResolver) {
            const r = _confirmModalResolver;
            _confirmModalResolver = null;
            r(!!result);
        }
    };

    IssueTimeController.prototype.onConfirmModalBackdrop = function (e) {
        if (e.target && e.target.id === 'confirm-modal') closeConfirmModal(false);
    };

    IssueTimeController.prototype.chooseAiSuggestFromConfirm = function () {
        closeConfirmModal(false);
        if (typeof suggestIssueTimesWithAI === 'function') suggestIssueTimesWithAI();
    };

    IssueTimeController.prototype.useLogLastTimeAndSend = function () {
        const t = window.__logLastTime || '';
        if (t && typeof setIssueTimeFromString === 'function' && setIssueTimeFromString(t)) {
            if (typeof markIssueTimeUserOwned === 'function') markIssueTimeUserOwned();
            closeConfirmModal(false);   // cancel this attempt; re-send with the time set
            sendMessage();
        } else {
            closeConfirmModal(true);    // no readable timestamp → analyse without a time
        }
    };

    IssueTimeController.prototype._confirmModalKeyHandler = function (e) {
        if (e.key === 'Escape') closeConfirmModal(false);
        else if (e.key === 'Enter') useLogLastTimeAndSend();
    };

    IssueTimeController.prototype._issueWindowMax = function () {
        const span = window.__logSpanMinutes;
        return (typeof span === 'number' && span > 0) ? span : 120;
    };

    IssueTimeController.prototype.getIssueWindowMinutes = function () {
        const el = document.getElementById('it-window-min');
        let v = parseInt(el ? el.value : '5', 10);
        if (isNaN(v)) v = 5;
        return Math.max(0, Math.min(_issueWindowMax(), v));
    };

    IssueTimeController.prototype.refreshIssueWindowBounds = function () {
        const el = document.getElementById('it-window-min');
        if (!el) return;
        const maxV = _issueWindowMax();
        el.min = '0';
        el.max = String(maxV);
        // Clamp the current value into [0, maxV].
        let v = parseInt(el.value, 10);
        if (!isNaN(v)) {
            const clamped = Math.max(0, Math.min(maxV, v));
            if (clamped !== v) el.value = String(clamped);
        }
        const hintMax = document.getElementById('itw-max');
        if (hintMax) hintMax.textContent = String(maxV);
        onIssueWindowChange();
    };

    IssueTimeController.prototype.onIssueWindowChange = function () {
        const el = document.getElementById('it-window-min');
        if (!el) return;
        const maxV = _issueWindowMax();
        let v = parseInt(el.value, 10);
        // Hard-clamp negatives / over-max as the user types so the box
        // can never hold an out-of-range value (the screenshot bug was
        // a typed "-33").
        if (!isNaN(v)) {
            const clamped = Math.max(0, Math.min(maxV, v));
            if (String(clamped) !== el.value) el.value = String(clamped);
            v = clamped;
        }
        const shown = isNaN(v) ? '—' : String(v);
        const r1 = document.getElementById('itw-readout');
        const r2 = document.getElementById('itw-readout2');
        if (r1) r1.textContent = shown;
        if (r2) r2.textContent = shown;
    };

    IssueTimeController.prototype.stepIssueWindow = function (delta) {
        const el = document.getElementById('it-window-min');
        if (!el) return;
        const maxV = _issueWindowMax();
        el.value = String(Math.max(0, Math.min(maxV, getIssueWindowMinutes() + delta)));
        onIssueWindowChange();
    };

    IssueTimeController.prototype._uuid4 = function () {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') {
            return window.crypto.randomUUID();
        }
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
            const r = Math.random() * 16 | 0;
            const v = c === 'x' ? r : (r & 0x3) | 0x8;
            return v.toString(16);
        });
    };

    IssueTimeController.prototype._detectIssueTimesInText = function (text) {
        if (!text || typeof text !== 'string') return [];
        // Seconds are optional so incomplete clock times like "23:21" are
        // detected too; the missing second defaults to 0 and the date is
        // borrowed from the loaded log when the user reviews the popup.
        const re = /\b(\d{1,2}):([0-5]?\d)(?::([0-5]?\d))?(?:\.(\d{1,3}))?\b/g;
        const out = [];
        const seen = new Set();
        let m;
        while ((m = re.exec(text)) !== null) {
            const H = parseInt(m[1], 10);
            const M = parseInt(m[2], 10);
            const S = m[3] != null ? parseInt(m[3], 10) : 0;
            if (H > 23 || M > 59 || S > 59) continue;
            const key = `${H}:${M}:${S}.${m[4] || ''}`;
            if (seen.has(key)) continue;
            seen.add(key);
            out.push({
                hh: H, mm: M, ss: S,
                ms: m[4] != null ? parseInt(m[4], 10) : null,
                raw: m[0], index: m.index,
            });
        }
        return out;
    };

    IssueTimeController.prototype._getReferenceDateForCapture = function () {
        const num = (id) => {
            const el = document.getElementById(id);
            const v = el ? el.value : '';
            return v === '' ? null : parseInt(v, 10);
        };
        const sidebar = {
            month: num('it-month'),
            day:   num('it-day'),
            year:  num('it-year'),
        };
        if (sidebar.month != null && sidebar.day != null && sidebar.year != null) {
            return sidebar;
        }
        if (window.__logAutoDate
            && window.__logAutoDate.month != null
            && window.__logAutoDate.day   != null
            && window.__logAutoDate.year  != null) {
            return { ...window.__logAutoDate };
        }
        return null;
    };

    IssueTimeController.prototype._extractDateFromTimeString = function (s) {
        if (!s || typeof s !== 'string') return null;
        let m = s.match(/(\d{1,2})\/(\d{1,2})\/(\d{4})/);     // MM/DD/YYYY
        if (m) return { month: +m[1], day: +m[2], year: +m[3] };
        m = s.match(/(\d{4})[-/](\d{1,2})[-/](\d{1,2})/);    // YYYY-MM-DD
        if (m) return { year: +m[1], month: +m[2], day: +m[3] };
        return null;
    };

    IssueTimeController.prototype.closeTimeCaptureModal = function () {
        const modal = document.getElementById('time-capture-modal');
        modal.classList.remove('open');
        modal.setAttribute('aria-hidden', 'true');
    };

    IssueTimeController.prototype._readEitRow = function (row) {
        const num = (sel) => {
            const v = row.querySelector(sel).value;
            return v === '' ? null : parseInt(v, 10);
        };
        return {
            month: num('.eit-month'),
            day:   num('.eit-day'),
            year:  num('.eit-year'),
            hh:    num('.eit-hour'),
            mm:    num('.eit-min'),
            ss:    num('.eit-sec'),
            ms:    num('.eit-ms'),
        };
    };

    IssueTimeController.prototype._formatEitRowString = function (t) {
        if (!t || t.hh == null || t.mm == null || t.ss == null) return '';
        const pad2 = (n) => String(n).padStart(2, '0');
        const pad3 = (n) => String(n).padStart(3, '0');
        let s = '';
        if (t.month != null && t.day != null) {
            s += pad2(t.month) + '/' + pad2(t.day);
            if (t.year != null) s += '/' + String(t.year);
            s += ' ';
        }
        s += pad2(t.hh) + ':' + pad2(t.mm) + ':' + pad2(t.ss);
        if (t.ms != null) s += '.' + pad3(t.ms);
        return s;
    };

    IssueTimeController.prototype._readPrimaryIssueTime = function () {
        // An unconfirmed auto-filled time does not count as a usable time.
        if (issueTimeAwaitingConfirm) return null;
        const num = (id) => {
            const el = document.getElementById(id);
            const v = el ? el.value : '';
            return v === '' ? null : parseInt(v, 10);
        };
        const t = {
            month: num('it-month'),
            day:   num('it-day'),
            year:  num('it-year'),
            hh:    num('it-hour'),
            mm:    num('it-min'),
            ss:    num('it-sec'),
            ms:    num('it-ms'),
        };
        return (t.hh != null && t.mm != null && t.ss != null) ? t : null;
    };

    IssueTimeController.prototype._parseCanonicalToRow = function (s) {
        if (!s) return null;
        let m = String(s).match(/(\d{1,2})\/(\d{1,2})\/(\d{4})[-\s](\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?/);
        if (m) {
            return { month:+m[1], day:+m[2], year:+m[3], hh:+m[4], mm:+m[5], ss:+m[6],
                     ms: m[7] != null ? parseInt(m[7].padEnd(3,'0').slice(0,3),10) : null };
        }
        m = String(s).match(/^(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?$/);
        if (m) {
            return { month:null, day:null, year:null, hh:+m[1], mm:+m[2], ss:+m[3],
                     ms: m[4] != null ? parseInt(m[4].padEnd(3,'0').slice(0,3),10) : null };
        }
        return null;
    };

    IssueTimeController.prototype.suggestIssueTimesWithAI = function () {
        const input = document.getElementById('user-input');
        const text = (input ? input.value : '').trim();
        const nodesc = document.getElementById('ai-time-nodesc');
        // An empty chat box is allowed — the AI can still infer the issue time
        // from the log alone. Show a non-blocking hint (not an error) nudging
        // the user to add a description for better accuracy, then continue.
        if (nodesc) nodesc.style.display = text ? 'none' : 'block';
        _llmTimeConsentText = text;   // may be empty
        const prev = document.getElementById('llm-consent-text');
        if (prev) prev.textContent = text
            || '(no description — the AI will infer the issue time from the log only)';
        const loghint = document.getElementById('llm-consent-loghint');
        if (loghint) {
            const hasLog = (typeof logLoaded !== 'undefined' && logLoaded) || !!window.__logSpanMinutes;
            loghint.innerHTML = hasLog
                ? '✔ A log is loaded and will be sampled to anchor the suggestion.'
                : '⚠️ No log loaded — load a log so the AI has something to analyze.';
            loghint.style.color = hasLog ? '#15803d' : '#92400e';
        }
        openLlmTimeConsent();
    };

    IssueTimeController.prototype.openLlmTimeConsent = function () {
        const m = document.getElementById('llm-time-consent-modal');
        if (!m) return;
        m.classList.add('open');
        m.setAttribute('aria-hidden', 'false');
    };

    IssueTimeController.prototype.closeLlmTimeConsent = function () {
        const m = document.getElementById('llm-time-consent-modal');
        if (!m) return;
        m.classList.remove('open');
        m.setAttribute('aria-hidden', 'true');
    };

    IssueTimeController.prototype._applyCapturedTimesToSidebar = function (...args) {
        return this.strategy._applyCapturedTimesToSidebar(...args);
    };
    IssueTimeController.prototype._itEscHtml = function (...args) {
        return this.strategy._itEscHtml(...args);
    };
    IssueTimeController.prototype._populateEitRow = function (...args) {
        return this.strategy._populateEitRow(...args);
    };
    IssueTimeController.prototype._prefillAutoTimes = function (...args) {
        return this.strategy._prefillAutoTimes(...args);
    };
    IssueTimeController.prototype._setAiTimeSummary = function (...args) {
        return this.strategy._setAiTimeSummary(...args);
    };
    IssueTimeController.prototype._writeTimeToPrimary = function (...args) {
        return this.strategy._writeTimeToPrimary(...args);
    };
    IssueTimeController.prototype.addExtraIssueTimeRow = function (...args) {
        return this.strategy.addExtraIssueTimeRow(...args);
    };
    IssueTimeController.prototype.addTimeCaptureRow = function (...args) {
        return this.strategy.addTimeCaptureRow(...args);
    };
    IssueTimeController.prototype.applyTimeCapture = function (...args) {
        return this.strategy.applyTimeCapture(...args);
    };
    IssueTimeController.prototype.clearAllExtraIssueTimes = function (...args) {
        return this.strategy.clearAllExtraIssueTimes(...args);
    };
    IssueTimeController.prototype.confirmLlmTimeConsent = function (...args) {
        return this.strategy.confirmLlmTimeConsent(...args);
    };
    IssueTimeController.prototype.getAllIssueTimes = function (...args) {
        return this.strategy.getAllIssueTimes(...args);
    };
    IssueTimeController.prototype.getAllIssueTimesParsed = function (...args) {
        return this.strategy.getAllIssueTimesParsed(...args);
    };
    IssueTimeController.prototype.onCalendarPicked = function (...args) {
        return this.strategy.onCalendarPicked(...args);
    };
    IssueTimeController.prototype.onUserInputChange = function (...args) {
        return this.strategy.onUserInputChange(...args);
    };
    IssueTimeController.prototype.openTimeCaptureModal = function (...args) {
        return this.strategy.openTimeCaptureModal(...args);
    };
    IssueTimeController.prototype.refreshIssueTimeDateOptional = function (...args) {
        return this.strategy.refreshIssueTimeDateOptional(...args);
    };
    IssueTimeController.prototype.removeEitRow = function (...args) {
        return this.strategy.removeEitRow(...args);
    };
    IssueTimeController.prototype.updateIssueTimeDisplay = function (...args) {
        return this.strategy.updateIssueTimeDisplay(...args);
    };

    const issueTimeProfile =
        (window.CHATBOT && window.CHATBOT.issue_time) || {};
    const issueTimeStrategy =
        typeof window.createIssueTimeStrategy === 'function'
            ? window.createIssueTimeStrategy(issueTimeProfile)
            : null;
    window.issueTimeController = new IssueTimeController(
        issueTimeProfile,
        issueTimeStrategy
    );
    window._applyCapturedTimesToSidebar = (...args) => window.issueTimeController._applyCapturedTimesToSidebar(...args);
    window._itEscHtml = (...args) => window.issueTimeController._itEscHtml(...args);
    window._populateEitRow = (...args) => window.issueTimeController._populateEitRow(...args);
    window._prefillAutoTimes = (...args) => window.issueTimeController._prefillAutoTimes(...args);
    window._setAiTimeSummary = (...args) => window.issueTimeController._setAiTimeSummary(...args);
    window._writeTimeToPrimary = (...args) => window.issueTimeController._writeTimeToPrimary(...args);
    window.addExtraIssueTimeRow = (...args) => window.issueTimeController.addExtraIssueTimeRow(...args);
    window.addTimeCaptureRow = (...args) => window.issueTimeController.addTimeCaptureRow(...args);
    window.applyTimeCapture = (...args) => window.issueTimeController.applyTimeCapture(...args);
    window.clearAllExtraIssueTimes = (...args) => window.issueTimeController.clearAllExtraIssueTimes(...args);
    window.confirmLlmTimeConsent = (...args) => window.issueTimeController.confirmLlmTimeConsent(...args);
    window.getAllIssueTimes = (...args) => window.issueTimeController.getAllIssueTimes(...args);
    window.getAllIssueTimesParsed = (...args) => window.issueTimeController.getAllIssueTimesParsed(...args);
    window.onCalendarPicked = (...args) => window.issueTimeController.onCalendarPicked(...args);
    window.onUserInputChange = (...args) => window.issueTimeController.onUserInputChange(...args);
    window.openTimeCaptureModal = (...args) => window.issueTimeController.openTimeCaptureModal(...args);
    window.refreshIssueTimeDateOptional = (...args) => window.issueTimeController.refreshIssueTimeDateOptional(...args);
    window.removeEitRow = (...args) => window.issueTimeController.removeEitRow(...args);
    window.updateIssueTimeDisplay = (...args) => window.issueTimeController.updateIssueTimeDisplay(...args);
    window.inputHasValidTime = (...args) => window.issueTimeController.inputHasValidTime(...args);
    window.pad2 = (...args) => window.issueTimeController.pad2(...args);
    window.pad3 = (...args) => window.issueTimeController.pad3(...args);
    window._itRequiredIds = (...args) => window.issueTimeController._itRequiredIds(...args);
    window.getPart = (...args) => window.issueTimeController.getPart(...args);
    window.partInRange = (...args) => window.issueTimeController.partInRange(...args);
    window.markFieldValid = (...args) => window.issueTimeController.markFieldValid(...args);
    window.onPartChange = (...args) => window.issueTimeController.onPartChange(...args);
    window.clampIssueTimeFields = (...args) => window.issueTimeController.clampIssueTimeFields(...args);
    window.daysInMonth = (...args) => window.issueTimeController.daysInMonth(...args);
    window.checkIssueTime = (...args) => window.issueTimeController.checkIssueTime(...args);
    window.getIssueTimeString = (...args) => window.issueTimeController.getIssueTimeString(...args);
    window.setIssueTimeFromString = (...args) => window.issueTimeController.setIssueTimeFromString(...args);
    window.clearIssueTime = (...args) => window.issueTimeController.clearIssueTime(...args);
    window.clearAllIssueTimes = (...args) => window.issueTimeController.clearAllIssueTimes(...args);
    window.setIssueTimeNow = (...args) => window.issueTimeController.setIssueTimeNow(...args);
    window.openCalendarPicker = (...args) => window.issueTimeController.openCalendarPicker(...args);
    window.stripTimestampsFromDesc = (...args) => window.issueTimeController.stripTimestampsFromDesc(...args);
    window.trimTrailingTimeConnector = (...args) => window.issueTimeController.trimTrailingTimeConnector(...args);
    window.validateUserInput = (...args) => window.issueTimeController.validateUserInput(...args);
    window.setIssueTimeConfirmUI = (...args) => window.issueTimeController.setIssueTimeConfirmUI(...args);
    window.markIssueTimeUserOwned = (...args) => window.issueTimeController.markIssueTimeUserOwned(...args);
    window.confirmNoIssueTime = (...args) => window.issueTimeController.confirmNoIssueTime(...args);
    window.closeConfirmModal = (...args) => window.issueTimeController.closeConfirmModal(...args);
    window.onConfirmModalBackdrop = (...args) => window.issueTimeController.onConfirmModalBackdrop(...args);
    window.chooseAiSuggestFromConfirm = (...args) => window.issueTimeController.chooseAiSuggestFromConfirm(...args);
    window.useLogLastTimeAndSend = (...args) => window.issueTimeController.useLogLastTimeAndSend(...args);
    window._confirmModalKeyHandler = (...args) => window.issueTimeController._confirmModalKeyHandler(...args);
    window._issueWindowMax = (...args) => window.issueTimeController._issueWindowMax(...args);
    window.getIssueWindowMinutes = (...args) => window.issueTimeController.getIssueWindowMinutes(...args);
    window.refreshIssueWindowBounds = (...args) => window.issueTimeController.refreshIssueWindowBounds(...args);
    window.onIssueWindowChange = (...args) => window.issueTimeController.onIssueWindowChange(...args);
    window.stepIssueWindow = (...args) => window.issueTimeController.stepIssueWindow(...args);
    window._uuid4 = (...args) => window.issueTimeController._uuid4(...args);
    window._detectIssueTimesInText = (...args) => window.issueTimeController._detectIssueTimesInText(...args);
    window._getReferenceDateForCapture = (...args) => window.issueTimeController._getReferenceDateForCapture(...args);
    window._extractDateFromTimeString = (...args) => window.issueTimeController._extractDateFromTimeString(...args);
    window.closeTimeCaptureModal = (...args) => window.issueTimeController.closeTimeCaptureModal(...args);
    window._readEitRow = (...args) => window.issueTimeController._readEitRow(...args);
    window._formatEitRowString = (...args) => window.issueTimeController._formatEitRowString(...args);
    window._readPrimaryIssueTime = (...args) => window.issueTimeController._readPrimaryIssueTime(...args);
    window._parseCanonicalToRow = (...args) => window.issueTimeController._parseCanonicalToRow(...args);
    window.suggestIssueTimesWithAI = (...args) => window.issueTimeController.suggestIssueTimesWithAI(...args);
    window.openLlmTimeConsent = (...args) => window.issueTimeController.openLlmTimeConsent(...args);
    window.closeLlmTimeConsent = (...args) => window.issueTimeController.closeLlmTimeConsent(...args);
