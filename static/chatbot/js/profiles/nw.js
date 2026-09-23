    // NW profile helpers. The chat runtime (browse / setLog / sendMessage /
    // appendReport / auto-analyze) lives in strategies/nw-chat-runtime.js;
    // only the sleepstudy sub-tab is NW-specific.

    // ── Switch between Wi-Fi log and Sleepstudy sub-tabs ────────────
    function switchLogTab(which) {
        const wifiBtn = document.getElementById('logtab-wifi');
        const sleepBtn = document.getElementById('logtab-sleep');
        const wifiPanel = document.getElementById('logpanel-wifi');
        const sleepPanel = document.getElementById('logpanel-sleep');
        const activeStyle = (btn, active) => {
            btn.style.color = active ? 'var(--accent)' : '#888';
            btn.style.borderBottom = active ? '2px solid var(--accent)' : '2px solid transparent';
        };
        if (which === 'wifi') {
            wifiPanel.style.display = '';
            sleepPanel.style.display = 'none';
            activeStyle(wifiBtn, true);
            activeStyle(sleepBtn, false);
        } else {
            wifiPanel.style.display = 'none';
            sleepPanel.style.display = '';
            activeStyle(wifiBtn, false);
            activeStyle(sleepBtn, true);
        }
    }

    // ── Browse for sleepstudy file ──────────────────────────────────
    async function browseSleepstudy() {
        const btn = document.querySelector('#sleepstudy-path-input + .btn-browse');
        if (btn) { btn.textContent = '⏳'; btn.disabled = true; }
        try {
            const res = await fetch(`${CHATBOT_API}/browse`);
            const data = await res.json();
            if (data.path) {
                document.getElementById('sleepstudy-path-input').value = data.path;
            }
        } catch (e) {
            console.error('Browse sleepstudy failed:', e);
        } finally {
            if (btn) { btn.textContent = '📁'; btn.disabled = false; }
        }
    }

    // ── Load sleepstudy file ────────────────────────────────────────
    async function setSleepstudy() {
        const path = document.getElementById('sleepstudy-path-input').value.trim();
        const statusEl = document.getElementById('sleepstudy-status');
        if (!path) { showStatus(statusEl, 'Please enter a sleepstudy file path.', 'err'); return; }

        statusEl.className = 'log-status'; statusEl.textContent = 'Loading…'; statusEl.style.display = 'block';

        try {
            const res = await fetch(`${CHATBOT_API}/set_log_sleepstudy`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({log_path: path})
            });
            const data = await res.json();
            if (data.success) {
                showStatus(statusEl, '✔ ' + (data.message || 'Sleepstudy file loaded.'), 'ok');
                // Run the dedicated sleepstudy analyzer pipeline
                runSleepstudyAnalysis(path);
            } else {
                showStatus(statusEl, '✘ ' + data.error, 'err');
            }
        } catch (e) {
            showStatus(statusEl, '✘ Network error: ' + e.message, 'err');
        }
    }

    // ── Run dedicated sleepstudy analyzer (SSE stream) ──────────────
    async function runSleepstudyAnalysis(path) {
        appendUserMsg('Analysing the uploaded sleepstudy at `' + path + '`');
        appendTyping();

        const card = createAgentStepRenderer({
            title: 'Sleepstudy Analyzer Steps',
            avatar: '🛌',
        });

        try {
            const res = await fetch(`${CHATBOT_API}/analyze_sleepstudy`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({log_path: path})
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

    document.addEventListener('DOMContentLoaded', function () {
        tryAutoAnalyzeOnLoad();
    });
