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

    // The plan as a card: one row per time point, with the suggested driver
    // log in an editable box. A point with no suggestion is flagged and its
    // box is the first thing focused, so the user picks one or leaves it
    // empty (that point is then reported without calling the LLM).
    function renderPlanCard(path, plan) {
        const c = plan.criteria || {};
        const points = plan.points || [];
        planPoints = {};
        const lines = [
            `**${plan.qualifying_count}** of ${plan.total_sessions} sessions are battery drains with ` +
            `SW DRIPS < ${Math.round(c.drip_threshold_pct || 0)}% and a RED top blocker; ` +
            `the worst **${points.length}** are listed (max ${c.max_points}, ${c.per_device_limit} per device).`,
        ];
        (plan.warnings || []).forEach(w => lines.push(`> ⚠️ ${w}`));
        if (!points.length) {
            lines.push('', 'No session matches the criteria. Nothing needs a driver-log look.');
            appendAssistantText(lines.join('\n'));
            return;
        }

        const rows = points.map(p => {
            const id = `ss-drv-${p.index}`;
            const when = `${escapeHtml(p.window_start)} → ${escapeHtml(p.window_end.split(' ')[1] || p.window_end)}`;
            const sw = p.sw_drip_pct == null ? 'N/A' : p.sw_drip_pct.toFixed(1) + '%';
            let logCell;
            if (!ANALYZABLE.includes(p.device)) {
                logCell = `<span style="color:#888;">${escapeHtml(p.device_label)}: classified only</span>`;
            } else {
                planPoints[p.index] = p;
                logCell = `
                    <div class="log-input-group" style="margin:0;">
                        <input id="${id}" type="text" data-index="${p.index}" class="ss-drv-input"
                               value="${escapeHtml(p.driver_log_path || '')}"
                               placeholder="Driver log for this time point…"
                               onchange="checkPlanLog(${p.index})" style="min-width:0;">
                        <button class="btn-browse" onclick="browsePlanLog(${p.index})" title="Browse for a WiFiLog file">📁</button>
                    </div>
                    <div id="ss-drv-info-${p.index}" style="font-size:0.72rem;margin-top:3px;line-height:1.4;overflow-wrap:anywhere;"></div>`;
            }
            return `<tr>
                <td style="padding:4px 6px;vertical-align:top;">${p.index}</td>
                <td style="padding:4px 6px;vertical-align:top;">
                    ${escapeHtml(String(p.session_id))} · ${escapeHtml(p.device_label)}<br>
                    <span style="color:#666;font-size:0.75rem;">${when} · SW ${sw}</span></td>
                <td style="padding:4px 6px;vertical-align:top;">${logCell}</td>
            </tr>`;
        }).join('');

        const row = document.createElement('div');
        row.className = 'msg-row assistant';
        row.innerHTML = `
            <div class="avatar-icon">🛌</div>
            <div class="msg-body">
                <div class="msg-bubble ss-plan">
                    ${marked.parse(lines.join('\n'))}
                    <table style="width:100%;table-layout:fixed;border-collapse:collapse;font-size:0.8rem;">
                        <colgroup><col style="width:28px;"><col style="width:38%;"><col></colgroup>
                        <thead><tr><th>#</th><th>Session</th><th>Driver log</th></tr></thead>
                        <tbody>${rows}</tbody>
                    </table>
                    <button class="btn-set-log ss-run" style="width:100%;margin-top:8px;">
                        Analyse ${points.length} time point(s), one by one
                    </button>
                    <div class="ss-progress" style="margin-top:8px;font-size:0.82rem;"></div>
                </div>
            </div>`;
        document.getElementById('chat-window').appendChild(row);
        points.forEach(p => {
            if (planPoints[p.index] === p) {
                renderPlanLogInfo(p.index, p.driver_log_path
                    ? {coverage: p.coverage, files: p.driver_log_files || []} : null);
            }
        });
        scrollBottom();

        const missing = row.querySelector('.ss-drv-input[value=""]');
        if (missing) missing.focus();

        row.querySelector('.ss-run').addEventListener('click', (ev) => {
            const driverLogs = {};
            row.querySelectorAll('.ss-drv-input').forEach(inp => {
                driverLogs[inp.dataset.index] = inp.value.trim();
            });
            // The plan is settled once it runs; a new plan comes from loading
            // a report again.
            row.querySelectorAll('input, button').forEach(el => { el.disabled = true; });
            ev.currentTarget.style.display = 'none';
            ssRun = {
                path: path,
                items: points.map(p => ({index: p.index, driverLog: driverLogs[p.index] || ''})),
                next: 0,
                busy: false,
                bar: row.querySelector('.ss-progress'),
            };
            continueSleepstudyRun();
        });
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
            run.bar.innerHTML = `✔ All ${total} time point(s) done.`;
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
        while (run.next < run.items.length) {
            renderRunBar('running');
            const outcome = await analyseOnePoint(run, run.items[run.next],
                                                  run.next + 1, run.items.length);
            if (outcome === 'paused') {
                run.busy = false;
                renderRunBar('paused');
                setSendBtnSendMode();
                return;
            }
            run.next += 1;
        }
        run.busy = false;
        renderRunBar('done');
        setSendBtnSendMode();
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
