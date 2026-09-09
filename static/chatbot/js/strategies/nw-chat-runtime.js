    // NW (Wi-Fi log + sleepstudy) chat runtime.
    //
    // NW has no issue-time picker, no conversation history and no feedback
    // widget, so it overrides the two flows that depend on them. Everything
    // else — setLog / browseLog / copyLogPath / appendReport / appendIncidentTag
    // — comes from chat-runtime-base.js unchanged, which is what keeps the Log
    // File box behaving exactly like BT's and Wi-Fi's (transient "✔ Log loaded"
    // confirmation instead of a permanent green line echoing the full path).
    class NwChatRuntimeStrategy extends ChatRuntimeStrategyBase {}

    NwChatRuntimeStrategy.prototype.sendMessage = async function () {
        const input = document.getElementById('user-input');
        const text = input.value.trim();
        if (!text) return;

        input.value = '';
        input.style.height = 'auto';
        document.getElementById('send-btn').disabled = true;
        setSendBtnStopMode();
        appendUserMsg(text);
        appendTyping();

        const card = createAgentStepRenderer();

        try {
            // Register this stream so the Stop button (core.js) can abort its
            // rendering. The server-side analysis is halted separately via
            // ${this.api}/chat/stop.
            if (window.__streamCtl) { try { window.__streamCtl.abort(); } catch (e) {} }
            const __chatCtl = new AbortController();
            window.__streamCtl = __chatCtl;
            const res = await fetch(`${this.api}/chat`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                signal: __chatCtl.signal,
                body: JSON.stringify({message: text})
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
                    const result = evt.result;
                    if (result && result.type === 'report') {
                        card.collapse();
                        appendReport(result.data, '📊 Analysis Report', result.issue_time || null);
                    } else if (result && result.type === 'partial_report' && result.data) {
                        card.collapse();
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
            if (e.name !== 'AbortError') {
                // AbortError means the user clicked Stop — stopChat() already
                // rendered the notice and reset the button.
                removeTyping();
                appendAssistantText('❌ Network error: ' + e.message);
            }
        } finally {
            removeTyping();
            setSendBtnSendMode();
        }
    };

    NwChatRuntimeStrategy.prototype.tryAutoAnalyzeOnLoad = async function () {
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

            const contextRes = await fetch(`${this.api}/get_issue_context`);
            const contextData = await contextRes.json();
            const question = contextData.description;

            // NW auto-SENDS (the full agents only pre-fill) so the turn is
            // recorded through the normal chat path.
            setTimeout(() => {
                const input = document.getElementById('user-input');
                if (input) input.value = question || '🔍 Run full multi-skill analysis';
                sendMessage();
            }, 400);

        } catch (err) {
            autoAnalyzeTriggered = false;
            console.error('[Auto-Analysis] Failed:', err);
        }
    };

    window.createChatRuntimeStrategy =
        (profile) => new NwChatRuntimeStrategy(profile);
