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
        if (await browseInto('sleepstudy-path-input')) await setSleepstudy();
    }

    // ── History hooks (called by features/history.js) ────────────────
    // The conversation a finished analysis or chat turn was saved under.
    function noteNwConversation(result) {
        if (!result || !result.conversation_id) return;
        window.__feedbackConversationId = result.conversation_id;
        firstRoundSent = true;
        if (typeof loadHistoryList === 'function') loadHistoryList();
    }

    function onHistoryNewConversation() {
        nwLoadedPath = '';
        document.getElementById('sleepstudy-path-input').value = '';
        const statusEl = document.getElementById('sleepstudy-status');
        statusEl.textContent = '';
        statusEl.style.display = 'none';
    }

    function onHistoryConversationLoaded(data) {
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
    async function setSleepstudy() {
        const path = document.getElementById('sleepstudy-path-input').value.trim();
        const statusEl = document.getElementById('sleepstudy-status');
        if (!path) { showStatus(statusEl, 'Please enter a sleepstudy file path.', 'err'); return; }
        if (path === nwLoadedPath) return;
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
                showStatus(statusEl, '✘ ' + data.error, 'err');
                return;
            }

            // A brief confirmation, as on the other chatbots; the path stays
            // in the box.
            showStatus(statusEl, '✔ Sleep Study loaded', 'ok', 3000);
            if (data.skills) renderSkills(data.skills);
            window.__feedbackConversationId = '';
            firstRoundSent = false;
            window.__viewingSavedConv = false;
            document.getElementById('chat-window').innerHTML = '';
            if (window.IpsPrompt) { await window.IpsPrompt.handleSetLog(data); }
            await planSleepstudy(path);
        } catch (e) {
            nwLoadedPath = '';
            showStatus(statusEl, '✘ Network error: ' + e.message, 'err');
        }
    }

    async function planSleepstudy(path) {
        appendTyping();
        try {
            const res = await fetch(`${CHATBOT_API}/sleepstudy_plan`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({log_path: path})
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
            el.textContent = '⚠ No driver log found for this time point — choose one, or leave empty to skip.';
            return;
        }
        if (!check) { el.textContent = ''; return; }
        if (check.error) {
            el.style.color = '#b91c1c';
            el.textContent = '✘ ' + check.error;
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
            const span = check.log_span ? ` It covers ${check.log_span.start} → ${check.log_span.end}.` : '';
            el.textContent = '✘ Driver log 沒有涵蓋這段時間 (this log does not cover the window).' + span;
            return;
        }
        el.style.color = check.coverage === 'full' ? '#15803d' : '#b45309';
        el.innerHTML = files.map(f =>
            `⏱ ${escapeHtml(f.start)} → ${escapeHtml(f.end)} <span style="color:#666;">` +
            `(${escapeHtml(f.file)}: start from ${escapeHtml(f.start_from)}, ` +
            `end from ${escapeHtml(f.end_from)})</span>`).join('<br>') +
            (check.coverage === 'full' ? '' : '<br>⚠ Covers only part of the window.');
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
                                      window_start: p.window_start, window_end: p.window_end})
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
    //     whether to check it against a driver log (ticked by default unless
    //     the report already pins the drain on an app's own traffic)
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
            lines.push(`**${sessions.bad_total}** bad session(s) on battery (${bar}): ` +
                       `**${sessions.wireless}** with a wireless RED top blocker, ` +
                       `**${sessions.system}** without one (system).`);
        } else {
            lines.push(`**${plan.qualifying_count}** of ${plan.total_sessions} sessions are battery drains ` +
                       `with ${bar} and a wireless RED top blocker.`);
        }
        if (points.length) {
            lines.push(`The worst **${points.length}** wireless session(s) are listed ` +
                       `(max ${c.max_points}, ${c.per_device_limit} per device). ` +
                       `Tick the ones to check against their driver log.`);
        } else {
            lines.push('No wireless session to check against a driver log.');
        }
        (plan.warnings || []).forEach(w => lines.push(`> ⚠️ ${w}`));

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
                else if (saved.done[p.index]) { statusText = '✔ ' + saved.done[p.index]; statusColor = '#15803d'; }
                else { statusText = '⏸ not analysed yet'; statusColor = '#b45309'; }
            }
            // Filled from History, and as each point finishes (markPointStatus).
            const status = `<br><span class="ss-status" data-index="${p.index}" ` +
                           `style="font-size:0.75rem;color:${statusColor};">${escapeHtml(statusText)}</span>`;
            const verdict = p.html_verdict
                ? `<div style="font-size:0.75rem;margin-top:2px;"><b>Report says:</b> ${escapeHtml(p.html_verdict)}</div>`
                : '';
            const logPath = locked ? (saved.driverLogs[p.index] || '') : (p.driver_log_path || '');
            planPoints[p.index] = p;
            const logCell = `
                <label style="display:flex;align-items:center;gap:6px;font-size:0.75rem;margin-bottom:4px;cursor:pointer;">
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
                            ${locked ? 'disabled' : ''} title="Browse for a WiFiLog file">📁</button>
                </div>
                <div id="ss-drv-info-${p.index}" style="font-size:0.72rem;margin-top:3px;line-height:1.4;overflow-wrap:anywhere;"></div>`;
            return `<tr data-index="${p.index}" style="${picked ? '' : 'opacity:0.6;'}">
                <td style="padding:4px 6px;vertical-align:top;">${p.index}</td>
                <td style="padding:4px 6px;vertical-align:top;">
                    ${escapeHtml(String(p.session_id))} · ${escapeHtml(p.device_label)}<br>
                    <span style="color:#666;font-size:0.75rem;">${when} · SW ${sw}</span>${verdict}${status}</td>
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
                    ${marked.parse(lines.join('\n'))}
                    ${points.length ? `
                    <table style="width:100%;table-layout:fixed;border-collapse:collapse;font-size:0.8rem;">
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
                : 'Write the report from the Sleep Study only';
        };

        if (locked) {
            const selected = points.filter(isSelected).map(p => p.index);
            ssRun = {
                path: path, busy: false, bar: row.querySelector('.ss-progress'), next: 0,
                items: selected.map(i => ({index: i, driverLog: saved.driverLogs[i] || ''})),
                done: new Set(Object.keys(saved.done).map(Number)),
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
                ? {coverage: p.coverage, files: p.driver_log_files || []} : null);
        });
        scrollBottom();

        const missing = [...row.querySelectorAll('.ss-drv-input')]
            .find(inp => !inp.value && row.querySelector(`.ss-check[data-index="${inp.dataset.index}"]`).checked);
        if (missing) missing.focus();

        runBtn.addEventListener('click', async (ev) => {
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
            ssRun = {
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
                noteNwConversation(await res.json());
            } catch (e) {
                console.warn('[sleepstudy] run not saved to History:', e);
            }
            continueSleepstudyRun();
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

    // ── Step 2: analyse the time points one after another ────────────
    // Each point is its own request, its own agent run (with its own skill
    // choices and log cut) and its own History turn. Running them in turn
    // keeps the backend to one agent at a time; Pause (or the Stop button)
    // cancels the running one, and Resume starts that point again.
    let ssRun = null;

    function renderRunBar(state) {
        const run = ssRun;
        if (!run || !run.bar) return;
        const total = run.items.length;
        const at = Math.min(run.next + 1, total);
        const btn = (label, handler) =>
            `<button class="btn-set-log" style="padding:3px 12px;margin-left:8px;width:auto;"
                     onclick="${handler}()">${label}</button>`;
        if (state === 'running') {
            run.bar.innerHTML = `⏳ Analysing time point <b>${at}/${total}</b>…` +
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

    // core.js stopChat(): aborts the stream on screen and asks the server to
    // cancel the running agent (chat_stop knows the time point's agent).
    function pauseSleepstudyRun() {
        if (typeof stopChat === 'function') stopChat();
    }

    async function continueSleepstudyRun() {
        const run = ssRun;
        if (!run || run.busy) return;
        run.busy = true;
        setSendBtnStopMode();
        skipFinished(run);
        while (run.next < run.items.length) {
            renderRunBar('running');
            const item = run.items[run.next];
            markPointStatus(run, item.index, '⏳ analysing…', '#0071c5');
            const outcome = await analyseOnePoint(run, item, run.next + 1, run.items.length);
            if (outcome === 'paused') {
                markPointStatus(run, item.index, '⏸ paused', '#b45309');
                run.busy = false;
                renderRunBar('paused');
                setSendBtnSendMode();
                return;
            }
            run.done.add(item.index);
            skipFinished(run);
        }
        run.busy = false;
        renderRunBar('done');
        setSendBtnSendMode();
        await summarizeSleepstudyRun();
    }

    // The closing analysis: what keeps waking the driver across the points,
    // with counts from the report. Saved as the conversation's last turn.
    async function summarizeSleepstudyRun() {
        const run = ssRun;
        if (!run || run.busy) return;
        run.busy = true;
        appendTyping();
        try {
            const res = await fetch(`${CHATBOT_API}/sleepstudy_summary`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({log_path: run.path})
            });
            const data = await res.json();
            removeTyping();
            if (!data.success) {
                appendAssistantText('❌ ' + (data.error || `Server error ${res.status}`));
                return;
            }
            appendUserMsg(data.user_message);
            appendAssistantText(data.result.data);
            noteNwConversation(data);
        } catch (e) {
            removeTyping();
            appendAssistantText('❌ Network error: ' + e.message);
        } finally {
            run.busy = false;
        }
    }

    // One time point over SSE. Returns 'paused' when the user stopped it.
    async function analyseOnePoint(run, item, position, total) {
        let card = null;
        const ctl = new AbortController();
        window.__streamCtl = ctl;
        try {
            const res = await fetch(`${CHATBOT_API}/analyze_sleepstudy_point`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                signal: ctl.signal,
                body: JSON.stringify({log_path: run.path, index: item.index,
                                      driver_log: item.driverLog, position: position, total: total})
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
                    appendUserMsg(evt.user_message);
                    appendTyping();
                    card = createAgentStepRenderer({
                        title: `Sleep Study ${position}/${total} · Steps`,
                        avatar: '🛌',
                    });
                } else if (evt.type === 'step') {
                    if (card) card.append(evt.step);
                } else if (evt.type === 'done') {
                    removeTyping();
                    if (card) card.collapse();
                    const r = evt.result || {};
                    noteNwConversation(r);
                    markPointStatus(run, item.index, '✔ ' + ((r.point || {}).status || 'done'), '#15803d');
                    const title = `📊 ${position}/${total} · Session ${(r.point || {}).session_id || ''}`;
                    if (r.type === 'report') {
                        appendReport(r.data || {}, title, r.issue_time || null);
                    } else if (r.type === 'partial_report' && r.data) {
                        appendReport(r.data, title + ' (step limit)', r.issue_time || null);
                    } else {
                        appendAssistantText(r.data || '_No response._');
                    }
                } else if (evt.type === 'error') {
                    removeTyping();
                    appendAssistantText('❌ Error: ' + (evt.content || 'Unknown error'));
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
            return 'done';
        } catch (e) {
            if (e.name === 'AbortError') return 'paused';
            removeTyping();
            appendAssistantText('❌ Network error: ' + e.message);
            return 'done';
        }
    }
