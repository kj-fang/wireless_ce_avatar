    // ── Browse for log file (server-side native dialog) ────────────
    async function browseLog() {
        const btn = document.querySelector('.btn-browse');
        btn.textContent = '⏳';
        btn.disabled = true;
        try {
            const res = await fetch(`${CHATBOT_API}/browse`);
            const data = await res.json();
            if (data.path) {
                document.getElementById('log-path-input').value = data.path;
            }
        } catch (e) {
            console.error('Browse failed:', e);
        } finally {
            btn.textContent = '📁';
            btn.disabled = false;
        }
    }

    // ── Set log file ───────────────────────────────────────────────
    async function setLog() {
        const path = document.getElementById('log-path-input').value.trim();
        const statusEl = document.getElementById('log-status');
        if (!path) { showStatus(statusEl, 'Please enter a log file path.', 'err'); return; }

        statusEl.className = 'log-status'; statusEl.textContent = 'Loading…'; statusEl.style.display = 'block';

        try {
            const res = await fetch(`${CHATBOT_API}/set_log`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({log_path: path})
            });
            const data = await res.json();
            if (data.success) {
                showStatus(statusEl, '✔ ' + data.message, 'ok');
                renderSkills(data.skills);
                logLoaded = true;
                // Enables the (collapsed) System Event Log panel when the capture
                // folder ships an .evt / .evtx next to the Wi-Fi log.
                updateEvtButton(data.evtx_path || '');
            } else {
                showStatus(statusEl, '✘ ' + data.error, 'err');
            }
        } catch (e) {
            showStatus(statusEl, '✘ Network error: ' + e.message, 'err');
        }
    }

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

        let agentCardBodyEl = null;
        let agentCardId = null;
        let stepCount = 0;

        function ensureCard() {
            if (agentCardBodyEl) return;
            removeTyping();
            hideWelcome();
            agentCardId = 'agent-process-' + Date.now();
            const html = `
            <div class="agent-process-card">
                <div class="agent-process-header" onclick="
                    this.classList.toggle('collapsed');
                    document.getElementById('${agentCardId}').classList.toggle('hidden');
                    const icon = this.querySelector('.toggle-icon');
                    icon.textContent = this.classList.contains('collapsed') ? '▼' : '▲';
                ">
                    Sleepstudy Analyzer Steps
                    <span id="${agentCardId}-summary" style="font-weight:400;font-size:0.75rem;opacity:0.75;margin-left:8px;"></span>
                    <span class="toggle-icon">▲</span>
                </div>
                <div class="agent-process-body" id="${agentCardId}"></div>
            </div>`;
            const wrapper = document.createElement('div');
            wrapper.className = 'msg-row assistant';
            wrapper.style.maxWidth = '95%';
            wrapper.innerHTML = `<div class="avatar-icon">🛌</div><div style="flex:1;">${html}</div>`;
            document.getElementById('chat-window').appendChild(wrapper);
            agentCardBodyEl = document.getElementById(agentCardId);
            scrollBottom();
        }

        function appendStep(content) {
            ensureCard();
            stepCount++;
            const stepHtml = `<div class="agent-step"><div class="agent-step-content">${marked.parse(content || '')}</div></div>`;
            agentCardBodyEl.insertAdjacentHTML('beforeend', stepHtml);
            const summaryEl = document.getElementById(agentCardId + '-summary');
            if (summaryEl) summaryEl.textContent = `${stepCount} steps`;
            scrollBottom();
        }

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
                    appendStep(evt.step && evt.step.content);
                } else if (evt.type === 'done') {
                    removeTyping();
                    if (agentCardId) collapseAgentProcessCard(agentCardId);
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

    // ── Analysis report card ───────────────────────────────────────
    function appendReport(data, header = '📊 Analysis Report', eventTime = null) {
        removeTyping();
        hideWelcome();

        const toArr = (v) => {
            if (Array.isArray(v)) return v;
            if (v == null) return [];
            if (typeof v === 'string') {
                return v.split(/\r?\n|;/).map(s => s.replace(/^[-*•\s]+/, '').trim()).filter(Boolean);
            }
            if (typeof v === 'object') return Object.values(v).map(String);
            return [String(v)];
        };
        const actions = toArr(data.recommended_actions).map(a => `<li>${escapeHtml(a)}</li>`).join('');
        const skillsArr = Array.isArray(data.involved_skills) ? data.involved_skills
                         : (data.skill_findings ? Object.keys(data.skill_findings) : toArr(data.involved_skills));
        const skills  = skillsArr.map(s => `<span class="skills-badge">${escapeHtml(String(s))}</span>`).join(' ');
        const score   = data.confidence_score || 0;
        const barWidth = Math.max(0, Math.min(100, score));

        const findings = data.skill_findings
            ? Object.entries(data.skill_findings).map(([k, v]) =>
                `<div style="margin-bottom:8px;"><strong style="font-size:0.78rem;color:var(--accent);">${escapeHtml(k)}</strong><div style="font-size:0.78rem;color:#444;white-space:pre-wrap;">${escapeHtml(String(v).substring(0,500))}${String(v).length>500?'…':''}</div></div>`
              ).join('')
            : '';

        const ts = Date.now();
        const detailsId = 'report-details-' + ts;
        const toggleId = 'toggle-' + ts;

        // Main section: always visible
        const mainHtml = `
            ${eventTime ? `
            <div class="report-row">
                <div class="report-label">Event Time</div>
                <div style="font-weight:600;color:#d97706;">${escapeHtml(eventTime)}</div>
            </div>` : ''}
            ${data.root_cause_summary ? `
            <div class="report-row">
                <div class="report-label">Root Cause</div>
                <div style="font-weight:600;color:#222;">${escapeHtml(data.root_cause_summary)}</div>
            </div>` : ''}
            ${score ? `
            <div class="report-row">
                <div class="report-label">Confidence</div>
                <div>
                    <strong>${score}%</strong>
                    <span class="confidence-bar" style="width:${barWidth}px;"></span>
                </div>
            </div>` : ''}
            ${actions ? `
            <div class="report-row">
                <div class="report-label">Recommendations</div>
                <ul style="margin:0;padding-left:16px;font-size:0.8rem;">${actions}</ul>
            </div>` : ''}`;

        // Details section: collapsible
        const detailsHtml = `
            ${skills ? `
            <div class="report-row">
                <div class="report-label">Skills Used</div>
                <div>${skills}</div>
            </div>` : ''}
            ${findings ? `
            <div class="report-row" style="flex-direction:column;">
                <div class="report-label" style="margin-bottom:6px;">Per-Skill Findings</div>
                ${findings}
            </div>` : ''}
            ${data.markdown_summary ? `
            <div class="markdown-section">${marked.parse(data.markdown_summary)}</div>` : ''}`;

        const html = `
        <div class="report-card">
            <div class="report-header">${escapeHtml(header)}</div>
            <div class="report-body">
                ${mainHtml}
            </div>
            ${detailsHtml ? `
            <div class="report-toggle" id="${toggleId}" data-state="shown"
                 onclick="toggleReportDetails('${toggleId}', '${detailsId}')"
                 style="cursor:pointer;padding:10px 16px;text-align:center;border-top:1px solid #e0e0e0;color:var(--accent);font-weight:600;font-size:0.85rem;user-select:none;"
            >\u25b2 Hide Details</div>
            <div id="${detailsId}" class="report-details" style="
                padding: 16px;
                border-top: 1px solid #f0f0f0;
                background: #fafafa;
            ">
                <div class="report-body">
                    ${detailsHtml}
                </div>
            </div>` : ''}
        </div>`;

        const wrapper = document.createElement('div');
        wrapper.className = 'msg-row assistant';
        wrapper.style.maxWidth = '95%';
        wrapper.innerHTML = `<div class="avatar-icon">🤖</div><div style="flex:1;">${html}</div>`;
        document.getElementById('chat-window').appendChild(wrapper);
        scrollBottom();

        // Auto-hide details after full render (brief flash so user sees content exists)
        if (detailsHtml) {
            setTimeout(() => toggleReportDetails(toggleId, detailsId), 600);
        }
    }

    // ── Send chat message ──────────────────────────────────────────
    async function sendMessage() {
        const input = document.getElementById('user-input');
        const text = input.value.trim();
        if (!text) return;

        input.value = '';
        input.style.height = 'auto';
        document.getElementById('send-btn').disabled = true;
        appendUserMsg(text);
        appendTyping();

        const steps = [];
        let agentCardBodyEl = null;   // live step list container created on first step
        let agentCardId = null;

        function ensureAgentCard() {
            if (agentCardBodyEl) return;
            removeTyping();
            hideWelcome();
            agentCardId = 'agent-process-' + Date.now();
            const html = `
            <div class="agent-process-card">
                <div class="agent-process-header" onclick="
                    this.classList.toggle('collapsed');
                    document.getElementById('${agentCardId}').classList.toggle('hidden');
                    const icon = this.querySelector('.toggle-icon');
                    icon.textContent = this.classList.contains('collapsed') ? '▼' : '▲';
                ">
                    Agent Processing Steps
                    <span id="${agentCardId}-summary" style="font-weight:400;font-size:0.75rem;opacity:0.75;margin-left:8px;"></span>
                    <span class="toggle-icon">▲</span>
                </div>
                <div class="agent-process-body" id="${agentCardId}"></div>
            </div>`;
            const wrapper = document.createElement('div');
            wrapper.className = 'msg-row assistant';
            wrapper.style.maxWidth = '95%';
            wrapper.innerHTML = `<div class="avatar-icon">🤖</div><div style="flex:1;">${html}</div>`;
            document.getElementById('chat-window').appendChild(wrapper);
            agentCardBodyEl = document.getElementById(agentCardId);
            scrollBottom();
        }

        function appendLiveStep(step) {
            ensureAgentCard();
            const stepHtml = `<div class="agent-step"><div class="agent-step-content">${marked.parse(step.content || '')}</div></div>`;
            agentCardBodyEl.insertAdjacentHTML('beforeend', stepHtml);
            // Update summary line
            const skillsUsed = steps
                .filter(s => s.content && s.content.includes('Invoking skill'))
                .map(s => { const m = s.content.match(/`([^`]+)`/); return m ? m[1] : ''; })
                .filter(Boolean);
            const summaryEl = document.getElementById(agentCardId + '-summary');
            if (summaryEl) summaryEl.textContent = `${steps.length} steps` + (skillsUsed.length ? ` · Skills: ${skillsUsed.join(', ')}` : '');
            scrollBottom();
        }

        try {
            const res = await fetch(`${CHATBOT_API}/chat`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({message: text, use_tools: agenticMode})
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
                    steps.push(evt.step);
                    appendLiveStep(evt.step);
                } else if (evt.type === 'done') {
                    removeTyping();
                    const result = evt.result;
                    if (result && result.type === 'report') {
                        if (agentCardId) collapseAgentProcessCard(agentCardId);
                        appendReport(result.data, '📊 Analysis Report', result.issue_time || null);
                    } else if (result && result.type === 'partial_report' && result.data) {
                        if (agentCardId) collapseAgentProcessCard(agentCardId);
                        appendReport(result.data, '⚠️ Partial Analysis (Step Limit)', result.issue_time || null);
                    } else if (result && result.type === 'text') appendAssistantText(result.data || '_No response._');
                    else if (result) appendAssistantText('⚠️ ' + (result.data || 'Unknown response.'));
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
        } finally {
            removeTyping();
            document.getElementById('send-btn').disabled = false;
        }
    }

    // === Auto-load log and trigger first Analyze-All on page load ===
    async function tryAutoAnalyzeOnLoad() {
        if (autoAnalyzeTriggered) return;

        const suggestedLog = document.getElementById('log-path-input').value.trim();
        const autoRun = new URLSearchParams(window.location.search).get('auto_run');
        const shouldAutoRun = (autoRun === 'analyze_all') || !!suggestedLog;

        if (!shouldAutoRun || !suggestedLog) return;

        autoAnalyzeTriggered = true;

        try {
            await setLog(); // load log file first

            // Wait for state synchronization
            let retries = 0;
            while ((!logLoaded || !skillsLoaded) && retries < 5) {
                await new Promise(resolve => setTimeout(resolve, 250));
                retries += 1;
            }

            // ---  fetch the concise issue description from the backend ---
            const contextRes = await fetch(`${CHATBOT_API}/get_issue_context`);
            const contextData = await contextRes.json();
            const question = contextData.description;

            // Pre-fill input and use sendMessage() so history is preserved
            setTimeout(() => {
                const input = document.getElementById('user-input');
                if (input) input.value = question || '🔍 Run full multi-skill analysis';
                sendMessage();
            }, 400);

        } catch (err) {
            autoAnalyzeTriggered = false;
            console.error('[Auto-Analysis] Failed:', err);
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        tryAutoAnalyzeOnLoad();
    });
