    // Agentic mode is no longer user-selectable: the sidebar "AI Mode" toggle
    // was removed, so the agent always runs with skills/tools available.
    // Still sent as `use_tools` on /chat, which the backend expects.
    let isAutoFillMode = false;  // true after auto_run prefill; requires a timestamp before sending
    let firstRoundSent = false;  // true after the user successfully sends the first question
    // True when the primary Issue Time was auto-filled from the PREVIOUS PAGE
    // and the user hasn't confirmed it yet. While true the primary time is
    // shown in the fields but does NOT count as a usable issue time (the user
    // must tick the confirm checkbox, edit a field, or apply an AI suggestion).
    let issueTimeAwaitingConfirm = false;

    // ── Utilities ──────────────────────────────────────────────────

    // Field id -> [min, max] valid range

    // Which Issue Time fields are REQUIRED. For a loaded log that has no date
    // (e.g. DDD/tracefmt logs), the date is optional — only the time is needed,
    // and Segment2 matches by time-of-day.
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

    // Reflect the date-optional state in the UI when the loaded log has no date.
    //
    // Wi-Fi (log_has_date=true): date row fully editable (required).
    // DDD / tracefmt (log_has_date=false): date row stays visible (same layout
    // as Wi-Fi) but the three Month/Day/Year inputs go DISABLED and turn light
    // grey. Any stale value is cleared. Users can still describe a date in the
    // chat description or AI-suggest popup if they want, but it's never
    // captured into the sidebar — the agent only sees HH:MM:SS.


    // "🗑️ Clear" button: wipe EVERYTHING in one click — the primary picker
    // AND every extra issue-time row.


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


    // ── Auto-filled issue-time confirmation gate ─────────────────────
    // Show/hide the "tick to use this auto-detected time" row and reset
    // its checkbox. Called when a previous-page time is auto-filled (show)
    // and whenever the time becomes user-owned: manual edit, clear, or an
    // applied AI suggestion (hide).
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




    // ── Browse for skills YAML file ────────────────────────────


    // ── Reload skills from shared folder ────────────────────────────

    // Insert a small "Incident N/M at <time>" chip into the chat — used
    // before each multi-time analysis iteration so the user can scan
    // which result belongs to which issue time.



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

    // send=false → "✓ Confirm time(s)": just put the reviewed time(s) into the
    //              sidebar (user reviews, then sends manually later).
    // send=true  → "Apply & send to agent": apply the time(s) AND immediately
    //              send the description already in the chat box for analysis.

    // ---- Sidebar extras list ----



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
    // Render an eit-row's fields into the same string format that
    // getIssueTimeString() emits for the primary picker, so the agent
    // sees a uniform "at around MM/DD/YYYY HH:MM:SS.mmm" suffix.

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

    // Combined accessor: primary picker first, then each filled extra row.
    // Extras only contribute when the master "Use multiple issue times" is on.

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





    // Relabel + repopulate the capture popup's title/summary for the AI
    // flow. Called AFTER openTimeCaptureModal (which sets its own defaults).
