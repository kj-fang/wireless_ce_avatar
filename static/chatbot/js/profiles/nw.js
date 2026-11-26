    // NW profile helpers: the Sleep Study flow and its History hooks. The chat
    // runtime (sendMessage / appendReport / …) lives in
    // strategies/nw-chat-runtime.js and the History panel in features/history.js.
    //
    // The flow has two steps:
    //   1. Load the Sleep Study HTML → /sleepstudy_plan lists the time points
    //      and suggests, for each one, the driver log that covers it (no LLM).
    //   2. The user confirms or changes those paths in the plan card, then
    //      /analyze_sleepstudy_point analyses the points one after another,
    //      each in its own log, with a k/N progress line and Pause / Resume.
    let firstRoundSent = false;  // read by history.js: true once this conversation has a turn
    window.__nwSleepstudyRuns = window.__nwSleepstudyRuns || {};
    // The report the page last loaded (or is loading). Enter and leaving the
    // box can both fire for one edit; the second must not load it again.
    let nwLoadedPath = '';

    // ── Browse for a file into an input; true when one was picked ────
    async function browseInto(inputId) {
        const btn = document.querySelector(`#${inputId} + .btn-browse`);
        if (btn) { btn.textContent = '⏳'; btn.disabled = true; }
        try {
            const res = await fetch(`${CHATBOT_API}/browse`);
            const data = await res.json();
            if (data.path) {
                document.getElementById(inputId).value = data.path;
                return true;
            }
        } catch (e) {
            console.error('Browse failed:', e);
        } finally {
            if (btn) { btn.textContent = '📁'; btn.disabled = false; }
        }
        return false;
    }

    // Picking a report loads it, as picking a log does on the other chatbots.
    async function browseSleepstudy() {
        if (await browseInto('sleepstudy-path-input')) await setSleepstudy(true);
    }

    // ── History hooks (called by features/history.js) ────────────────
    // The conversation a finished analysis or chat turn was saved under.
    function noteNwConversation(result) {
        if (!result || !result.conversation_id) return;
        window.__feedbackConversationId = result.conversation_id;
        firstRoundSent = true;
        if (typeof loadHistoryList === 'function') loadHistoryList();
    }

    function shouldKeepActiveStreamOnHistorySwitch() {
        return Object.values(window.__nwSleepstudyRuns || {}).some(run =>
            run && run.busy && run.controller && run.controller === window.__streamCtl);
    }

    function onHistoryNewConversation() {
        nwLoadedPath = '';
        document.getElementById('sleepstudy-path-input').value = '';
        const statusEl = document.getElementById('sleepstudy-status');
        statusEl.textContent = '';
        statusEl.style.display = 'none';
    }

    // NW's unsent work is the Sleep Study report path (and its generated plan),
    // rather than the generic log-path field. The plan itself can be rebuilt
    // from the report when the user resumes this draft.
    function captureHistoryDraft() {
        const input = document.getElementById('sleepstudy-path-input');
        const path = input ? input.value.trim() : '';
        return path ? { sleepstudyPath: path } : {};
    }

    async function onHistoryDraftRestored(draft) {
        const path = draft && draft.sleepstudyPath ? String(draft.sleepstudyPath).trim() : '';
        if (!path) return;
        const input = document.getElementById('sleepstudy-path-input');
        if (!input) return;
        input.value = path;
        // The draft may point at the same file as the conversation just left;
        // force a fresh plan because Resume draft has cleared the chat window.
        nwLoadedPath = '';
        await setSleepstudy();
    }

    function onHistoryConversationLoaded(data) {
        firstRoundSent = true;
        nwLoadedPath = data.log_path || '';
        document.getElementById('sleepstudy-path-input').value = data.log_path || '';
        const statusEl = document.getElementById('sleepstudy-status');
        if (data.log_exists) {
            showStatus(statusEl, '✔ Resumed — report re-loaded.', 'ok', 3000);
        } else if (data.log_path) {
            showStatus(statusEl, '⚠ Original report not found — viewing history only.', 'warn');
        }
    }

    // ── Step 1: load the report and list its time points ─────────────
    // A report loaded (a new one, or the same one again) starts from a clean
    // page: the previous report's plan card, point results, run bar and
    // time points go, and so does its conversation. A run of the previous
    // report that is still analysing carries on in its own History
    // conversation; it no longer draws here (isSleepstudyRunVisible).
    function clearSleepstudyView() {
        ssRun = null;
        planPoints = {};
        window.__feedbackConversationId = '';
        firstRoundSent = false;
        window.__viewingSavedConv = false;
        document.getElementById('chat-window').innerHTML = '';
        if (typeof setSendBtnSendMode === 'function') setSendBtnSendMode();
    }

    // `force` (Enter, or a file picked) loads even the report already loaded,
    // for a clean run of it; the box's change event leaves a report already
    // loaded alone, since it also fires when the box loses focus after Enter.
    async function setSleepstudy(force) {
        const path = document.getElementById('sleepstudy-path-input').value.trim();
        const statusEl = document.getElementById('sleepstudy-status');
        if (!path) { showStatus(statusEl, 'Please enter a sleepstudy file path.', 'err'); return; }
        if (!force && path === nwLoadedPath) return;
        nwLoadedPath = path;

        statusEl.className = 'log-status'; statusEl.textContent = 'Loading…'; statusEl.style.display = 'block';

        try {
            // NW's /set_log loads the Sleep Study report and starts a new
            // conversation (a new History entry) for it.
            const res = await fetch(`${CHATBOT_API}/set_log`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({log_path: path})
            });
            const data = await res.json();
            if (!data.success) {
                nwLoadedPath = '';
                if (data.code === 'E_MULTIPLE_SLEEPSTUDY_REPORTS') {
                    showStatus(statusEl, '⚠ ' + data.error, 'warn');
                    clearSleepstudyView();
                    renderReportChoice(data.candidates || []);
                    return;
                }
                showStatus(statusEl, '✘ ' + data.error, 'err');
                return;
            }

            // A brief confirmation, as on the other chatbots; the path stays
            // in the box. A zip or folder resolves to the report inside it,
            // which every later call takes.
            const report = data.report_path || path;
            showStatus(statusEl, data.extracted_to
                ? `✔ Sleep Study loaded from the zip: ${report.split(/[\\/]/).pop()}`
                : '✔ Sleep Study loaded', 'ok', 3000);
            if (data.skills) renderSkills(data.skills);
            clearSleepstudyView();
            if (typeof loadHistoryList === 'function') loadHistoryList();
            if (window.IpsPrompt) { await window.IpsPrompt.handleSetLog(data); }
            if (data.missing_driver_log) {
                renderDriverLogPrompt(report, data.search_root || '');
                return;
            }
            await planSleepstudy(report);
        } catch (e) {
            nwLoadedPath = '';
            showStatus(statusEl, '✘ Network error: ' + e.message, 'err');
        }
    }

    // A zip or folder holding several Sleep Study reports: the person picks
    // the one to load, which then loads as if typed in the box.
    function renderReportChoice(candidates) {
        const row = document.createElement('div');
        row.className = 'msg-row assistant';
        row.innerHTML = `
            <div class="avatar-icon">🛌</div>
            <div class="msg-body"><div class="msg-bubble">
                <p>⚠ This holds ${candidates.length} Sleep Study reports. Which one should be analysed?</p>
                ${candidates.map((c, i) => `<button class="btn-set-log ss-pick-report" data-i="${i}"
                    style="width:100%;margin-top:6px;text-align:left;white-space:normal;word-break:break-all;">
                    <b>${escapeHtml(c.split(/[\\/]/).pop())}</b><br><small>${escapeHtml(c)}</small></button>`).join('')}
            </div></div>`;
        document.getElementById('chat-window').appendChild(row);
        row.querySelectorAll('.ss-pick-report').forEach(btn => btn.addEventListener('click', () => {
            document.getElementById('sleepstudy-path-input').value = candidates[Number(btn.dataset.i)];
            setSleepstudy(true);
        }));
        scrollBottom();
    }

    // No driver log beside the report (or in its zip): the person either
    // points at one, or goes on with the report alone.
    function renderDriverLogPrompt(report, searched) {
        const row = document.createElement('div');
        row.className = 'msg-row assistant';
        row.innerHTML = `
            <div class="avatar-icon">🛌</div>
            <div class="msg-body"><div class="msg-bubble">
                <p>⚠ <b>目前缺少相關 driver log</b> (no driver log found).
                   No Wi-Fi or Bluetooth driver log (WiFiLog*.LOG, WifiDriver*.etl, *.hci.txt) is next to the
                   report${searched ? ' in <code>' + escapeHtml(searched) + '</code>' : ''}.</p>
                <button class="btn-set-log ss-pick-driver" style="width:100%;margin-top:6px;">📁 選擇 driver log… (choose a driver log)</button>
                <button class="btn-set-log ss-report-only" style="width:100%;margin-top:6px;background:#64748b;">只分析報告 (analyse the report only)</button>
                <div class="ss-driver-prompt-status" style="font-size:0.8rem;margin-top:6px;"></div>
            </div></div>`;
        document.getElementById('chat-window').appendChild(row);
        const status = row.querySelector('.ss-driver-prompt-status');
        const done = () => row.querySelectorAll('button').forEach(b => { b.disabled = true; });
        row.querySelector('.ss-pick-driver').addEventListener('click', async () => {
            try {
                const res = await fetch(`${CHATBOT_API}/browse`);
                const picked = (await res.json()).path;
                if (!picked) { status.textContent = 'No file chosen.'; return; }
                done();
                status.textContent = 'Driver log: ' + picked;
                await planSleepstudy(report, picked);
            } catch (e) {
                status.textContent = '✘ Browse failed: ' + e.message;
            }
        });
        row.querySelector('.ss-report-only').addEventListener('click', async () => {
            done();
            status.textContent = 'Analysing the report only.';
            await planSleepstudy(report);
        });
        scrollBottom();
    }

    async function planSleepstudy(path, driverLog) {
        appendTyping();
        try {
            const res = await fetch(`${CHATBOT_API}/sleepstudy_plan`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(driverLog ? {log_path: path, driver_log: driverLog} : {log_path: path})
            });
            const plan = await res.json();
            removeTyping();
            if (!plan.success) {
                appendAssistantText('❌ ' + (plan.error || `Server error ${res.status}`));
                return;
            }
            renderPlanCard(path, plan);
        } catch (e) {
            removeTyping();
            appendAssistantText('❌ Network error: ' + e.message);
        }
    }

    const ANALYZABLE = ['wifi', 'bt'];
    // The current plan's time points that take a driver log, by index.
    let planPoints = {};

    // Under each driver log box: the time span of each file that overlaps the
    // point's window, and where that span was read from (the file's own name,
    // the next file's name, or a line inside it), so a person can check it.
    function renderPlanLogInfo(index, check) {
        const el = document.getElementById(`ss-drv-info-${index}`);
        if (!el) return;
        const path = (document.getElementById(`ss-drv-${index}`) || {}).value || '';
        if (!path.trim()) {
            el.style.color = '#b45309';
            // The plan's own reason when it has one (e.g. the log under the
            // report's folder stops before this standby began).
            const why = ((planPoints[index] || {}).message) || '';
            el.textContent = why
                ? `⚠ ${why} Or leave it empty to skip.`
                : '⚠ No driver log found for this time point — choose one, or leave it empty to skip.';
            return;
        }
        if (!check) { el.textContent = ''; return; }
        if (check.error) {
            el.style.color = '#b91c1c';
            el.textContent = '✘ ' + check.error;
            return;
        }
        // A Wi-Fi driver ETL with no decoded log yet: it is decoded when the
        // point runs (minutes, so not while the plan is being checked).
        // The box itself shows both clocks of a converted log, so only a note
        // that asks the person to check something stays beneath it: a time
        // zone that was not recorded and lined up only after trying others.
        const clockNote = check.clock_note && !/^Decoded log is in /.test(check.clock_note)
            ? `<div class="ss-coverage-warning">🕐 ${escapeHtml(check.clock_note)}</div>` : '';
        if (check.coverage === 'etl') {
            const etls = check.etl_candidates || [];
            const first = etls[0] || {file: path.split(/[\\/]/).pop(), captured: ''};
            el.style.color = '#0369a1';
            el.innerHTML = `<div class="ss-coverage-entry"><span>🗜 Wi-Fi driver ETL, decoded when this point is analysed</span>` +
                `<small>${escapeHtml(first.file)}${first.captured ? ' · captured ' + escapeHtml(first.captured) : ''}` +
                `${etls.length > 1 ? ` · ${etls.length - 1} more to try if it does not cover the window` : ''}</small></div>` +
                (check.gap_note ? `<div class="ss-coverage-warning">${escapeHtml(check.gap_note)}</div>` : '');
            return;
        }
        const files = check.files || [];
        if (!files.length) {
            el.style.color = '#b91c1c';
            if (!check.log_span && (check.warnings || []).length) {
                // Not a log this tool can place in time at all.
                el.textContent = '✘ ' + check.warnings.join(' ');
                return;
            }
            if (check.gap_note) {
                // It reaches only the margin around the session, not the standby.
                el.textContent = '✘ ' + check.gap_note;
                return;
            }
            const span = check.log_span ? ` It covers ${check.log_span.start} → ${check.log_span.end}.` : '';
            el.textContent = '✘ Driver log 沒有涵蓋這段時間 (this log does not cover the window).' + span;
            return;
        }
        el.style.color = check.coverage === 'full' ? '#15803d' : '#b45309';
        // Every time shown is on the customer's clock (the report's). A log
        // stamped in another clock (decoded elsewhere) shows the converted
        // span first and its own times beneath, so both can be checked.
        const customer = ((planPoints[index] || {}).utc_offset_label) || '';
        const tag = (text, cls) => text ? `<i class="ss-tz-tag ${cls || ''}">${escapeHtml(text)}</i>` : '';
        // "MM/DD/YYYY HH:MM:SS → HH:MM:SS" when both ends fall on one day.
        // The date and the clock time are each kept whole: a narrow box
        // breaks the line between them or at the arrow, never inside one.
        const whole = s => String(s).split(' ').filter(Boolean)
            .map(part => `<span class="ss-tz-span">${escapeHtml(part)}</span>`).join(' ');
        const span = (a, b) => {
            a = String(a || ''); b = String(b || '');
            const sameDay = a.slice(0, 10) && a.slice(0, 10) === b.slice(0, 10);
            return `${whole(a)} → ${whole(sameDay ? b.slice(11) : b)}`;
        };
        el.innerHTML = files.map(f => {
            const fileName = String(f.file || '').split(/[\\/]/).pop();
            if (f.shifted) {
                // Both clocks, one per row, the zone first so the rows line up:
                // the customer's (converted, what the report says) above the
                // log's own, with the shift between them.
                return `<div class="ss-coverage-entry shifted"><div class="ss-tz-grid">` +
                    tag(`${customer || 'customer'} · customer`, 'customer') +
                    `<span class="ss-tz-time now">${span(f.start, f.end)}</span>` +
                    // "≈" marks a log clock that was assumed, not recorded.
                    (f.log_clock_guessed
                        ? `<i class="ss-tz-tag log" title="Time zone assumed (not recorded with the log)">≈ ${escapeHtml(f.log_clock)} · log</i>`
                        : tag(`${f.log_clock} · log`, 'log')) +
                    `<span class="ss-tz-time was">${span(f.log_start, f.log_end)}` +
                    `<b class="ss-tz-shift">${escapeHtml(f.shift)}</b></span>` +
                    `</div><small>${escapeHtml(fileName)}</small></div>`;
            }
            return `<div class="ss-coverage-entry"><span>⏱ ${span(f.start, f.end)}` +
                   `${tag(customer ? `${customer} · customer` : '', 'muted')}</span>` +
                   `<small>${escapeHtml(fileName)}</small></div>`;
        }).join('') + (check.coverage === 'full'
            ? '' : '<div class="ss-coverage-warning">⚠ Driver log covers only part of this window.</div>') + clockNote;
    }

    async function checkPlanLog(index) {
        const p = planPoints[index];
        const path = document.getElementById(`ss-drv-${index}`).value.trim();
        if (!p || !path) { renderPlanLogInfo(index, null); return; }
        try {
            const res = await fetch(`${CHATBOT_API}/sleepstudy_driver_check`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path: path, device: p.device,
                                      window_start: p.window_start, window_end: p.window_end,
                                      // The session's own ends: a capped window
                                      // no longer ends 2 min after the session.
                                      session_start: p.start, session_end: p.end,
                                      utc_offset_minutes: p.utc_offset_minutes})
            });
            renderPlanLogInfo(index, await res.json());
        } catch (e) {
            renderPlanLogInfo(index, {error: 'Network error: ' + e.message});
        }
    }

    async function browsePlanLog(index) {
        if (await browseInto(`ss-drv-${index}`)) await checkPlanLog(index);
    }

    // The HTML conclusion card. What the report alone says, before any driver
    // log is read:
    //   - how many bad sessions there are, and how many are not wireless at
    //     all ("system": no driver log of ours can explain them)
    //   - per wireless time point, the scenario the report points to, and
    //     whether to check it against a driver log. Every wireless point is
    //     ticked by default (analyse_default): the report's verdict is a
    //     hypothesis for the driver agent, not a reason to skip the log.
    //   - the suggested driver log per point, editable; a point with none is
    //     flagged and focused
    // Analyse checks the ticked points one by one, then writes the report;
    // with nothing ticked it writes the report from the Sleep Study alone.
    //
    // `saved` redraws a run reopened from History: {driverLogs, done,
    // selected}, where done maps a point index to the status its saved turn
    // recorded. The card is then locked, shows each point's status, and
    // offers Resume when some ticked points never finished.
    function renderPlanCard(path, plan, saved) {
        const locked = !!saved;
        const c = plan.criteria || {};
        const points = plan.points || [];
        const sessions = plan.sessions || null;
        planPoints = {};
        const bar = `${c.drip_metric === 'sw' ? 'SW DRIPS' : 'SW or HW DRIPS'} < ${Math.round(c.drip_threshold_pct || 0)}%`;
        const lines = [];
        if (sessions && sessions.bad_total) {
            lines.push(`**${sessions.bad_total}** bad session(s) on battery &nbsp;·&nbsp; ${bar}`);
            lines.push(`- **${sessions.wireless}** with a wireless RED top blocker\n` +
                       `- **${sessions.system}** without one (system)`);
        } else {
            lines.push(`**${plan.qualifying_count}** of ${plan.total_sessions} sessions are battery drains ` +
                       `with ${bar} and a wireless RED top blocker.`);
        }
        // Every bad session is a platform problem: say so first, and do not
        // ask for driver logs nobody needs.
        const allSystem = !points.length && sessions && sessions.system > 0;
        if (points.length) {
            lines.push(`The worst **${points.length}** wireless session(s) are listed below ` +
                       `— max ${c.max_points}, ${c.per_device_limit} per device.`);
            lines.push(`Tick the ones to check against their driver log.`);
        } else if (allSystem) {
            lines.unshift(`✅ **No driver log check needed.** None of the ${sessions.bad_total} bad ` +
                          `session(s) has a wireless RED top blocker: this is a platform (system) issue, ` +
                          `not a wireless driver one.`);
        } else {
            lines.push('No wireless session to check against a driver log.');
        }
        // The driver log search only matters when there is something to check.
        // Escaped: a warning names file patterns (WiFiLog--*.LOG, WifiDriver*.etl)
        // whose asterisks markdown would otherwise turn into italics.
        const mdEscape = s => String(s).replace(/([\\`*_[\]])/g, '\\$1');
        if (points.length) (plan.warnings || []).forEach(w => lines.push(`> ⚠️ ${mdEscape(w)}`));

        const isSelected = (p) => locked
            ? (saved.selected ? saved.selected.includes(p.index) : true)
            : p.analyse_default !== false;

        const rows = points.map(p => {
            const id = `ss-drv-${p.index}`;
            const when = `${escapeHtml(p.window_start)} → ${escapeHtml(p.window_end.split(' ')[1] || p.window_end)}`;
            const sw = p.sw_drip_pct == null ? 'N/A' : p.sw_drip_pct.toFixed(1) + '%';
            const picked = isSelected(p);
            let statusText = '', statusColor = '';
            if (locked) {
                if (!picked) { statusText = 'report only'; statusColor = '#888'; }
                else if (saved.done[p.index] === 'paused') {
                    statusText = '⏸ paused · resume available'; statusColor = '#b45309';
                }
                else if (saved.done[p.index]) { statusText = '✔ ' + saved.done[p.index]; statusColor = '#15803d'; }
                else { statusText = '⏸ not analysed yet'; statusColor = '#b45309'; }
            }
            // Filled from History, and as each point finishes (markPointStatus).
            const status = `<br><span class="ss-status" data-index="${p.index}" ` +
                           `style="font-size:0.75rem;color:${statusColor};">${escapeHtml(statusText)}</span>`;
            const verdict = p.html_verdict
                ? `<div class="ss-plan-verdict"><span>Sleep Study</span><b>${escapeHtml(p.html_verdict)}</b></div>`
                : '';
            const logPath = locked ? (saved.driverLogs[p.index] || '') : (p.driver_log_path || '');
            planPoints[p.index] = p;
            const logCell = `
                <label class="ss-driver-check">
                    <input type="checkbox" class="ss-check" data-index="${p.index}"
                           ${picked ? 'checked' : ''} ${locked ? 'disabled' : ''}>
                    Check against the driver log
                </label>
                <div class="log-input-group" style="margin:0;">
                    <input id="${id}" type="text" data-index="${p.index}" class="ss-drv-input"
                           value="${escapeHtml(logPath)}" ${locked ? 'disabled' : ''}
                           placeholder="Driver log for this time point…"
                           onchange="checkPlanLog(${p.index})" style="min-width:0;">
                    <button class="btn-browse" onclick="browsePlanLog(${p.index})"
                            ${locked ? 'disabled' : ''} title="Browse for a WiFiLog, a Wi-Fi driver ETL (WifiDriver*.etl) or its decoded log, or a decoded *.hci.txt">📁</button>
                </div>
                <div id="ss-drv-info-${p.index}" style="font-size:0.72rem;margin-top:3px;line-height:1.4;overflow-wrap:anywhere;"></div>`;
            return `<tr class="ss-plan-row" data-index="${p.index}" style="${picked ? '' : 'opacity:0.58;'}">
                <td style="padding:4px 6px;vertical-align:top;">${p.index}</td>
                <td style="padding:4px 6px;vertical-align:top;">
                    <div class="ss-plan-session-title"><b>Session ${escapeHtml(String(p.session_id))}</b><span>${escapeHtml(p.device_label)}</span></div>
                    <div class="ss-plan-session-meta"><span>${when}</span>${p.utc_offset_label
                        ? `<i class="ss-tz-tag" title="Times are the customer machine's local clock, as the Sleep Study report records them">${escapeHtml(p.utc_offset_label)} · customer</i>` : ''}<b>SW ${sw}</b></div>${verdict}${status}</td>
                <td style="padding:4px 6px;vertical-align:top;">${logCell}</td>
            </tr>`;
        }).join('');

        const groups = (sessions && sessions.groups) || [];
        const systemBlock = groups.length ? `
            <div style="margin-top:10px;font-size:0.8rem;">
                <b>System sessions</b> — no wireless RED blocker, so not a wireless driver issue;
                they go into the report for the platform team, without a driver log:
                <ul style="margin:4px 0 0 18px;padding:0;">
                    ${groups.map(g => `<li>${escapeHtml(g.blocker)}: ${g.sessions.length} session(s)` +
                        (g.worst_sw != null ? `, worst SW DRIPS ${g.worst_sw}%` : '') + '</li>').join('')}
                </ul>
            </div>` : '';

        const row = document.createElement('div');
        row.className = 'msg-row assistant';
        row.innerHTML = `
            <div class="avatar-icon">🛌</div>
            <div class="msg-body">
                <div class="msg-bubble ss-plan">
                    <div class="ss-plan-intro">${marked.parse(lines.join('\n\n'))}</div>
                    ${points.length ? `
                    <table class="ss-plan-table" style="width:100%;table-layout:fixed;border-collapse:collapse;font-size:0.8rem;">
                        <colgroup><col style="width:28px;"><col style="width:40%;"><col></colgroup>
                        <thead><tr><th>#</th><th>Session · report</th><th>Driver log</th></tr></thead>
                        <tbody>${rows}</tbody>
                    </table>` : ''}
                    ${systemBlock}
                    <button class="btn-set-log ss-run" style="width:100%;margin-top:8px;${locked ? 'display:none;' : ''}"></button>
                    <div class="ss-progress" style="margin-top:8px;font-size:0.82rem;"></div>
                </div>
            </div>`;
        document.getElementById('chat-window').appendChild(row);

        const checkedIndexes = () => [...row.querySelectorAll('.ss-check')]
            .filter(cb => cb.checked).map(cb => Number(cb.dataset.index));
        const runBtn = row.querySelector('.ss-run');
        const labelRun = () => {
            const k = checkedIndexes().length;
            runBtn.textContent = k
                ? `Analyse ${k} ticked time point(s), then write the report`
                : allSystem ? 'Write the platform (system) report'
                : 'Write the report from the Sleep Study only';
        };

        if (locked) {
            const selected = points.filter(isSelected).map(p => p.index);
            const activeRun = window.__nwSleepstudyRuns[window.__feedbackConversationId || ''];
            if (activeRun && activeRun.busy) {
                activeRun.bar = row.querySelector('.ss-progress');
                ssRun = activeRun;
                const activeIndex = activeRun.items[activeRun.next]?.index;
                if (activeIndex != null) {
                    markPointStatus(activeRun, activeIndex, '⏳ analysing in background…', '#0071c5');
                }
                renderRunBar('running');
                return;
            }
            ssRun = {
                path: path, busy: false, bar: row.querySelector('.ss-progress'), next: 0,
                items: selected.map(i => ({index: i, driverLog: saved.driverLogs[i] || ''})),
                done: new Set(Object.keys(saved.done)
                    .filter(i => saved.done[i] !== 'paused').map(Number)),
                conversationId: window.__feedbackConversationId || '',
            };
            skipFinished(ssRun);
            renderRunBar(ssRun.next < ssRun.items.length ? 'paused' : 'done');
            return;
        }

        labelRun();
        row.querySelectorAll('.ss-check').forEach(cb => cb.addEventListener('change', () => {
            cb.closest('tr').style.opacity = cb.checked ? '' : '0.6';
            labelRun();
        }));
        points.forEach(p => {
            renderPlanLogInfo(p.index, p.driver_log_path
                ? {coverage: p.coverage, files: p.driver_log_files || [],
                   etl_candidates: p.etl_candidates || [],
                   clock_note: p.log_clock_note || ''} : null);
        });
        scrollBottom();

        const missing = [...row.querySelectorAll('.ss-drv-input')]
            .find(inp => !inp.value && row.querySelector(`.ss-check[data-index="${inp.dataset.index}"]`).checked);
        if (missing) missing.focus();

        runBtn.addEventListener('click', async (ev) => {
            if (shouldKeepActiveStreamOnHistorySwitch()) {
                const statusEl = document.getElementById('sleepstudy-status');
                showStatus(statusEl,
                    'A Sleep Study run is already continuing in another History session. Wait for it to finish before starting another run.',
                    'warn');
                return;
            }
            const startViewId = window.__feedbackConversationId || '';
            const driverLogs = {};
            row.querySelectorAll('.ss-drv-input').forEach(inp => {
                driverLogs[inp.dataset.index] = inp.value.trim();
            });
            const selected = checkedIndexes();
            // The plan is settled once it runs; a new plan comes from loading
            // a report again.
            row.querySelectorAll('input, button').forEach(el => { el.disabled = true; });
            ev.currentTarget.style.display = 'none';
            points.forEach(p => {
                if (!selected.includes(p.index)) markPointStatus({bar: row.querySelector('.ss-progress')},
                                                                p.index, 'report only', '#888');
            });
            const run = ssRun = {
                path: path,
                items: selected.map(i => ({index: i, driverLog: driverLogs[i] || ''})),
                next: 0,
                busy: false,
                done: new Set(),
                bar: row.querySelector('.ss-progress'),
            };
            // The plan is the conversation's first History turn, so reopening
            // it later brings this card (and Resume) back.
            try {
                const res = await fetch(`${CHATBOT_API}/sleepstudy_run_start`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({log_path: path, plan: plan, driver_logs: driverLogs,
                                          selected: selected})
                });
                const savedRun = await res.json();
                if (!savedRun.success || !savedRun.conversation_id) {
                    throw new Error(savedRun.error || 'The run could not be saved to History.');
                }
                // The user may have switched History while the run-start
                // request was in flight. Keep its ID, but never steal the
                // visible conversation selection back.
                if (!window.__viewingSavedConv
                    && (window.__feedbackConversationId || '') === startViewId) {
                    noteNwConversation(savedRun);
                } else if (typeof loadHistoryList === 'function') {
                    loadHistoryList();
                }
                run.conversationId = savedRun.conversation_id;
                if (run.conversationId) window.__nwSleepstudyRuns[run.conversationId] = run;
            } catch (e) {
                console.warn('[sleepstudy] run not saved to History:', e);
                row.querySelectorAll('input, button').forEach(el => { el.disabled = false; });
                // Not ev.currentTarget: the click dispatch ended at the await above.
                runBtn.style.display = '';
                labelRun();
                showStatus(document.getElementById('sleepstudy-status'),
                           '✘ ' + e.message, 'err');
                return;
            }
            continueSleepstudyRun(run);
        });
    }

    // History hook (features/history.js renderHistoryTurns): the plan turn
    // redraws the card, marking which points the later turns finished.
    function renderCustomHistoryResult(turn, turns) {
        const r = (turn && turn.result) || {};
        if (r.type !== 'sleepstudy_plan') return false;
        const plan = r.data || {};
        const done = {};
        (turns || []).forEach(t => {
            const pt = (t.result || {}).point;
            if (pt && pt.index != null) done[pt.index] = pt.status || 'done';
        });
        renderPlanCard(plan.log_path || '', plan, {
            driverLogs: plan.driver_logs || {}, done: done,
            // Runs saved before points could be left unticked checked them all.
            selected: Array.isArray(plan.selected) ? plan.selected : null,
        });
        return true;
    }

    function markPointStatus(run, index, text, color) {
        const card = run.bar && run.bar.closest('.ss-plan');
        const el = card && card.querySelector(`.ss-status[data-index="${index}"]`);
        if (!el) return;
        el.textContent = text;
        el.style.color = color;
    }

    // Move a run's cursor past the points that already have a saved turn.
    function skipFinished(run) {
        while (run.next < run.items.length && run.done.has(run.items[run.next].index)) run.next += 1;
    }

    function isSleepstudyRunVisible(run) {
        return !!(run && run.conversationId
            && window.__feedbackConversationId === run.conversationId);
    }

    // ── Step 2: analyse the time points one after another ────────────
    // Each point is its own request, its own agent run (with its own skill
    // choices and log cut) and its own History turn. Running them in turn
    // keeps the backend to one agent at a time; Pause (or the Stop button)
    // cancels the running one, and Resume starts that point again.
    let ssRun = null;

    function renderRunBar(state, runOverride) {
        const run = runOverride || ssRun;
        if (!run || !run.bar) return;
        const total = run.items.length;
        const at = Math.min(run.next + 1, total);
        const btn = (label, handler) =>
            `<button class="btn-set-log" style="padding:3px 12px;margin-left:8px;width:auto;"
                     onclick="${handler}()">${label}</button>`;
        if (state === 'running') {
            // Pause does not need run.controller: it is only set once
            // analyseOnePoint() is inside its fetch, and cleared between
            // points, so testing it here would hide the button all run.
            run.bar.innerHTML = run.summarizing
                ? '📊 Writing the combined Sleep Study report…'
                : `⏳ Analysing time point <b>${at}/${total}</b>…` +
                  btn('⏸ Pause', 'pauseSleepstudyRun');
        } else if (state === 'paused') {
            run.bar.innerHTML = `⏸ Paused at time point <b>${at}/${total}</b>.` +
                                btn('▶ Resume', 'continueSleepstudyRun');
        } else {
            run.bar.innerHTML = (total ? `✔ All ${total} ticked time point(s) done.`
                                       : '✔ Nothing to check against a driver log.') +
                                btn('📊 Write the report again', 'summarizeSleepstudyRun');
        }
    }

    // Pause has to reach the server as well. Aborting the browser's reader
    // only stops the rendering: the agent runs on a worker thread and would
    // keep spending until it next tried to yield. /chat/stop sets that
    // agent's cancel_event, and Resume waits on `run.pausing` so a second
    // worker cannot start on top of the first.
    function pauseSleepstudyRun() {
        const run = ssRun;
        if (!run) {
            if (typeof stopChat === 'function') stopChat();
            return;
        }
        if (run.bar) run.bar.innerHTML = '⏸ Stopping the running agent…';
        run.pausing = fetch(`${CHATBOT_API}/chat/stop`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({conversation_id: run.conversationId || ''}),
        }).catch(e => console.warn('[sleepstudy] stop request failed:', e));
        if (run.controller) run.controller.abort();
    }

    async function continueSleepstudyRun(runOverride) {
        const run = runOverride || ssRun;
        if (!run || run.busy) return;
        if (run.conversationId) window.__nwSleepstudyRuns[run.conversationId] = run;
        run.busy = true;
        if (isSleepstudyRunVisible(run)) setSendBtnStopMode();
        skipFinished(run);
        while (run.next < run.items.length) {
            renderRunBar('running', run);
            const item = run.items[run.next];
            markPointStatus(run, item.index, '⏳ analysing…', '#0071c5');
            const outcome = await analyseOnePoint(run, item, run.next + 1, run.items.length);
            if (outcome === 'paused') {
                markPointStatus(run, item.index, '⏸ paused', '#b45309');
                // Offer Resume only once the server has acknowledged the cancel.
                if (run.pausing) { try { await run.pausing; } catch (e) {} run.pausing = null; }
                run.busy = false;
                if (run.conversationId) delete window.__nwSleepstudyRuns[run.conversationId];
                renderRunBar('paused', run);
                if (isSleepstudyRunVisible(run)) setSendBtnSendMode();
                if (typeof loadHistoryList === 'function') loadHistoryList();
                return;
            }
            run.done.add(item.index);
            skipFinished(run);
        }
        run.busy = false;
        renderRunBar('done', run);
        if (isSleepstudyRunVisible(run)) setSendBtnSendMode();
        await summarizeSleepstudyRun(run);
    }

    // The closing analysis: what keeps waking the driver across the points,
    // with counts from the report. Saved as the conversation's last turn.
    async function summarizeSleepstudyRun(runOverride) {
        const run = runOverride || ssRun;
        if (!run || run.busy) return;
        run.busy = true;
        run.summarizing = true;
        renderRunBar('running', run);
        const isVisible = () => isSleepstudyRunVisible(run);
        if (isVisible()) appendTyping();
        try {
            const res = await fetch(`${CHATBOT_API}/sleepstudy_summary`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({log_path: run.path, conversation_id: run.conversationId})
            });
            const data = await res.json();
            if (isVisible()) removeTyping();
            if (!data.success) {
                if (isVisible()) appendAssistantText('❌ ' + (data.error || `Server error ${res.status}`));
                return;
            }
            if (isVisible()) {
                appendUserMsg(data.user_message);
                appendAssistantText(data.result.data);
                noteNwConversation(data);
            }
            // Re-open from the durable turns so a switched-away run gets its
            // complete saved step traces and the follow-up context is rebuilt
            // from this exact conversation, not another one in the UI.
            run.needsHistoryRefresh = true;
        } catch (e) {
            if (isVisible()) {
                removeTyping();
                appendAssistantText('❌ Network error: ' + e.message);
            }
        } finally {
            run.busy = false;
            run.summarizing = false;
            if (run.conversationId) delete window.__nwSleepstudyRuns[run.conversationId];
            if (typeof loadHistoryList === 'function') loadHistoryList();
            if (run.needsHistoryRefresh && isVisible() && typeof openHistoryConversation === 'function') {
                await openHistoryConversation(run.conversationId);
            }
        }
    }

    // One time point over SSE. Returns 'done' only when the stream carried a
    // terminal "done" event; anything else ('paused') leaves the point
    // unfinished so the run bar offers Resume instead of writing the report.
    async function analyseOnePoint(run, item, position, total) {
        let card = null;
        let finished = false;
        const isVisible = () => isSleepstudyRunVisible(run);
        const ctl = new AbortController();
        run.controller = ctl;
        window.__streamCtl = ctl;
        try {
            const res = await fetch(`${CHATBOT_API}/analyze_sleepstudy_point`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                signal: ctl.signal,
                body: JSON.stringify({log_path: run.path, index: item.index,
                                      driver_log: item.driverLog, position: position, total: total,
                                      conversation_id: run.conversationId})
            });
            if (!res.ok) throw new Error(`Server error ${res.status}`);

            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            const processLine = (line) => {
                if (!line.startsWith('data:')) return;
                const jsonStr = line.slice(5).trim();
                if (!jsonStr) return;
                let evt;
                try { evt = JSON.parse(jsonStr); } catch { return; }

                if (evt.type === 'turn') {
                    if (isVisible()) {
                        appendUserMsg(evt.user_message);
                        appendTyping();
                        card = createAgentStepRenderer({
                            title: `Sleep Study ${position}/${total} · Steps`,
                            avatar: '🛌',
                        });
                    } else run.needsHistoryRefresh = true;
                } else if (evt.type === 'step') {
                    if (card) card.append(evt.step);
                } else if (evt.type === 'done') {
                    finished = true;
                    if (isVisible()) removeTyping();
                    if (card) card.collapse();
                    const r = evt.result || {};
                    if (isVisible()) noteNwConversation(r);
                    else run.needsHistoryRefresh = true;
                    markPointStatus(run, item.index, '✔ ' + ((r.point || {}).status || 'done'), '#15803d');
                    const title = `📊 ${position}/${total} · Session ${(r.point || {}).session_id || ''}`;
                    if (!isVisible()) return;
                    if (r.type === 'report') {
                        appendReport(r.data || {}, title, r.issue_time || null);
                    } else if (r.type === 'partial_report' && r.data) {
                        appendReport(r.data, title + ' (step limit)', r.issue_time || null);
                    } else {
                        appendAssistantText(r.data || '_No response._');
                    }
                } else if (evt.type === 'error') {
                    if (isVisible()) {
                        removeTyping();
                        appendAssistantText('❌ Error: ' + (evt.content || 'Unknown error'));
                    }
                }
            };

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop();
                lines.forEach(processLine);
            }
            if (buffer) processLine(buffer);
            if (!finished) {
                markPointStatus(run, item.index, '⚠ stopped before finishing', '#b45309');
                if (isVisible()) removeTyping();
                if (card) card.collapse();
            }
            return finished ? 'done' : 'paused';
        } catch (e) {
            if (e.name === 'AbortError') {
                if (isVisible()) removeTyping();
                if (card) card.collapse();
                return 'paused';
            }
            if (isVisible()) {
                removeTyping();
                appendAssistantText('❌ Network error: ' + e.message);
            } else run.needsHistoryRefresh = true;
            if (card) card.collapse();
            return finished ? 'done' : 'paused';
        } finally {
            if (run.controller === ctl) run.controller = null;
            if (window.__streamCtl === ctl) window.__streamCtl = null;
        }
    }
