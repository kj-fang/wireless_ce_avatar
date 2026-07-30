    let logLoaded = false;
    let skillsLoaded = false;
    let autoAnalyzeTriggered = false;
    // Agentic mode is no longer user-selectable: the sidebar "AI Mode" toggle
    // was removed, so every agent always runs with skills/tools available.
    // Still sent as `use_tools` on /chat, which the backends expect.
    const agenticMode = true;

    const CHATBOT_API = (window.CHATBOT && window.CHATBOT.api) || '';

    // ── Utilities ──────────────────────────────────────────────────
    function autoResize(el) {
        el.style.height = 'auto';
        el.style.height = Math.min(el.scrollHeight, 120) + 'px';
    }

    function onChatInput(el) {
        if (typeof window.onUserInputChange === 'function') {
            window.onUserInputChange(el);
            return;
        }
        autoResize(el);
    }

    function handleEnter(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            onSendBtnClick();
        }
    }

    // ── Stop / abort support ───────────────────────────────────────
    // While a tools-mode analysis streams, the Send button becomes a red Stop
    // button. Clicking it aborts the SSE stream locally (via the shared
    // window.__streamCtl AbortController that each chat runtime installs) AND
    // asks the backend to halt the running analysis — its agent loop bails out
    // at the next reasoning-step boundary.
    //
    // Lives here rather than in each profile because only the endpoint differs,
    // and that already comes from CHATBOT_API.
    let __chatStreaming = false;

    function isChatStreaming() { return __chatStreaming; }

    function setSendBtnStopMode() {
        __chatStreaming = true;
        const btn = document.getElementById('send-btn');
        if (!btn) return;
        btn.disabled = false;
        btn.classList.add('stopping');
        btn.textContent = 'Stop ■';
    }

    function setSendBtnSendMode() {
        __chatStreaming = false;
        window.__streamCtl = null;
        const btn = document.getElementById('send-btn');
        if (!btn) return;
        btn.classList.remove('stopping');
        btn.textContent = 'Send ↑';
        // Re-derive the disabled state from the current input validity on the
        // profiles that gate Send (BT / Wi-Fi); plain enable elsewhere (NW).
        if (typeof window.validateUserInput === 'function') window.validateUserInput(false);
        else btn.disabled = false;
    }

    // Single click entry point for the Send/Stop button (and the Enter key).
    function onSendBtnClick() {
        if (__chatStreaming) { stopChat(); return; }
        sendMessage();
    }

    async function stopChat() {
        // Stop rendering immediately by aborting the active stream.
        if (window.__streamCtl) { try { window.__streamCtl.abort(); } catch (e) {} }
        // Drop any queued multi-incident continuations so the chain ends here.
        window.__multiTimeContext = null;
        // Ask the backend to halt the running analysis (cooperative cancel).
        try {
            await fetch(`${CHATBOT_API}/chat/stop`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ conversation_id: window.__feedbackConversationId || '' }),
            });
        } catch (e) { /* best-effort */ }
        removeTyping();
        appendAssistantText('⏹️ Analysis stopped by user.');
        setSendBtnSendMode();
    }

    function scrollBottom() {
        const w = document.getElementById('chat-window');
        w.scrollTop = w.scrollHeight;
    }

    function hideWelcome() {
        const w = document.getElementById('welcome-msg');
        if (w) w.remove();
    }

    function escapeHtml(s) {
        return String(s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    // ttlMs (optional): when > 0, auto-clear this status after that many ms
    // (used for transient "✔ loaded / resumed" confirmations so they don't
    // permanently occupy the sidebar). A newer message cancels the pending
    // hide, and the hide only fires if the same message is still showing.
    function showStatus(el, msg, type, ttlMs) {
        el.textContent = msg;
        el.className = 'log-status ' + type;
        el.style.display = 'block';
        if (el._hideTimer) { clearTimeout(el._hideTimer); el._hideTimer = null; }
        if (ttlMs && ttlMs > 0) {
            el._hideTimer = setTimeout(function () {
                if (el.textContent === msg) {
                    el.className = 'log-status';
                    el.textContent = '';
                    el.style.display = 'none';
                }
            }, ttlMs);
        }
    }

    // ── Append messages ────────────────────────────────────────────
    function appendUserMsg(text) {
        hideWelcome();
        const row = document.createElement('div');
        row.className = 'msg-row user';
        row.innerHTML = `
            <div class="avatar-icon">👤</div>
            <div class="msg-bubble">${escapeHtml(text)}</div>
        `;
        document.getElementById('chat-window').appendChild(row);
        scrollBottom();
    }

    function appendTyping() {
        hideWelcome();
        const row = document.createElement('div');
        row.className = 'msg-row assistant';
        row.id = 'typing-row';
        row.innerHTML = `
            <div class="avatar-icon">🤖</div>
            <div class="typing-indicator">
                <span class="dot"></span><span class="dot"></span><span class="dot"></span>
                &nbsp;Analysing…
            </div>
        `;
        document.getElementById('chat-window').appendChild(row);
        scrollBottom();
        return row;
    }

    function removeTyping() {
        const t = document.getElementById('typing-row');
        if (t) t.remove();
    }

    // turnId is only supplied by agents that enable the feedback sidecar; the
    // widget is skipped entirely when the page does not define it.
    function appendAssistantText(text, turnId) {
        removeTyping();
        const showFeedback = turnId && typeof renderFeedbackWidget === 'function';
        const row = document.createElement('div');
        row.className = 'msg-row assistant';
        row.innerHTML = `
            <div class="avatar-icon">🤖</div>
            <div style="flex:1;min-width:0;">
                <div class="msg-bubble">${marked.parse(text)}</div>
                ${showFeedback ? renderFeedbackWidget(turnId) : ''}
            </div>
        `;
        document.getElementById('chat-window').appendChild(row);
        scrollBottom();
    }

    function toggleReportDetails(toggleBtnId, detailsId) {
        const details = document.getElementById(detailsId);
        const btn = document.getElementById(toggleBtnId);
        if (!details || !btn) return;
        const isHidden = btn.dataset.state === 'hidden';
        if (isHidden) {
            details.classList.remove('hidden');
            btn.dataset.state = 'shown';
            btn.textContent = '\u25b2 Hide Details';
        } else {
            details.classList.add('hidden');
            btn.dataset.state = 'hidden';
            btn.textContent = '\u25bc Show Details';
        }
    }

    // ── Browse for skills YAML file ────────────────────────────
    async function browseYaml() {
        const btn = document.querySelector('#yaml-path-input + .btn-browse');
        btn.textContent = '⏳';
        btn.disabled = true;
        try {
            const res = await fetch(`${CHATBOT_API}/browse_yaml`);
            const data = await res.json();
            if (data.path) {
                document.getElementById('yaml-path-input').value = data.path;
            }
        } catch (e) {
            console.error('Browse YAML failed:', e);
        } finally {
            btn.textContent = '📁';
            btn.disabled = false;
        }
    }

    // ── Load skills from YAML file ──────────────────────────────
    async function loadSkillsYaml() {
        const yamlPath = document.getElementById('yaml-path-input').value.trim();
        const statusEl = document.getElementById('yaml-load-status');
        if (!yamlPath) {
            showStatus(statusEl, 'Please enter a YAML file path.', 'err');
            return;
        }
        statusEl.textContent = 'Loading…';
        statusEl.className = 'log-status';
        statusEl.style.display = 'block';
        try {
            const res = await fetch(`${CHATBOT_API}/load_skills_yaml`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({yaml_path: yamlPath})
            });
            const data = await res.json();
            if (data.success) {
                showStatus(statusEl, '✔ ' + data.message, 'ok');
                renderSkills(data.skills);
            } else {
                showStatus(statusEl, '✘ ' + data.error, 'err');
            }
        } catch (e) {
            showStatus(statusEl, '✘ Network error: ' + e.message, 'err');
        }
    }

    // ── Browse for skills data directory ──────────────────────────
    async function browseSkillsDir() {
        const btn = document.querySelector('#skills-dir-input + .btn-browse');
        btn.textContent = '⏳';
        btn.disabled = true;
        try {
            const res = await fetch(`${CHATBOT_API}/browse_dir`);
            const data = await res.json();
            if (data.path) {
                document.getElementById('skills-dir-input').value = data.path;
            }
        } catch (e) {
            console.error('Browse dir failed:', e);
        } finally {
            btn.textContent = '📁';
            btn.disabled = false;
        }
    }

    // ── Reload skills from directory ───────────────────────────────
    async function reloadSkills() {
        const dir = document.getElementById('skills-dir-input').value.trim();
        const statusEl = document.getElementById('skills-reload-status');
        statusEl.textContent = 'Reloading…';
        statusEl.className = 'log-status';
        statusEl.style.display = 'block';
        try {
            const res = await fetch(`${CHATBOT_API}/reload_skills`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({data_dir: dir})
            });
            const data = await res.json();
            if (data.success) {
                if (data.warning) {
                    showStatus(statusEl, '⚠ ' + data.warning, 'warn');
                } else {
                    showStatus(statusEl, '✔ ' + data.message, 'ok');
                }
                renderSkills(data.skills);
            } else {
                showStatus(statusEl, '✘ ' + data.error, 'err');
            }
        } catch (e) {
            showStatus(statusEl, '✘ Network error: ' + e.message, 'err');
        }
    }

    // ── Reload skills from shared folder ────────────────────────────
    async function reloadFromShared() {
        const statusEl = document.getElementById('reload-shared-status');
        statusEl.textContent = 'Reloading…';
        statusEl.className = 'log-status';
        statusEl.style.display = 'block';
        try {
            const res = await fetch(`${CHATBOT_API}/reload_from_shared`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({})
            });
            const data = await res.json();
            if (data.success) {
                showStatus(statusEl, '✔ ' + data.message, 'ok');
                renderSkills(data.skills);
                skillsLoaded = true;
            } else {
                showStatus(statusEl, '✘ ' + data.error, 'err');
            }
        } catch (e) {
            showStatus(statusEl, '✘ Network error: ' + e.message, 'err');
        }
    }

    // Skills are presented as collapsed disclosures: expanding one only reveals
    // its description and never starts a conversation with the agent.
    function renderSkills(skills) {
        const container = document.getElementById('skills-list');
        container.innerHTML = '';
        if (!skills || skills.length === 0) {
            const empty = document.createElement('div');
            empty.style.cssText = 'font-size:0.78rem;color:#aaa;';
            empty.textContent = 'No skills loaded.';
            container.appendChild(empty);
            skillsLoaded = false;
        } else {
            // Build nodes via the DOM API and assign text via textContent so
            // skill names/descriptions from the user's YAML can never break the
            // markup or inject script.
            skills.forEach((s) => {
                const name = String(s && s.name != null ? s.name : '');
                const card = document.createElement('details');
                card.className = 'skill-card';
                const summary = document.createElement('summary');
                const nameEl = document.createElement('div');
                nameEl.className = 'skill-name';
                nameEl.textContent = name;
                summary.appendChild(nameEl);
                card.appendChild(summary);
                if (s && s.description) {
                    const description = document.createElement('div');
                    description.className = 'skill-desc';
                    description.textContent = String(s.description);
                    card.appendChild(description);
                }
                container.appendChild(card);
            });
            skillsLoaded = true;
        }
    }

    // ── Agent process cards ─────────────────────────────────────────
    function collapseAgentProcessCard(cardId) {
        const body = document.getElementById(cardId);
        if (!body) return;
        const card = body.closest('.agent-process-card');
        const header = card ? card.querySelector('.agent-process-header') : null;
        if (header && !header.classList.contains('collapsed')) {
            header.classList.add('collapsed');
            body.classList.add('hidden');
            const toggleIcon = header.querySelector('.toggle-icon');
            if (toggleIcon) toggleIcon.textContent = '▼';
        }
    }

    let lazyMdObserver = null;

    function ensureLazyMarkdownObserver() {
        if (lazyMdObserver || !('IntersectionObserver' in window)) return;
        lazyMdObserver = new IntersectionObserver((entries) => {
            entries.forEach((entry) => {
                if (!entry.isIntersecting) return;
                renderLazyMarkdown(entry.target);
                lazyMdObserver.unobserve(entry.target);
            });
        }, { root: null, threshold: 0.01, rootMargin: '200px 0px' });
    }

    function renderLazyMarkdown(el) {
        if (!el || el.dataset.mdRendered === '1') return;
        const raw = el.__mdRaw || el.textContent || '';
        el.innerHTML = marked.parse(raw);
        el.dataset.mdRendered = '1';
    }

    function createLazyStepNode(content) {
        const step = document.createElement('div');
        step.className = 'agent-step';

        const body = document.createElement('div');
        body.className = 'agent-step-content';
        body.textContent = content;
        body.__mdRaw = content;
        body.dataset.mdRendered = '0';
        step.appendChild(body);

        ensureLazyMarkdownObserver();
        if (lazyMdObserver) {
            lazyMdObserver.observe(body);
        } else {
            renderLazyMarkdown(body); // Fallback for older browsers.
        }

        return step;
    }

    function sendQuick(text) {
        document.getElementById('user-input').value = text;
        sendMessage();
    }

    // ── Render agent steps with dynamic loading and auto-collapse ────
    function appendAgentProcess(steps) {
        hideWelcome();
        removeTyping();

        const cardId = 'agent-process-' + Date.now();
        const skillsUsed = steps
            .filter(s => s.content && s.content.includes('Invoking skill'))
            .map(s => { const m = s.content.match(/`([^`]+)`/); return m ? m[1] : ''; })
            .filter(Boolean);

        const summaryText = `${steps.length} steps` +
            (skillsUsed.length ? ` · Skills: ${skillsUsed.join(', ')}` : '');

        // Create the container (initially collapsed)
        const html = `
        <div class="agent-process-card">
            <div class="agent-process-header" onclick="
                this.classList.toggle('collapsed');
                document.getElementById('${cardId}').classList.toggle('hidden');
                const icon = this.querySelector('.toggle-icon');
                if (this.classList.contains('collapsed')) icon.textContent = '\u25bc';
                else icon.textContent = '\u25b2';
            ">
                Agent Processing Steps
                <span style="font-weight:400;font-size:0.75rem;opacity:0.75;margin-left:8px;">${escapeHtml(summaryText)}</span>
                <span class="toggle-icon">\u25b2</span>
            </div>
            <div class="agent-process-body" id="${cardId}">
            </div>
        </div>`;

        const wrapper = document.createElement('div');
        wrapper.className = 'msg-row assistant';
        wrapper.style.maxWidth = '95%';
        wrapper.innerHTML = `<div class="avatar-icon">🤖</div><div style="flex:1;">${html}</div>`;
        document.getElementById('chat-window').appendChild(wrapper);
        scrollBottom();

        // Append steps immediately for smoother UX
        const bodyEl = document.getElementById(cardId);
        steps.forEach((step, idx) => {
            bodyEl.appendChild(createLazyStepNode(step.content || ''));
            if (idx === steps.length - 1) {
                scrollBottom();
            }
        });
    }
