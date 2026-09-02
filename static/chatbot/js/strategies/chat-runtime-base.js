    // Shared chat-runtime behaviour for the full agents (BT and Wi-Fi).
    //
    // Everything that only differed by API prefix or accent colour lives here;
    // a profile subclass supplies the endpoint via `profile.api` and overrides
    // only the two genuinely domain-specific hooks:
    //
    //   setLog(opts)             — how a freshly loaded log seeds the sidebar
    //   tryAutoAnalyzeOnLoad()   — how the case-number hand-off pre-fills a turn
    //
    // Loaded before the profile strategy script, so `class` bindings declared
    // here are visible to it.
    class ChatRuntimeStrategyBase {
        constructor(profile) {
            this.profile = Object.freeze({ ...(profile || {}) });
            this.api = this.profile.api || '';
        }
    }

    ChatRuntimeStrategyBase.prototype.appendIncidentTag = function (idx, total, timeObj) {
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

    ChatRuntimeStrategyBase.prototype.appendReport = function (data, header = '📊 Analysis Report', eventTime = null, turnId = null) {
        removeTyping();
        hideWelcome();

        // Backends may return these as an array, a newline/semicolon-separated
        // string, or a dict — normalise before rendering.
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
        wrapper.innerHTML = `<div class="avatar-icon">🤖</div><div style="flex:1;">${html}${turnId ? renderFeedbackWidget(turnId) : ''}</div>`;
        document.getElementById('chat-window').appendChild(wrapper);
        scrollBottom();

        // Auto-hide details after full render (brief flash so user sees content exists)
        if (detailsHtml) {
            setTimeout(() => toggleReportDetails(toggleId, detailsId), 600);
        }
    };

    ChatRuntimeStrategyBase.prototype.browseLog = async function () {
        const btn = document.querySelector('.btn-browse');
        btn.textContent = '⏳';
        btn.disabled = true;
        try {
            const res = await fetch(`${this.api}/browse`);
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

    ChatRuntimeStrategyBase.prototype.copyLogPath = async function (btn) {
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

    ChatRuntimeStrategyBase.prototype.sendMessage = async function () {
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
            if (window.__draftByConv && typeof _draftKey === 'function') {
                delete window.__draftByConv[_draftKey()];
            }
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
        // Flip the Send button into its red Stop state for this stream.
        setSendBtnStopMode();
        // Only show the user's typed message ONCE — at the start of the
        // very first iteration. Continuations re-use the same message.
        if (!isContinuation) appendUserMsg(text);
        appendTyping();

        const card = createAgentStepRenderer();
        // Set when the stream is aborted — either the user clicked Stop, or
        // they switched to another conversation mid-stream. Both mean "stop
        // rendering and don't chain the next queued issue time".
        let aborted = false;

        try {
            // Register this stream so the Stop button (core.js) can abort its
            // rendering. The server-side analysis is halted separately via
            // <api>/chat/stop; switching conversations aborts it the same way,
            // leaving the server job running to be re-attached from History.
            if (window.__streamCtl) { try { window.__streamCtl.abort(); } catch (e) {} }
            const __chatCtl = new AbortController();
            window.__streamCtl = __chatCtl;
            const res = await fetch(`${this.api}/chat`, {
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
                // DOM and make typing in the new session lag. It must also not
                // rebind the global conversation id — that would corrupt the
                // current draft's context.
                if (window.__streamCtl !== __chatCtl) return;

                if (evt.type === 'step') {
                    card.append(evt.step);
                } else if (evt.type === 'done') {
                    removeTyping();
                    const result = evt.result;
                    const turnId = evt.turn_id || null;
                    if (evt.conversation_id) window.__feedbackConversationId = evt.conversation_id;
                    if (turnId) {
                        captureTurnContext(turnId, card.steps);
                    }
                    // Keep the history sidebar current — this turn was just
                    // persisted server-side, so refresh the list.
                    if (typeof loadHistoryList === 'function') loadHistoryList();
                    if (result && result.type === 'report') {
                        card.collapse();
                        appendReport(result.data, '📊 Analysis Report', result.issue_time || null, turnId);
                    } else if (result && result.type === 'partial_report' && result.data) {
                        card.collapse();
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
            if (e.name === 'AbortError') {
                // Stop button (stopChat() already rendered the notice and the
                // backend is halting) or a conversation switch (server job
                // keeps running). Either way there's nothing to show here.
                aborted = true;
            } else {
                removeTyping();
                appendAssistantText('❌ Network error: ' + e.message);
            }
        } finally {
            removeTyping();
            // An abort cancels the whole (possibly multi-incident) send: don't
            // schedule the next iteration, and leave the sidebar issue times
            // alone so the user can re-send them.
            if (aborted) {
                window.__multiTimeContext = null;
                setSendBtnSendMode();
                return;
            }
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
                setSendBtnSendMode();
            }
        }
    };

    // Reset the chat UI after set_log, with a 30 s undo.
    //
    // `rotated=true` means a DIFFERENT log replaced the previous one, so the
    // previous conversation is gone either way — always reset and offer undo.
    // Profiles whose backend resets the conversation on EVERY set_log
    // (history_reset_before_set_log) additionally clear a same-file reload so
    // the UI can't show messages the agent no longer remembers; there is
    // nothing meaningful to undo in that case.
    ChatRuntimeStrategyBase.prototype.resetChatForNewLog = function (data) {
        const chatWindow = document.getElementById('chat-window');
        const alsoOnReload = !!this.profile.history_reset_before_set_log
                             && !!chatWindow.querySelector('.msg-row');

        if (!data.rotated && !alsoOnReload) {
            // First-time load (no rotation) — just record the id.
            if (data.new_conversation_id) {
                window.__feedbackConversationId = data.new_conversation_id;
            }
            return;
        }

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

        if (!undoCache) return;

        showToast({
            message: '🔄 Detected new log — chat history cleared.',
            actionLabel: 'Undo (30s)',
            ttlMs: 30000,
            onAction: () => {
                // Visual restore: bring back the previous chat panel + per-turn
                // caches. The previous conversation_id is also restored so any
                // feedback widgets in the restored HTML keep writing into the
                // right snapshot. The agent's in-memory state for the old log
                // is gone, but the user can re-load that log to re-prime.
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
    };

    // ---- setLog / auto-analyze hooks --------------------------------------

    /**
     * Cache JUST the date portion of the log's auto-detected timestamp, so the
     * time-capture popup can pre-fill MM/DD/YYYY when the user types a
     * time-only token (e.g. "17:36:13"). Survives after the sidebar is cleared.
     *
     * Gated on log_has_date: for time-only logs (DDD/tracefmt) the backend
     * still returns issue_time as a full datetime — anchored on "today" or the
     * log's first parseable timestamp — but that date is a placeholder, not a
     * signal. Caching it would let _getReferenceDateForCapture silently push
     * the placeholder back into the sidebar's date fields, making
     * getIssueTimeString emit "MM/DD/YYYY-HH:MM:SS" where the analysis wants a
     * bare "HH:MM:SS".
     */
    ChatRuntimeStrategyBase.prototype._cacheLogAutoDate = function (data) {
        // _extractDateFromTimeString lives in issue-time-controller.js, which a
        // profile without an issue-time picker does not load. Such a backend
        // also never returns issue_time, so the guard is belt-and-braces.
        window.__logAutoDate = (data.issue_time && data.log_has_date !== false
                                && typeof _extractDateFromTimeString === 'function')
            ? _extractDateFromTimeString(data.issue_time)
            : null;
    };

    ChatRuntimeStrategyBase.prototype._afterDateOptionalRefresh = function () {};
    ChatRuntimeStrategyBase.prototype._applyIssueTimeOnLoad = function (data, ctx) {};
    ChatRuntimeStrategyBase.prototype._onIssueContextLoaded = function (contextData) {};
    ChatRuntimeStrategyBase.prototype._onAutoIssueTimeApplied = function () {};
    ChatRuntimeStrategyBase.prototype._afterAutoPrefill = function () {};

    ChatRuntimeStrategyBase.prototype.setLog = async function (opts) {
        // When restoring a per-conversation draft / a resumed live session the
        // caller manages the Issue Time itself (from the saved snapshot), so it
        // can suppress this function's own issue-time auto-fill — otherwise the
        // restored value would be clobbered by the log's own anchor.
        const skipIssueTimeAutofill = !!(opts && opts.skipIssueTimeAutofill);
        const path = document.getElementById('log-path-input').value.trim();
        const statusEl = document.getElementById('log-status');
        if (!path) { showStatus(statusEl, 'Please enter a log file path.', 'err'); return; }
        // Generation token so a previous setLog()'s still-pending async
        // issue-time auto-fill can't resolve late and overwrite a newer load's
        // (or a restored draft's) issue time.
        const logGen = (window.__setLogGen = (window.__setLogGen || 0) + 1);

        statusEl.className = 'log-status'; statusEl.textContent = 'Loading…'; statusEl.style.display = 'block';

        try {
            const res = await fetch(`${this.api}/set_log`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({log_path: path})
            });
            const data = await res.json();
            if (!data.success) {
                showStatus(statusEl, '✘ ' + data.error, 'err');
                return;
            }
            // Brief "loaded" confirmation that auto-dismisses after 3s rather
            // than a persistent line (the full path lives in the input box +
            // the 📋 copy button).
            showStatus(statusEl, '✔ Log loaded', 'ok', 3000);
            renderSkills(data.skills);
            logLoaded = true;
            // Enables the (collapsed) System Event Log panel when the capture
            // folder ships an .evt / .evtx next to the log.
            if (typeof updateEvtButton === 'function') {
                updateEvtButton(data.evtx_path || '');
            }

            // Auto-reset (with a 30 s undo) when this set_log replaced a
            // DIFFERENT log path.
            this.resetChatForNewLog(data);
            this._cacheLogAutoDate(data);

            // Cap the capture-window control at the log's actual span.
            window.__logSpanMinutes =
                (typeof data.log_span_minutes === 'number' && data.log_span_minutes > 0)
                    ? data.log_span_minutes
                    : null;   // unknown → generic cap
            // Log's last timestamp — used by the "Use log's last time" button
            // in the no-issue-time prompt.
            window.__logLastTime = data.log_last_time || '';
            // Whether this log has dates. Time-only logs (e.g. DDD) make the
            // sidebar's date fields optional (time-only issue time is valid).
            window.__logHasDate = (data.log_has_date !== false);
            if (typeof refreshIssueTimeDateOptional === 'function') refreshIssueTimeDateOptional();
            this._afterDateOptionalRefresh();

            this._applyIssueTimeOnLoad(data, { skipIssueTimeAutofill, logGen });

            // Issue-time-only follow-ups. A profile with features.issue_time
            // off (NW) never loads issue-time-controller.js, so these are
            // genuinely absent there rather than merely optional.
            if (typeof refreshIssueWindowBounds === 'function') refreshIssueWindowBounds();
            if (typeof validateUserInput === 'function') validateUserInput();
        } catch (e) {
            showStatus(statusEl, '✘ Network error: ' + e.message, 'err');
        }
    };

    ChatRuntimeStrategyBase.prototype.tryAutoAnalyzeOnLoad = async function () {
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
            const contextRes = await fetch(`${this.api}/get_issue_context`);
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
            this._onIssueContextLoaded(contextData);

            // Pre-fill input but DO NOT auto-send.  The user reviews / edits
            // the issue context (especially the timestamp) and clicks Send.
            setTimeout(() => {
                const input = document.getElementById('user-input');
                const rawQuestion = question || '🔍 Run full multi-skill analysis';

                let descPart = rawQuestion;
                let filledSourceLabel = '';
                // The backend validated the carried-over time against the log
                // the user actually selected. When it says the two disagree,
                // no lower-priority fallback may quietly fill the field back
                // in — that is what used to anchor an analysis on a timestamp
                // belonging to a different capture.
                const issueTimeBlocked = !!contextData.issue_time_blocked;
                // Priority: LLM-organized multi-time list (from the case
                // description) → URL ?issue_time= → attachment_time →
                // backend-resolved issue_time → regex on description.
                if (issueTimeBlocked) {
                    clearAllIssueTimes();
                    const warn = document.getElementById('it-unknown-warn');
                    if (warn) {
                        warn.textContent = '⚠ ' + (contextData.issue_time_warning
                            || 'The auto-detected issue time is outside this log. Please confirm it.');
                        warn.style.display = 'block';
                    }
                } else if (issueTimes.length > 0 && _prefillAutoTimes(issueTimes)) {
                    // The backend already returned a CLEAN description (raw
                    // timestamps stripped), so use it verbatim.
                    filledSourceLabel = ISSUE_TIME_SOURCE.DESCRIPTION;
                    this._onAutoIssueTimeApplied();
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
                        if (attachmentTime && foundTime === attachmentTime) {
                            filledSourceLabel = ISSUE_TIME_SOURCE.ATTACHMENT;
                        }
                        this._onAutoIssueTimeApplied();
                        validateUserInput();
                    } else {
                        // Couldn't find/parse a time — surface the warning so
                        // the user knows to fill it in (or use AI suggest).
                        const warn = document.getElementById('it-unknown-warn');
                        if (warn) warn.style.display = 'block';
                    }
                }
                _setIssueTimeSourceTag(filledSourceLabel);
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
                this._afterAutoPrefill();
            }, 400);

        } catch (err) {
            autoAnalyzeTriggered = false;
            console.error('[Auto-Analysis] Failed:', err);
        }
    };
