    // NW (Sleep Study) chat runtime.
    //
    // NW has no issue-time picker and no feedback widget, so it overrides the
    // send flow that depends on them. Follow-up questions are asked about the
    // loaded Sleep Study report; each turn is saved to NW's History, and the
    // conversation id comes back on 'done' so the History list can mark it.
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
                    noteNwConversation(result);
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

    window.createChatRuntimeStrategy =
        (profile) => new NwChatRuntimeStrategy(profile);
