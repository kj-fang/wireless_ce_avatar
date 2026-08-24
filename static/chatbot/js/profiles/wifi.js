    let isAutoFillMode = false;  // true after auto_run prefill; requires a timestamp before sending
    let firstRoundSent = false;  // true after the user successfully sends the first question
    // True when the primary Issue Time was auto-filled from the PREVIOUS PAGE
    // and the user hasn't confirmed it yet. While true the primary time is
    // shown in the fields but does NOT count as a usable issue time (the user
    // must tick the confirm checkbox, edit a field, or apply an AI suggestion).
    let issueTimeAwaitingConfirm = false;

    // Show / hide the inline "Log ends at: HH:MM:SS.mmm" link that sits under
    // the no-date hint. Visible only when:
    //   * the loaded log is time-only (DDD/tracefmt), AND
    //   * the backend successfully extracted a last timestamp.
    // Lets DDD users see the log's last event time immediately on upload
    // (instead of having to send-without-time first and click the modal
    // button to discover it).
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

    // Parse a "(GMT-0500)" / "(UTC-05:00)" / "GMT+8" style suffix into minutes
    // from UTC. Returns null when no offset can be read.
    function _parseTzOffsetMin(label) {
        const m = String(label || '').match(/(?:GMT|UTC)\s*([+-])(\d{1,2})(?::?(\d{2}))?/i);
        if (!m) return null;
        const sign = (m[1] === '-') ? -1 : 1;
        const hh = parseInt(m[2], 10) || 0;
        const mm = parseInt(m[3] || '0', 10) || 0;
        return sign * (hh * 60 + mm);
    }

    // Express a UTC instant as wall-clock parts in an IANA zone (e.g.
    // "America/Los_Angeles"). Intl applies that zone's DST rules for the
    // instant, so the result is correct in both standard and summer time.
    // Returns null if the zone id is unusable.
    function _partsInZone(date, iana) {
        try {
            const fmt = new Intl.DateTimeFormat('en-US', {
                timeZone: iana, hour12: false,
                year: 'numeric', month: '2-digit', day: '2-digit',
                hour: '2-digit', minute: '2-digit', second: '2-digit'
            });
            const o = {};
            for (const p of fmt.formatToParts(date)) {
                if (p.type !== 'literal') o[p.type] = p.value;
            }
            return {
                y: parseInt(o.year, 10), mo: parseInt(o.month, 10),
                d: parseInt(o.day, 10), hh: parseInt(o.hour, 10) % 24,
                mi: parseInt(o.minute, 10), ss: parseInt(o.second, 10)
            };
        } catch (e) { return null; }
    }

    // Update the 👤 Customer time card. The editable fields hold the ETL
    // decode-host (GMT+8) value; this converts that LIVE to the customer wall
    // clock so the card tracks manual edits (no server round-trip). When an
    // IANA zone id is known (window.__customerIana) the conversion is
    // DST-aware for the date in the picker; otherwise it falls back to the
    // label's fixed standard offset (customer = decode_host + (offset − 8h)).
    function populateCustomerAnnotation() {
        const wrap = document.getElementById('issue-time-customer-annotation');
        const valEl = document.getElementById('issue-time-customer-value');
        const tzEl  = document.getElementById('issue-time-customer-tz');
        if (!wrap || !valEl || !tzEl) return;
        const tz = (window.__customerTzLabel || '').trim();
        const iana = (window.__customerIana || '').trim();
        const offMin = _parseTzOffsetMin(tz);
        // Render when EITHER a parseable UTC/GMT offset OR an IANA zone id is
        // available. A label like "Central Standard Time" carries no offset,
        // but the backend can still supply __customerIana for the DST-aware
        // path below, so don't hide on offMin === null alone.
        if (!tz || (offMin === null && !iana)) { wrap.style.display = 'none'; return; }

        const r = (typeof checkIssueTime === 'function') ? checkIssueTime() : { valid: false };
        const hh = getPart('it-hour'), mi = getPart('it-min'), ss = getPart('it-sec');
        if (!r.valid || hh === null || mi === null) { wrap.style.display = 'none'; return; }
        const mo = getPart('it-month'), dy = getPart('it-day'), yr = getPart('it-year');
        const hasDate = (mo !== null && dy !== null && yr !== null);

        const DECODE_HOST_OFFSET_MIN = 8 * 60;          // GMT+8 (decoder host)
        const p2 = n => String(n).padStart(2, '0');
        const ms = getPart('it-ms');
        const msStr = (ms !== null) ? '.' + String(ms).padStart(3, '0') : '';

        // DST-aware path: only meaningful with a real date. Treat the picker
        // value as GMT+8 wall clock, derive the UTC instant, then read it back
        // in the customer IANA zone so summer/winter offsets are correct.
        if (hasDate && iana) {
            const utcMs = Date.UTC(yr, mo - 1, dy, hh, mi, ss || 0)
                          - DECODE_HOST_OFFSET_MIN * 60000;
            const cp = _partsInZone(new Date(utcMs), iana);
            if (cp) {
                const clock = `${p2(cp.hh)}:${p2(cp.mi)}:${p2(cp.ss)}${msStr}`;
                valEl.textContent = `${p2(cp.mo)}/${p2(cp.d)}/${cp.y}-${clock}`;
                tzEl.textContent = tz;
                wrap.style.display = '';
                return;
            }
        }

        // Fallback fixed-offset path. It needs a numeric offset; the IANA
        // path above already handled (and returned for) the DST-aware case.
        // If the label carried no offset (offMin === null) and that path
        // didn't render — no date, or an unusable zone id — there's nothing
        // valid to show, so hide rather than emit a NaN timestamp.
        if (offMin === null) { wrap.style.display = 'none'; return; }
        const deltaMin = offMin - DECODE_HOST_OFFSET_MIN;
        // Do the arithmetic entirely in UTC so the browser's own tz can't skew
        // it. For time-only logs (no date) use a placeholder date and show only
        // the shifted clock.
        const baseY = hasDate ? yr : 2000;
        const baseMo = hasDate ? (mo - 1) : 0;
        const baseD = hasDate ? dy : 1;
        const c = new Date(Date.UTC(baseY, baseMo, baseD, hh, mi, ss || 0) + deltaMin * 60000);
        const clock = `${p2(c.getUTCHours())}:${p2(c.getUTCMinutes())}:${p2(c.getUTCSeconds())}${msStr}`;
        valEl.textContent = hasDate
            ? `${p2(c.getUTCMonth() + 1)}/${p2(c.getUTCDate())}/${c.getUTCFullYear()}-${clock}`
            : clock;
        tzEl.textContent = tz;
        wrap.style.display = '';
    }


    // ── Confirm modal (used when sending without Issue Time) ──────
    let _confirmModalResolver = null;




    // Insert a small "Incident N/M at <time>" chip into the chat — used
    // before each multi-time analysis iteration so the user can scan
    // which result belongs to which issue time.



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
    // For DDD/tracefmt logs (window.__logHasDate === false), strip the
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





