    // NW profile helpers: the Sleep Study panel and its History hooks. The chat
    // runtime (sendMessage / appendReport / …) lives in
    // strategies/nw-chat-runtime.js and the History panel in features/history.js.
    let firstRoundSent = false;  // read by history.js: true once this conversation has a turn

    // ── Browse for sleepstudy / driver log file ─────────────────────
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
    // One WiFiLog picked here stands for its whole WRT folder (server side).
    function browseDriverLog() { return browseInto('driver-log-path-input'); }

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
        document.getElementById('driver-log-path-input').value = '';
        const statusEl = document.getElementById('sleepstudy-status');
        statusEl.textContent = '';
        statusEl.style.display = 'none';
    }

    function onHistoryConversationLoaded(data) {
        document.getElementById('sleepstudy-path-input').value = data.log_path || '';
        document.getElementById('driver-log-path-input').value = data.driver_log_path || '';
        const statusEl = document.getElementById('sleepstudy-status');
        if (data.log_exists) {
            showStatus(statusEl, '✔ Resumed — report re-loaded.', 'ok', 3000);
        } else if (data.log_path) {
            showStatus(statusEl, '⚠ Original report not found — viewing history only.', 'warn');
        }
    }

    // ── Load sleepstudy file ────────────────────────────────────────
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
            if (data.success) {
                showStatus(statusEl, '✔ ' + (data.message || 'Sleepstudy file loaded.'), 'ok');
                if (data.skills) renderSkills(data.skills);
                window.__feedbackConversationId = '';
                firstRoundSent = false;
                window.__viewingSavedConv = false;
                document.getElementById('chat-window').innerHTML = '';
                if (window.IpsPrompt) { await window.IpsPrompt.handleSetLog(data); }
                // With a driver log: worst sessions → driver log agent per session.
                // Without one: the Sleep Study-only Wi-Fi report.
                const driverPath = document.getElementById('driver-log-path-input').value.trim();
                await runSleepstudyAnalysis(path, driverPath);
            } else {
                showStatus(statusEl, '✘ ' + data.error, 'err');
            }
        } catch (e) {
            showStatus(statusEl, '✘ Network error: ' + e.message, 'err');
        }
    }

    // The user turn an analysis is saved as; the server's
    // _sleepstudy_request_text writes the same text to History.
    function sleepstudyRequestText(path, driverPath) {
        const base = (p) => p.replace(/[\\/]+$/, '').split(/[\\/]/).pop();
        const lines = ['Sleep Study: ' + base(path) + (driverPath ? ' × ' + base(driverPath) : ''),
                       'Report: `' + path + '`'];
        if (driverPath) lines.push('Driver log: `' + driverPath + '`');
        return lines.join('\n');
    }

    // ── Run dedicated sleepstudy analyzer (SSE stream) ──────────────
    async function runSleepstudyAnalysis(path, driverPath) {
        appendUserMsg(sleepstudyRequestText(path, driverPath));
        appendTyping();

        const card = createAgentStepRenderer({
            title: driverPath ? 'Sleepstudy × Driver Log Steps' : 'Sleepstudy Analyzer Steps',
            avatar: '🛌',
        });

        try {
            const res = await fetch(
                `${CHATBOT_API}/${driverPath ? 'analyze_sleepstudy_driver' : 'analyze_sleepstudy'}`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(driverPath
                    ? {log_path: path, driver_log_path: driverPath}
                    : {log_path: path})
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
