    class WifiChatRuntimeStrategy {
        constructor(profile) {
            this.profile = Object.freeze({ ...(profile || {}) });
        }
    }

    WifiChatRuntimeStrategy.prototype.appendIncidentTag = function (idx, total, timeObj) {
        if (!total || total <= 1) return;
        const fmt = _formatEitRowString(timeObj);
        const wrapper = document.createElement('div');
        wrapper.className = 'msg-row assistant';
        wrapper.innerHTML =
            `<div class="avatar-icon">🕒</div>` +
            `<div style="flex:1;"><span class="incident-tag">Incident ${idx} / ${total}` +
            (fmt ? ` &nbsp;·&nbsp; ${fmt}` : '') + `</span></div>`;
        document.getElementById('chat-window').appendChild(wrapper);
        scrollBottom();
    };

    WifiChatRuntimeStrategy.prototype.appendReport = function (data, header = '📊 Analysis Report', eventTime = null, turnId = null) {
        removeTyping();
        hideWelcome();

        const actions = (data.recommended_actions || []).map(a => `<li>${escapeHtml(a)}</li>`).join('');
        const skills  = (data.involved_skills || data.skill_findings ? Object.keys(data.skill_findings || {}) : [])
                         .map(s => `<span class="skills-badge">${s}</span>`).join(' ');
        const score   = data.confidence_score || 0;
        const barWidth = Math.max(0, Math.min(100, score));

        const findings = data.skill_findings
            ? Object.entries(data.skill_findings).map(([k, v]) =>
                `<div style="margin-bottom:8px;"><strong style="font-size:0.78rem;color:#0071c5;">${k}</strong><div style="font-size:0.78rem;color:#444;white-space:pre-wrap;">${escapeHtml(String(v).substring(0,500))}${String(v).length>500?'…':''}</div></div>`
              ).join('')
            : '';

        const ts = Date.now();
        const reportId = 'report-' + ts;
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
                 style="cursor:pointer;padding:10px 16px;text-align:center;border-top:1px solid #e0e0e0;color:#0071c5;font-weight:600;font-size:0.85rem;user-select:none;"
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
        wrapper.innerHTML = `<div class="avatar-icon">🤖</div><div style="flex:1;">${html}${turnId ? renderFeedbackWidget(turnId) : ''}</div>`;
        document.getElementById('chat-window').appendChild(wrapper);
        scrollBottom();

        // Auto-hide details after full render (brief flash so user sees content exists)
        if (detailsHtml) {
            setTimeout(() => toggleReportDetails(toggleId, detailsId), 600);
        }
    };

    WifiChatRuntimeStrategy.prototype.browseLog = async function () {
        const btn = document.querySelector('.btn-browse');
        btn.textContent = '⏳';
        btn.disabled = true;
        try {
            const res = await fetch('/log_chatbot/browse');
            const data = await res.json();
            if (data.path) {
                document.getElementById('log-path-input').value = data.path;
                await setLog();
            }
        } catch (e) {
            console.error('Browse failed:', e);
        } finally {
            btn.textContent = '📁';
            btn.disabled = false;
        }
    };

    WifiChatRuntimeStrategy.prototype.copyLogPath = async function (btn) {
        const path = (document.getElementById('log-path-input') || {}).value || '';
        if (!path.trim()) return;
        try {
            await navigator.clipboard.writeText(path);
        } catch (_e) {
            const inp = document.getElementById('log-path-input');
            inp.select(); document.execCommand('copy');
        }
        if (btn) {
            btn.classList.add('copied');
            setTimeout(() => btn.classList.remove('copied'), 1200);
        }
    };

    WifiChatRuntimeStrategy.prototype.sendMessage = async function () {
        const input = document.getElementById('user-input');
        let desc = input.value.trim();

        // ── Multi-time CONTINUATION? ──────────────────────────────────
        // When the previous /chat iteration finished and there are
        // queued issue times, sendMessage is re-entered with the saved
        // desc + the next time popped from the queue.
        const isContinuation = !!window.__multiTimeContext;
        let multiTimeForThisIter = null;
        // parentMessageId: UUID shared across every iteration of a
        // single Send click. Created on the FIRST iteration, reused
        // on all continuations, sent to /chat so the sidecar stamps
        // every resulting turn with it (multi-incident co-firing
        // recoverable downstream).
        let parentMessageId = '';

        if (isContinuation) {
            const ctx = window.__multiTimeContext;
            multiTimeForThisIter = ctx.queue.shift();
            ctx.currentIdx += 1;
            desc = ctx.desc;
            parentMessageId = ctx.parentMessageId || '';
            appendIncidentTag(ctx.currentIdx, ctx.total, multiTimeForThisIter);
        } else {
            parentMessageId = _uuid4();
        }

        if (!desc) return;

        // Hard blockers (log not loaded, empty description). Skipped on
        // continuations because the textbox was cleared after iteration
        // 1 — we already know the original input passed validation.
        if (!isContinuation && !validateUserInput(true)) {
            input.focus();
            return;
        }

        // ── Detect time tokens inside the typed message ──────────────
        // Skipped on continuations and on the second-pass call after
        // the popup has already been resolved (via __skipTimeCaptureOnce).
        if (!isContinuation && !window.__skipTimeCaptureOnce) {
            const detected = _detectIssueTimesInText(desc);
            if (detected.length > 0) {
                // The popup's buttons drive what happens next: "Apply & send to
                // agent" re-runs this send (with the skip-once guard), while
                // "Confirm time(s)" just fills the sidebar.
                openTimeCaptureModal(detected);
                return;     // wait for popup before continuing
            }
        }
        window.__skipTimeCaptureOnce = false;

        // ── Decide the issue time for THIS iteration ──────────────────
        let issueTime = '';
        if (multiTimeForThisIter) {
            issueTime = _formatEitRowString(multiTimeForThisIter);
        } else if (!isContinuation) {
            // Fresh send: gather all times. If >1, set up multi-time
            // context now and use the FIRST time on this iteration; the
            // rest will be processed in chained re-calls from finally{}.
            const allTimes = getAllIssueTimesParsed();
            if (allTimes.length > 1) {
                window.__multiTimeContext = {
                    desc:             desc,
                    queue:            allTimes.slice(1),
                    currentIdx:       1,
                    total:            allTimes.length,
                    parentMessageId:  parentMessageId,
                };
                issueTime = _formatEitRowString(allTimes[0]);
                appendIncidentTag(1, allTimes.length, allTimes[0]);
            } else if (allTimes.length === 1) {
                issueTime = _formatEitRowString(allTimes[0]);
            }
        }

        // Soft gate: first round without an Issue Time. Ask for explicit
        // confirmation via a modal dialog before proceeding.
        const firstRound = !firstRoundSent;
        if (firstRound && !issueTime) {
            const confirmed = await confirmNoIssueTime();
            if (!confirmed) {
                input.focus();
                return;
            }
        }

        // Compose final message: description + (optional) issue time hint.
        const cleanDesc = isAutoFillMode
            ? trimTrailingTimeConnector(stripTimestampsFromDesc(desc))
            : desc;
        const text = issueTime
            ? `${cleanDesc} at around ${issueTime}`
            : cleanDesc;

        // Reset the textbox only on the FIRST iteration (not on
        // continuations — we don't want the user's input to disappear
        // before all queued times have been processed).
        if (!isContinuation) {
            input.value = '';
            input.style.height = 'auto';
            // The draft was sent — drop the saved draft (text + issue time) for
            // this conversation so switching back to it shows the persisted
            // server state (incl. its issue time), not the now-stale draft.
            if (window.__draftByConv) delete window.__draftByConv[_draftKey()];
            // Hide the prefill / time hints once the message is actually sent.
            ['prefill-hint','time-hint','no-time-hint','no-desc-hint','no-log-hint']
                .forEach((id) => {
                    const el = document.getElementById(id);
                    if (el) el.style.display = 'none';
                });
        }
        isAutoFillMode = false;  // auto-fill obligation fulfilled
        firstRoundSent = true;   // subsequent rounds skip the strict gate
        let __chatStreamAborted = false;  // set if user switches away mid-stream

        // Clear the sidebar Issue Time only when we won't need it again.
        // Single-time send → clear now. Multi-time → clear after the
        // last iteration finishes (handled in the finally block below).
        if (!window.__multiTimeContext) {
            clearIssueTime();
        }
        document.getElementById('send-btn').disabled = true;
        // Only show the user's typed message ONCE — at the start of the
        // very first iteration. Continuations re-use the same message.
        if (!isContinuation) appendUserMsg(text);
        const typingRow = appendTyping();

        const steps = [];
        let agentCardBodyEl = null;   // live step list container created on first step
        let agentCardId = null;
        const streamStartTs = Date.now();
        let lastStepTs = streamStartTs;

        /** Format an elapsed milliseconds value as a human-friendly badge. */
        function fmtElapsed(ms) {
            if (ms < 1000) return `${ms}ms`;
            if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
            const m = Math.floor(ms / 60000);
            const s = Math.floor((ms % 60000) / 1000);
            return `${m}m${String(s).padStart(2, '0')}s`;
        }

        function buildTimeBadge(elapsedMs, deltaMs) {
            const elapsed = fmtElapsed(elapsedMs);
            const delta = (deltaMs >= 50)
                ? `<span class="step-time-delta">Δ${fmtElapsed(deltaMs)}</span>`
                : '';
            return `<span class="step-time" title="Elapsed since start · delta from previous step">+${elapsed}${delta}</span>`;
        }

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
            const content = step.content || '';
            const role = step.role || 'agent';

            // Classify step type by role + content keywords
            let stepClass = 'step-info';
            let label = '';
            let isStepDivider = false;
            let stepNum = null;

            if (role === 'token_usage') {
                stepClass = 'step-token';
                label = '📊 Tokens';
            } else if (role === 'error') {
                stepClass = 'step-error';
                label = '⛔ Error';
            } else if (/Reasoning Step (\d+)/.test(content)) {
                // Extract step number and render as a visual divider
                const m = content.match(/Reasoning Step (\d+)/);
                stepNum = m ? m[1] : steps.length;
                isStepDivider = true;
            } else if (/🧠.*Thinking/i.test(content)) {
                stepClass = 'step-thinking';
                label = '🧠 Thinking';
            } else if (/Invoking skill|fetch_filtered_logs/i.test(content)) {
                stepClass = 'step-skill';
                label = '🔬 Skill';
            } else if (/Tool call|Tool cap|🧭/i.test(content)) {
                stepClass = 'step-tool';
                label = '🧭 Tools';
            } else if (/Conclusion reached|submit_final_report|✅/i.test(content)) {
                stepClass = 'step-done';
                label = '✅ Done';
            } else {
                stepClass = 'step-info';
                label = 'ℹ️';
            }

            let html;
            const now = Date.now();
            const elapsedMs = now - streamStartTs;
            const deltaMs = now - lastStepTs;
            lastStepTs = now;
            const timeBadge = buildTimeBadge(elapsedMs, deltaMs);

            if (isStepDivider) {
                html = `<div class="agent-step-divider"><span class="step-num">${stepNum}</span>Reasoning Step ${stepNum}${timeBadge}</div>`;
            } else {
                html = `<div class="agent-step ${stepClass}">
                    <span class="step-label">${label}</span>
                    <div class="agent-step-content">${marked.parse(content)}</div>
                    ${timeBadge}
                </div>`;
            }

            agentCardBodyEl.insertAdjacentHTML('beforeend', html);

            // Update summary line (also surface total elapsed so far)
            const skillsUsed = steps
                .filter(s => s.content && s.content.includes('Invoking skill'))
                .map(s => { const m = s.content.match(/`([^`]+)`/); return m ? m[1] : ''; })
                .filter(Boolean);
            const summaryEl = document.getElementById(agentCardId + '-summary');
            if (summaryEl) {
                summaryEl.textContent =
                    `${steps.length} steps · ${fmtElapsed(elapsedMs)}` +
                    (skillsUsed.length ? ` · Skills: ${skillsUsed.join(', ')}` : '');
            }
            scrollBottom();
        }

        try {
            // Register this stream so switching to another conversation can
            // stop its rendering. The server-side analysis keeps running and
            // can be re-attached to via the History sidebar.
            if (window.__streamCtl) { try { window.__streamCtl.abort(); } catch (e) {} }
            const __chatCtl = new AbortController();
            window.__streamCtl = __chatCtl;
            const res = await fetch('/log_chatbot/chat', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                signal: __chatCtl.signal,
                body: JSON.stringify({
                    message: text,
                    use_tools: agenticMode,
                    // Sidebar Issue Time is the SINGLE source of truth.
                    // Always send the field explicitly so the backend can
                    // override / clear any pre-primed attachment_time.
                    // issue_time_cleared=true signals the user explicitly
                    // removed all time fields (vs. time-only with blank date).
                    issue_time: issueTime || "",
                    issue_time_cleared: !issueTime && !checkIssueTime().anyFilled,
                    // Per-Send UUID; shared across every iteration of a
                    // multi-incident chained send so all resulting turns
                    // carry the same parent_message_id in the snapshot.
                    parent_message_id: parentMessageId,
                    // Sidebar-adjustable ±N min log capture window.
                    issue_time_window_minutes: getIssueWindowMinutes(),
                })
            });

            if (!res.ok) throw new Error(`Server error ${res.status}`);

            // The analysis is now registered as a running job server-side.
            // Refresh the sidebar immediately so its ⏳ entry shows up right
            // away; loadHistoryList then self-polls until the job finishes.
            if (typeof loadHistoryList === 'function') loadHistoryList();

            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            const processLine = (line) => {
                if (!line.startsWith('data:')) return;
                const jsonStr = line.slice(5).trim();
                if (!jsonStr) return;
                let evt;
                try { evt = JSON.parse(jsonStr); } catch { return; }

                // Superseded stream (user navigated away / resumed a draft /
                // started a new session): stop ALL rendering immediately so a
                // backgrounded analysis can't keep pumping markdown into the
                // DOM and make typing in the new session lag.
                if (window.__streamCtl !== __chatCtl) return;

                if (evt.type === 'step') {
                    steps.push(evt.step);
                    appendLiveStep(evt.step);
                } else if (evt.type === 'done') {
                    // A superseded stream (user switched conversation / resumed
                    // a draft / started a new session) must not rebind the
                    // global conversation id or render into the now-different
                    // chat window — that corrupts the current draft's context.
                    if (window.__streamCtl !== __chatCtl) return;
                    removeTyping();
                    const result = evt.result;
                    const turnId = evt.turn_id || null;
                    if (evt.conversation_id) window.__feedbackConversationId = evt.conversation_id;
                    if (turnId) {
                        captureTurnContext(turnId, steps);
                    }
                    // Keep the history sidebar current — this turn was just
                    // persisted server-side, so refresh the list.
                    if (typeof loadHistoryList === 'function') loadHistoryList();
                    if (result && result.type === 'report') {
                        if (agentCardId) collapseAgentProcessCard(agentCardId);
                        appendReport(result.data, '📊 Analysis Report', result.issue_time || null, turnId);
                    } else if (result && result.type === 'partial_report' && result.data) {
                        if (agentCardId) collapseAgentProcessCard(agentCardId);
                        appendReport(result.data, '⚠️ Partial Analysis (Step Limit)', result.issue_time || null, turnId);
                    } else if (result && result.type === 'text') appendAssistantText(result.data || '_No response._', turnId);
                    else if (result) appendAssistantText('⚠️ ' + (result.data || 'Unknown response.'), turnId);
                } else if (evt.type === 'error') {
                    if (window.__streamCtl !== __chatCtl) return;
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
            if (e.name === 'AbortError') {
                // User switched conversations mid-stream — the server job keeps
                // running; just stop rendering here, no error to show.
                __chatStreamAborted = true;
            } else {
                removeTyping();
                appendAssistantText('❌ Network error: ' + e.message);
            }
        } finally {
            removeTyping();
            // ── Multi-time chain: if the queue still has unprocessed
            // issue times, schedule the next iteration AND keep Send
            // disabled. Re-enabling Send between iterations lets the
            // user click it during the ~250ms gap, which would re-
            // enter sendMessage with isContinuation=true (because
            // __multiTimeContext is still set) — popping the queue
            // early and ignoring whatever they had just typed.
            // We only re-enable Send when the chain is fully drained
            // (or on a regular single-time finish).
            const stillChaining = !__chatStreamAborted
                                   && !!(window.__multiTimeContext
                                         && window.__multiTimeContext.queue.length > 0);
            if (stillChaining) {
                setTimeout(() => sendMessage(), 250);
                // intentionally NOT touching send-btn.disabled here
            } else {
                if (window.__multiTimeContext) {
                    // Last iteration of the multi-time chain — reset state
                    // and clean the sidebar so the next Send starts fresh.
                    window.__multiTimeContext = null;
                    clearIssueTime();
                    clearAllExtraIssueTimes();
                }
                document.getElementById('send-btn').disabled = false;
            }
        }
    };

    WifiChatRuntimeStrategy.prototype.setLog = async function (opts) {
        // When restoring a per-conversation draft we manage the Issue Time
        // ourselves (from the saved draft), so the caller can suppress this
        // function's own async issue-time auto-fill — otherwise that
        // fire-and-forget fetch resolves AFTER the draft time is set and
        // clobbers it with the log's last timestamp.
        const skipIssueTimeAutofill = !!(opts && opts.skipIssueTimeAutofill);
        const path = document.getElementById('log-path-input').value.trim();
        const statusEl = document.getElementById('log-status');
        if (!path) { showStatus(statusEl, 'Please enter a log file path.', 'err'); return; }
        // Bump a generation token so a previous setLog()'s still-pending
        // async issue-time auto-fill can't resolve late and overwrite a newer
        // load's (or a restored draft's) issue time.
        const _myLogGen = (window.__setLogGen = (window.__setLogGen || 0) + 1);

        statusEl.className = 'log-status'; statusEl.textContent = 'Loading…'; statusEl.style.display = 'block';

        try {
            const res = await fetch('/log_chatbot/set_log', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({log_path: path})
            });
            const data = await res.json();
            if (data.success) {
                showStatus(statusEl, '✔ Log loaded', 'ok');
                setTimeout(() => {
                    if (statusEl.textContent === '✔ Log loaded') {
                        statusEl.className = 'log-status';
                        statusEl.textContent = '';
                        statusEl.style.display = 'none';
                    }
                }, 3000);
                renderSkills(data.skills);
                logLoaded = true;

                // ── Always reset the chat UI when any log is loaded ──
                // The backend always calls reset_conversation() in set_log,
                // so the UI must always match: clear existing messages.
                // `rotated=true` means a DIFFERENT file was loaded — offer undo.
                // `rotated=false` means same file or first load — no undo needed.
                const chatWindow = document.getElementById('chat-window');
                const hasRealMessages = !!chatWindow.querySelector('.msg-row');

                if (data.rotated || hasRealMessages) {
                    // Snapshot current state so the user can undo if this
                    // was a file switch (rotated). For same-file reloads
                    // there is nothing meaningful to undo.
                    const undoCache = data.rotated ? {
                        chatHTML:           chatWindow.innerHTML,
                        feedbackConvId:     window.__feedbackConversationId || '',
                        lastFeedback:       window.__lastFeedback || {},
                        turnContext:        window.__turnContext || {},
                        yamlModified:       window.__yamlModified,
                        previousLogPath:    data.previous_log_path || '',
                    } : null;

                    // Clear everything the previous conversation owned.
                    chatWindow.innerHTML = `
                        <div class="welcome-msg" id="welcome-msg">
                            <div class="big-icon">🔄</div>
                            <strong>New log loaded.</strong><br>
                            Previous chat history was cleared so the agent
                            starts fresh on this log.
                        </div>`;
                    window.__feedbackConversationId = data.new_conversation_id || '';
                    window.__lastFeedback = {};
                    window.__turnContext = {};
                    window.__yamlModified = false;

                    if (data.rotated && undoCache) {
                        showToast({
                            message: '🔄 Detected new log — chat history cleared.',
                            actionLabel: 'Undo (30s)',
                            ttlMs: 30000,
                            onAction: () => {
                                // Visual restore: bring back the previous chat
                                // panel + per-turn caches. The previous
                                // conversation_id is also restored so any
                                // feedback widgets in the restored HTML keep
                                // writing into the right snapshot. The agent's
                                // in-memory state for the old log is gone, but
                                // the user can re-load that log to re-prime.
                                chatWindow.innerHTML = undoCache.chatHTML;
                                window.__feedbackConversationId = undoCache.feedbackConvId;
                                window.__lastFeedback = undoCache.lastFeedback;
                                window.__turnContext  = undoCache.turnContext;
                                window.__yamlModified = undoCache.yamlModified;
                                if (undoCache.previousLogPath) {
                                    document.getElementById('log-path-input').value =
                                        undoCache.previousLogPath;
                                }
                                showToast({
                                    message: '↩️ Restored previous chat view. Reload the old log file to continue chatting.',
                                    ttlMs: 6000,
                                });
                            },
                        });
                    }
                } else {
                    // First-time load with no existing messages — just record the id.
                    window.__feedbackConversationId = data.new_conversation_id || '';
                }

                // NOTE: the sidebar Issue Time is intentionally left EMPTY on
                // load — we no longer auto-fill it with the log's latest
                // timestamp. The user either types a time, or uses the
                // "🪄 AI suggest a time" option offered in the no-issue-time
                // confirm prompt (which infers it from the description + log).
                // Cache JUST the date portion of the log's auto-detected
                // timestamp, so the time-capture popup can pre-fill MM/DD/
                // YYYY when the user types a time-only token (e.g.
                // "17:36:13"). Survives after the sidebar gets cleared.
                //
                // IMPORTANT: only cache the date for logs that actually carry
                // dates. For time-only logs (DDD/tracefmt), the backend still
                // returns issue_time as a full datetime — anchored on
                // "today" or the log's first parseable timestamp — but that
                // date is a placeholder, NOT a real signal. If we cached it,
                // _getReferenceDateForCapture would silently pre-fill that
                // placeholder back into the sidebar's date fields, and
                // getIssueTimeString would emit a full "MM/DD/YYYY-HH:MM:SS"
                // instead of the time-only "HH:MM:SS" that DDD analysis
                // actually wants. Gate on data.log_has_date.
                if (data.issue_time && data.log_has_date !== false) {
                    window.__logAutoDate = _extractDateFromTimeString(data.issue_time);
                } else {
                    window.__logAutoDate = null;
                }
                // Cap the capture-window control at the log's actual span.
                if (typeof data.log_span_minutes === 'number' && data.log_span_minutes > 0) {
                    window.__logSpanMinutes = data.log_span_minutes;
                } else {
                    window.__logSpanMinutes = null;   // unknown → generic cap
                }
                // Log's last timestamp — used by the "Use log's last time"
                // button in the no-issue-time prompt AND surfaced inline on
                // time-only logs (see the it-nodate hint below) so DDD users
                // see the value the moment they upload, not buried behind
                // a fallback modal.
                window.__logLastTime = data.log_last_time || '';
                // Whether this log has dates. Time-only logs (e.g. DDD) make the
                // sidebar's date fields optional (time-only issue time is valid).
                window.__logHasDate = (data.log_has_date !== false);
                if (typeof refreshIssueTimeDateOptional === 'function') refreshIssueTimeDateOptional();
                // Refresh the inline "Log ends at: …" affordance under the
                // no-date hint. Visible only for time-only logs that actually
                // produced a last timestamp.
                if (typeof refreshLogLastTimeHint === 'function') refreshLogLastTimeHint();
                // Auto-fill the sidebar issue time on log load — for BOTH
                // Wi-Fi (dated) and DDD (time-only) logs. Priority chain:
                //   1. LLM-organized issue time(s) from the select-attachments
                //      pre-pass (cached server-side as _issue_ai_quick and
                //      re-aligned to the loaded log's date by
                //      realign_times_to_log). This is the user's "previous
                //      page" issue time — IPS case description → LLM.
                //   2. log_last_time (Wi-Fi: full datetime; DDD: time-only).
                //      Used when the LLM pre-pass produced nothing usable.
                // Always runs (overrides any stale value left from a
                // previously-loaded log) so reloading a different log always
                // shows that log's natural anchor.
                //
                // The CONFIRMATION GATE differs by source on purpose:
                //   * LLM-sourced (path 1) → awaiting-confirm, just like the
                //     auto-run prefill path. The model might pick the wrong
                //     anchor for a vague description, so the user must
                //     explicitly tick before analysis fires.
                //   * log_last_time (path 2) → user-owned immediately. It's
                //     a deterministic value visible in the inline "Log ends
                //     at:" hint, so making the user double-confirm what
                //     they're already looking at is busywork.
                //
                // Skipped entirely when restoring a per-conversation draft:
                // the caller sets the saved draft's Issue Time right after
                // setLog() resolves, and this fire-and-forget fetch would
                // otherwise resolve LATER and clobber it with log_last_time.
                if (!skipIssueTimeAutofill) (async () => {
                    let chosen = '';
                    let fromLlm = false;
                    try {
                        const ctxRes = await fetch('/log_chatbot/get_issue_context');
                        const ctxData = await ctxRes.json();
                        const times = Array.isArray(ctxData && ctxData.issue_times)
                            ? ctxData.issue_times.filter(Boolean)
                            : [];
                        if (times.length > 0) {
                            chosen = String(times[0] || '').trim();
                            fromLlm = !!chosen;
                        }
                    } catch (_e) { /* best effort */ }
                    if (!chosen && data.log_last_time) {
                        chosen = data.log_last_time;
                        fromLlm = false;
                    }
                    // A newer setLog() (or anything that re-loaded a log)
                    // started after us → abandon, we'd be writing a stale time.
                    if (_myLogGen !== window.__setLogGen) return;
                    if (chosen && typeof setIssueTimeFromString === 'function'
                            && setIssueTimeFromString(chosen)) {
                        // Auto-detected time is applied immediately (the old
                        // confirm-gate was removed).
                        if (typeof markIssueTimeUserOwned === 'function') {
                            markIssueTimeUserOwned();
                        }
                    }
                })();
                refreshIssueWindowBounds();
                validateUserInput();
            } else {
                showStatus(statusEl, '✘ ' + data.error, 'err');
            }
        } catch (e) {
            showStatus(statusEl, '✘ Network error: ' + e.message, 'err');
        }
    };

    WifiChatRuntimeStrategy.prototype.tryAutoAnalyzeOnLoad = async function () {
        if (autoAnalyzeTriggered) return;

        const suggestedLog = document.getElementById('log-path-input').value.trim();
        const _urlParams = new URLSearchParams(window.location.search);
        const autoRun = _urlParams.get('auto_run');
        const urlIssueTime = (_urlParams.get('issue_time') || '').trim();
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
            const contextRes = await fetch('/log_chatbot/get_issue_context');
            const contextData = await contextRes.json();
            const question = contextData.description;
            // Prefer the backend-resolved issue_time (attachment_time, with
            // log-latest fallback). attachment_time is kept as a separate
            // signal in case the description still embeds the same string.
            const resolvedTime = (contextData.issue_time || '').trim();
            const attachmentTime = (contextData.attachment_time || '').trim();
            // LLM-organized list of issue time points from the case description
            // (may be several, e.g. "23:16 ... 23:17:09"). Best-first.
            const issueTimes = Array.isArray(contextData.issue_times)
                ? contextData.issue_times.filter(Boolean) : [];
            // Customer-tz annotation map: ``{ "<log_frame_str>": "<customer_str>" }``
            // populated by determine_issue_time_frames on the server. The
            // sidebar surfaces it under the picker once an issue time is
            // applied so the engineer sees what time the customer would
            // have seen on their own wall clock.
            window.__customerAnnotations = contextData.customer_annotations || {};
            window.__customerTzLabel = contextData.customer_tz || '';
            window.__customerIana = contextData.customer_iana || '';
            if (typeof populateCustomerAnnotation === 'function') {
                populateCustomerAnnotation();
            }

            // Pre-fill input but DO NOT auto-send.  The user reviews / edits
            // the issue context (especially the timestamp) and clicks Send.
            setTimeout(() => {
                const input = document.getElementById('user-input');
                const rawQuestion = question || '🔍 Run full multi-skill analysis';

                let descPart = rawQuestion;
                // Priority: LLM-organized multi-time list (from the case
                // description) → URL ?issue_time= → attachment_time →
                // backend-resolved issue_time → regex on description.
                if (issueTimes.length > 0 && _prefillAutoTimes(issueTimes)) {
                    // The backend already returned a CLEAN description (raw
                    // timestamps stripped), so use it verbatim. Auto-detected
                    // time(s) apply immediately (confirm-gate removed).
                    validateUserInput();
                } else {
                    let foundTime = urlIssueTime || attachmentTime || resolvedTime;
                    if (!foundTime) {
                        for (const re of VALID_TIME_REGEXES) {
                            const m = rawQuestion.match(re);
                            if (m) { foundTime = m[0]; break; }
                        }
                    }
                    if (foundTime && setIssueTimeFromString(foundTime)) {
                        descPart = rawQuestion.replace(foundTime, ' ');
                        // Carried over from the previous page — applied
                        // immediately (confirm-gate removed).
                        validateUserInput();
                    } else {
                        // Couldn't find/parse a time — surface the warning so
                        // the user knows to fill it in (or use AI suggest).
                        const warn = document.getElementById('it-unknown-warn');
                        if (warn) warn.style.display = 'block';
                    }
                }
                descPart = trimTrailingTimeConnector(descPart.replace(/\s+/g, ' ').trim());

                if (input) {
                    input.value = descPart;
                    autoResize(input);
                    input.focus();
                    const len = input.value.length;
                    input.setSelectionRange(len, len);
                }
                isAutoFillMode = true;
                const prefillHint = document.getElementById('prefill-hint');
                if (prefillHint) prefillHint.style.display = 'block';
                validateUserInput();
            }, 400);

        } catch (err) {
            autoAnalyzeTriggered = false;
            console.error('[Auto-Analysis] Failed:', err);
        }
    };

    window.createChatRuntimeStrategy =
        (profile) => new WifiChatRuntimeStrategy(profile);
