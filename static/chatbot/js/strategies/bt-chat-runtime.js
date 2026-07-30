    class BtChatRuntimeStrategy {
        constructor(profile) {
            this.profile = Object.freeze({ ...(profile || {}) });
        }
    }

    BtChatRuntimeStrategy.prototype.appendIncidentTag = function (idx, total, timeObj) {
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

    BtChatRuntimeStrategy.prototype.appendReport = function (data, header = '📊 Analysis Report', eventTime = null, turnId = null) {
        removeTyping();
        hideWelcome();

        const actions = (data.recommended_actions || []).map(a => `<li>${escapeHtml(a)}</li>`).join('');
        const skills  = (data.involved_skills || data.skill_findings ? Object.keys(data.skill_findings || {}) : [])
                         .map(s => `<span class="skills-badge">${s}</span>`).join(' ');
        const score   = data.confidence_score || 0;
        const barWidth = Math.max(0, Math.min(100, score));

        const findings = data.skill_findings
            ? Object.entries(data.skill_findings).map(([k, v]) =>
                `<div style="margin-bottom:8px;"><strong style="font-size:0.78rem;color:#2563eb;">${k}</strong><div style="font-size:0.78rem;color:#444;white-space:pre-wrap;">${escapeHtml(String(v).substring(0,500))}${String(v).length>500?'…':''}</div></div>`
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
                 style="cursor:pointer;padding:10px 16px;text-align:center;border-top:1px solid #e0e0e0;color:#2563eb;font-weight:600;font-size:0.85rem;user-select:none;"
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

    BtChatRuntimeStrategy.prototype.browseLog = async function () {
        const btn = document.querySelector('.btn-browse');
        btn.textContent = '⏳';
        btn.disabled = true;
        try {
            const res = await fetch('/bt_chatbot/browse');
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

    BtChatRuntimeStrategy.prototype.copyLogPath = async function (btn) {
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

    BtChatRuntimeStrategy.prototype.sendMessage = async function () {
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
            // Hide the prefill / time hints once the message is actually sent.
            ['prefill-hint','time-hint','no-time-hint','no-desc-hint','no-log-hint']
                .forEach((id) => {
                    const el = document.getElementById(id);
                    if (el) el.style.display = 'none';
                });
        }
        isAutoFillMode = false;  // auto-fill obligation fulfilled
        firstRoundSent = true;   // subsequent rounds skip the strict gate

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
            const res = await fetch('/bt_chatbot/chat', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
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
                    const turnId = evt.turn_id || null;
                    if (evt.conversation_id) window.__feedbackConversationId = evt.conversation_id;
                    if (turnId) {
                        captureTurnContext(turnId, steps);
                    }
                    if (result && result.type === 'report') {
                        if (agentCardId) collapseAgentProcessCard(agentCardId);
                        appendReport(result.data, '📊 Analysis Report', result.issue_time || null, turnId);
                    } else if (result && result.type === 'partial_report' && result.data) {
                        if (agentCardId) collapseAgentProcessCard(agentCardId);
                        appendReport(result.data, '⚠️ Partial Analysis (Step Limit)', result.issue_time || null, turnId);
                    } else if (result && result.type === 'text') appendAssistantText(result.data || '_No response._', turnId);
                    else if (result) appendAssistantText('⚠️ ' + (result.data || 'Unknown response.'), turnId);
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
            // ── Multi-time chain: if the queue still has unprocessed
            // issue times, schedule the next iteration AND keep Send
            // disabled. Re-enabling Send between iterations lets the
            // user click it during the ~250ms gap, which would re-
            // enter sendMessage with isContinuation=true (because
            // __multiTimeContext is still set) — popping the queue
            // early and ignoring whatever they had just typed.
            // We only re-enable Send when the chain is fully drained
            // (or on a regular single-time finish).
            const stillChaining = !!(window.__multiTimeContext
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

    BtChatRuntimeStrategy.prototype.setLog = async function (opts) {
        // When restoring a per-conversation draft / a resumed live session we
        // manage the Issue Time ourselves (from the saved snapshot), so the
        // caller can suppress this function's own issue-time auto-fill —
        // otherwise the rotation branch below would clear the restored time
        // and refill it with the log's last timestamp, losing the user's
        // newly-edited issue time.
        const skipIssueTimeAutofill = !!(opts && opts.skipIssueTimeAutofill);
        const path = document.getElementById('log-path-input').value.trim();
        const statusEl = document.getElementById('log-status');
        if (!path) { showStatus(statusEl, 'Please enter a log file path.', 'err'); return; }

        statusEl.className = 'log-status'; statusEl.textContent = 'Loading…'; statusEl.style.display = 'block';

        try {
            const res = await fetch('/bt_chatbot/set_log', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({log_path: path})
            });
            const data = await res.json();
            if (data.success) {
                // Match the Wi-Fi chatbot: show a brief "loaded" confirmation
                // that auto-dismisses after 3s rather than a persistent line
                // (the full path lives in the input box + the 📋 copy button).
                showStatus(statusEl, '✔ Log loaded', 'ok', 3000);
                renderSkills(data.skills);
                logLoaded = true;

                // ── Auto-reset when switching from one log to another ──
                // Backend signals `rotated=true` when this set_log replaces
                // a DIFFERENT log path. We snapshot the chat UI + per-turn
                // caches so the user can undo the reset for 30 s.
                if (data.rotated) {
                    const undoCache = {
                        chatHTML:           document.getElementById('chat-window').innerHTML,
                        feedbackConvId:     window.__feedbackConversationId || '',
                        lastFeedback:       window.__lastFeedback || {},
                        turnContext:        window.__turnContext || {},
                        yamlModified:       window.__yamlModified,
                        previousLogPath:    data.previous_log_path || '',
                    };
                    // Clear everything the previous conversation owned.
                    document.getElementById('chat-window').innerHTML = `
                        <div class="welcome-msg" id="welcome-msg">
                            starts fresh on this log.
                        </div>`;
                    window.__feedbackConversationId = data.new_conversation_id || '';
                    window.__lastFeedback = {};
                    window.__turnContext = {};
                    window.__yamlModified = false;

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
                            document.getElementById('chat-window').innerHTML = undoCache.chatHTML;
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
                } else if (data.new_conversation_id) {
                    // First-time load (no rotation) — just record the id.
                    window.__feedbackConversationId = data.new_conversation_id;
                }

                // Cache JUST the date portion of the log's auto-detected
                // timestamp, so the time-capture popup can pre-fill MM/DD/
                // YYYY when the user types a time-only token (e.g.
                // "17:36:13"). Survives after the sidebar gets cleared.
                if (data.issue_time) {
                    window.__logAutoDate = _extractDateFromTimeString(data.issue_time);
                }
                // Pre-fill the sidebar Issue Time with the log's resolved time
                // (its last parseable timestamp when no case context supplies
                // one). On ROTATION (a DIFFERENT log replaced the previous one)
                // the old value belonged to the previous log, so wipe it and
                // refresh to the new log's time — clearing first so a stale
                // value can't survive when the new log has no detectable time.
                // On a normal (non-rotated) load we only fill when empty, so we
                // never clobber a value the user typed or that AI suggest /
                // session context already placed. The user can still overwrite
                // it via "🪄 AI suggest a time". Backend already returns the
                // canonical resolved value in data.issue_time.
                //
                // skipIssueTimeAutofill: set when restoring a draft / resumed
                // live session — the caller has ALREADY put the user's saved
                // (possibly newly-edited) issue time into the sidebar, so we
                // must not touch it here at all.
                if (skipIssueTimeAutofill) {
                    // Caller owns the Issue Time — leave the restored value be.
                } else if (data.rotated) {
                    clearAllIssueTimes();
                    if (data.issue_time) setIssueTimeFromString(data.issue_time);
                } else if (data.issue_time && !getIssueTimeString()) {
                    setIssueTimeFromString(data.issue_time);
                }
                // Cap the capture-window control at the log's actual span.
                if (typeof data.log_span_minutes === 'number' && data.log_span_minutes > 0) {
                    window.__logSpanMinutes = data.log_span_minutes;
                } else {
                    window.__logSpanMinutes = null;   // unknown → generic cap
                }
                // Log's last timestamp — used by the "Use log's last time"
                // button in the no-issue-time prompt.
                window.__logLastTime = data.log_last_time || '';
                // Whether this log has dates. Time-only logs (e.g. DDD) make the
                // sidebar's date fields optional (time-only issue time is valid).
                window.__logHasDate = (data.log_has_date !== false);
                if (typeof refreshIssueTimeDateOptional === 'function') refreshIssueTimeDateOptional();
                refreshIssueWindowBounds();
                validateUserInput();

                // ── System Event Log button state ──
                updateEvtButton(data.evtx_path || '');

                // If an issue time is already filled (e.g. from session context),
                // auto-refine it against the closest system event error.
                // Skipped when restoring a draft / resumed live session: the
                // user already committed to a specific time, so we must not
                // pop the refine picker or shift it to a nearby event.
                if (!skipIssueTimeAutofill && getIssueTimeString()) {
                    refineIssueTimeFromEventLog();
                }
            } else {
                showStatus(statusEl, '✘ ' + data.error, 'err');
            }
        } catch (e) {
            showStatus(statusEl, '✘ Network error: ' + e.message, 'err');
        }
    };

    BtChatRuntimeStrategy.prototype.tryAutoAnalyzeOnLoad = async function () {
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
            const contextRes = await fetch('/bt_chatbot/get_issue_context');
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
                    // timestamps stripped), so use it verbatim. These auto-
                    // filled time(s) await the user's confirm tick.
                    issueTimeAwaitingConfirm = true;
                    setIssueTimeConfirmUI(true);
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
                        // Carried over from the previous page → show it, but
                        // require the user to tick the confirm box first.
                        issueTimeAwaitingConfirm = true;
                        setIssueTimeConfirmUI(true);
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

                // Auto-refine: cross-reference the issue time with system event
                // errors and snap to the closest error timestamp (if within 10 min).
                refineIssueTimeFromEventLog();
            }, 400);

        } catch (err) {
            autoAnalyzeTriggered = false;
            console.error('[Auto-Analysis] Failed:', err);
        }
    };

    window.createChatRuntimeStrategy =
        (profile) => new BtChatRuntimeStrategy(profile);
