    // NW profile helpers: the Sleep Study flow and its History hooks. The chat
    // runtime (sendMessage / appendReport / …) lives in
    // strategies/nw-chat-runtime.js and the History panel in features/history.js.
    //
    // The flow has two steps:
    //   1. Load the Sleep Study HTML → /sleepstudy_plan lists the time points
    //      and suggests, for each one, the driver log that covers it (no LLM).
    //   2. The user confirms or changes those paths in the plan card, then
    //      /analyze_sleepstudy_driver analyses each point in its own log.
    let firstRoundSent = false;  // read by history.js: true once this conversation has a turn

    // ── Browse for a file into an input ─────────────────────────────
    async function browseInto(inputId) {
        const btn = document.querySelector(`#${inputId} + .btn-browse`);
        if (btn) { btn.textContent = '⏳'; btn.disabled = true; }
        try {
            const res = await fetch(`${CHATBOT_API}/browse`);
            const data = await res.json();
            if (data.path) {
                document.getElementById(inputId).value = data.path;
            }
        } catch (e) {
            console.error('Browse failed:', e);
        } finally {
            if (btn) { btn.textContent = '📁'; btn.disabled = false; }
        }
    }

    function browseSleepstudy() { return browseInto('sleepstudy-path-input'); }

    // ── History hooks (called by features/history.js) ────────────────
    // The conversation a finished analysis or chat turn was saved under.
    function noteNwConversation(result) {
        if (!result || !result.conversation_id) return;
        window.__feedbackConversationId = result.conversation_id;
        firstRoundSent = true;
        if (typeof loadHistoryList === 'function') loadHistoryList();
    }

    function onHistoryNewConversation() {
        document.getElementById('sleepstudy-path-input').value = '';
        const statusEl = document.getElementById('sleepstudy-status');
        statusEl.textContent = '';
        statusEl.style.display = 'none';
    }

    function onHistoryConversationLoaded(data) {
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
            if (!data.success) { showStatus(statusEl, '✘ ' + data.error, 'err'); return; }

            showStatus(statusEl, '✔ ' + (data.message || 'Sleepstudy file loaded.'), 'ok');
            if (data.skills) renderSkills(data.skills);
            window.__feedbackConversationId = '';
            firstRoundSent = false;
            window.__viewingSavedConv = false;
            document.getElementById('chat-window').innerHTML = '';
            if (window.IpsPrompt) { await window.IpsPrompt.handleSetLog(data); }
            await planSleepstudy(path);
        } catch (e) {
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

    // The plan as a card: one row per time point, with the suggested driver
    // log in an editable box. A point with no suggestion is flagged and its
    // box is the first thing focused, so the user picks one or leaves it
    // empty (that point is then reported without calling the LLM).
    function renderPlanCard(path, plan) {
        const c = plan.criteria || {};
        const points = plan.points || [];
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
                const found = !!p.driver_log_path;
                logCell = `
                    <div class="log-input-group" style="margin:0;">
                        <input id="${id}" type="text" data-index="${p.index}" class="ss-drv-input"
                               value="${escapeHtml(p.driver_log_path || '')}"
                               placeholder="Driver log for this time point…"
                               style="${found ? '' : 'border-color:#f59e0b;background:#fffbeb;'}">
                        <button class="btn-browse" onclick="browseInto('${id}')" title="Browse for a WiFiLog file">📁</button>
                    </div>
                    <div style="font-size:0.72rem;margin-top:2px;color:${found ? '#15803d' : '#b45309'};">
                        ${found ? '✔ found under the report folder' : '⚠ not found — choose one, or leave empty to skip'}
                    </div>`;
            }
            return `<tr>
                <td style="padding:4px 6px;vertical-align:top;">${p.index}</td>
                <td style="padding:4px 6px;vertical-align:top;white-space:nowrap;">
                    ${escapeHtml(String(p.session_id))} · ${escapeHtml(p.device_label)}<br>
                    <span style="color:#666;font-size:0.75rem;">${when} · SW ${sw}</span></td>
                <td style="padding:4px 6px;vertical-align:top;min-width:260px;">${logCell}</td>
            </tr>`;
        }).join('');

        const row = document.createElement('div');
        row.className = 'msg-row assistant';
        row.innerHTML = `
            <div class="avatar-icon">🛌</div>
            <div class="msg-body">
                <div class="msg-bubble ss-plan">
                    ${marked.parse(lines.join('\n'))}
                    <table style="width:100%;border-collapse:collapse;font-size:0.8rem;">
                        <thead><tr><th>#</th><th>Session</th><th>Driver log</th></tr></thead>
                        <tbody>${rows}</tbody>
                    </table>
                    <button class="btn-set-log ss-run" style="width:100%;margin-top:8px;">
                        Analyse ${points.length} time point(s)
                    </button>
                </div>
            </div>`;
        document.getElementById('chat-window').appendChild(row);
        scrollBottom();

        const missing = row.querySelector('.ss-drv-input[value=""]');
        if (missing) missing.focus();

        row.querySelector('.ss-run').addEventListener('click', async (ev) => {
            const driverLogs = {};
            row.querySelectorAll('.ss-drv-input').forEach(inp => {
                driverLogs[inp.dataset.index] = inp.value.trim();
            });
            // The plan is settled once it runs; a new plan comes from Load.
            row.querySelectorAll('input, button').forEach(el => { el.disabled = true; });
            ev.currentTarget.textContent = 'Analysing…';
            await runSleepstudyAnalysis(path, driverLogs);
        });
    }

    // The user turn an analysis is saved as; the server's
    // _sleepstudy_request_text writes the same text to History.
    function sleepstudyRequestText(path, driverLogs) {
        const base = (p) => p.replace(/[\\/]+$/, '').split(/[\\/]/).pop();
        const entries = Object.entries(driverLogs || {}).sort((a, b) => Number(a[0]) - Number(b[0]));
        const lines = ['Sleep Study: ' + base(path) +
                           (entries.length ? ` — ${entries.length} time point(s)` : ''),
                       'Report: `' + path + '`'];
        entries.forEach(([index, log]) => {
            lines.push(log ? `#${index} → \`${log}\`` : `#${index} → (no driver log)`);
        });
        return lines.join('\n');
    }

    // ── Step 2: analyse each time point in its driver log (SSE) ──────
    async function runSleepstudyAnalysis(path, driverLogs) {
        appendUserMsg(sleepstudyRequestText(path, driverLogs));
        appendTyping();

        const card = createAgentStepRenderer({
            title: 'Sleepstudy × Driver Log Steps',
            avatar: '🛌',
        });

        try {
            const res = await fetch(`${CHATBOT_API}/analyze_sleepstudy_driver`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({log_path: path, driver_logs: driverLogs})
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

                if (evt.type === 'step') {
                    card.append(evt.step);
                } else if (evt.type === 'done') {
                    removeTyping();
                    card.collapse();
                    const result = evt.result;
                    noteNwConversation(result);
                    if (result && result.type === 'text') {
                        appendAssistantText(result.data || '_No response._');
                    } else {
                        appendAssistantText('⚠️ ' + (result && result.data ? result.data : 'Unknown response.'));
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

        } catch (e) {
            removeTyping();
            appendAssistantText('❌ Network error: ' + e.message);
        }
    }
