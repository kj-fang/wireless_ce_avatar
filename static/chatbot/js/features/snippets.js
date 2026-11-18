(function () {
    const area = document.querySelector('.chat-area');
    const composer = area.querySelector(':scope > .input-row');
    const banner = document.createElement('div');
    banner.className = 'snippet-banner';
    banner.hidden = true;
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    const preview = document.createElement('pre');
    details.append(summary, preview);
    const clear = makeKeyLogButton('x', 'End fragment explanation', 'x');
    banner.append(details, clear);
    area.insertBefore(banner, composer);
    let selected = null;
    let histories = {avatar: [], echo: []};
    let controller = null;
    let pendingAnswer = null;
    let generation = 0;
    let busy = false;

    function recipient() { return window.__chatRecipient === 'echo' ? 'echo' : 'avatar'; }
    function setAnswerText(el, text) {
        el.textContent = text;
        el.style.whiteSpace = 'pre-wrap';
    }
    function renderSkillMetadata(container, target, payload) {
        container.replaceChildren();
        container.hidden = target !== 'avatar';
        if (container.hidden) return;

        const status = payload.selection_status;
        if (status === 'applied' && Array.isArray(payload.applied_skills) && payload.applied_skills.length) {
            const heading = document.createElement('span');
            heading.className = 'snippet-skill-heading';
            heading.textContent = 'Skills applied';
            const list = document.createElement('ul');
            payload.applied_skills.forEach(skill => {
                if (!skill || typeof skill.name !== 'string' || !skill.name) return;
                const item = document.createElement('li');
                const matchedKeywords = Array.isArray(skill.matched_keywords)
                    ? skill.matched_keywords.filter(keyword => typeof keyword === 'string' && keyword)
                    : [];
                const evidence = matchedKeywords.length
                    ? `Matched keywords: ${matchedKeywords.join(', ')}`
                    : '';
                item.textContent = [
                    skill.name,
                    evidence,
                    typeof skill.reason === 'string' ? skill.reason : '',
                ].filter(Boolean).join(' | ');
                list.appendChild(item);
            });
            if (list.childElementCount) container.append(heading, list);
            else container.textContent = 'No matching configured skill; general knowledge used.';
        } else if (status === 'selection_failed') {
            container.textContent = 'Skill selection failed; general knowledge used.';
        } else if (status === 'no_match') {
            container.textContent = 'No matching configured skill; general knowledge used.';
        } else {
            container.hidden = true;
        }
        if (Array.isArray(payload.tool_results) && payload.tool_results.length) {
            container.hidden = false;
            const heading = document.createElement('span');
            heading.className = 'snippet-skill-heading';
            heading.textContent = 'Tools';
            const list = document.createElement('ul');
            payload.tool_results.forEach(tool => {
                if (!tool || typeof tool.name !== 'string' || typeof tool.status !== 'string') return;
                const item = document.createElement('li');
                item.textContent = [tool.name, tool.status, typeof tool.detail === 'string' ? tool.detail : ''].filter(Boolean).join(' | ');
                list.appendChild(item);
            });
            if (list.childElementCount) container.append(heading, list);
        }
    }
    function renderTokenUsage(container, target, usage) {
        container.hidden = target !== 'avatar';
        if (container.hidden) return;
        if (!usage || typeof usage !== 'object') {
            container.textContent = 'Token usage unavailable';
            return;
        }
        const format = value => Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString() : 'N/A';
        container.textContent = `Tokens | Input: ${format(usage.input_tokens)} | Output: ${format(usage.output_tokens)} | Total: ${format(usage.total_tokens)}`;
    }
    function formatRows(rows) {
        const lines = [];
        let previous = 0;
        for (const row of rows) {
            if (previous && row.lineNumber !== previous + 1) lines.push('[hidden or unselected lines omitted]');
            lines.push(`[L${row.lineNumber}] ${row.text.replace(/[\r\n]+$/, '')}`);
            previous = row.lineNumber;
        }
        return lines.join('\n');
    }
    function refresh() {
        banner.hidden = !selected;
        if (selected) {
            summary.textContent = `Fragment explanation (${recipient() === 'echo' ? 'Echo' : 'Avatar'}) | ${selected.name} | L${selected.rows[0].lineNumber}-L${selected.rows[selected.rows.length - 1].lineNumber} | ${selected.rows.length} lines`;
            preview.textContent = formatRows(selected.rows);
        }
        if (window.refreshChatRecipientAvailability) window.refreshChatRecipientAvailability();
        else document.querySelectorAll('[data-chat-mode]').forEach(button => { button.disabled = busy; });
        if (typeof validateUserInput === 'function') validateUserInput();
    }
    clear.addEventListener('click', () => {
        generation++;
        if (controller) controller.abort();
        if (pendingAnswer?.isConnected) setAnswerText(pendingAnswer, 'Fragment explanation cancelled.');
        selected = null;
        histories = {avatar: [], echo: []};
        refresh();
    });
    document.addEventListener('chatbot:recipient-changed', refresh);
    document.addEventListener('chatbot:context-changed', () => {
        generation++;
        if (controller) controller.abort();
        selected = null;
        histories = {avatar: [], echo: []};
        refresh();
    });
    window.ChatbotSnippets = {
        get active() { return !!selected; },
        get busy() { return busy; },
        select(snippet) {
            if (!snippet.rows.length || snippet.rows.length > 200 || snippet.rows.reduce((total, row) => total + row.text.length, 0) > 8000) return;
            selected = {name: snippet.name, rows: snippet.rows.map(row => ({lineNumber: row.lineNumber, text: row.text}))};
            histories = {avatar: [], echo: []};
            refresh();
            document.getElementById('user-input').focus();
        },
        async send() {
            if (!selected || busy || window.__echoChatBusy || isChatStreaming()) return;
            const input = document.getElementById('user-input');
            const question = input.value.trim() || 'Please explain this log fragment.';
            const target = recipient();
            const snapshot = selected;
            const history = histories[target];
            const contextGeneration = generation;
            busy = true;
            refresh();
            appendUserMsg(question);
            const userRow = document.getElementById('chat-window').lastElementChild;
            const attachment = document.createElement('details');
            attachment.className = 'snippet-message-attachment';
            const heading = document.createElement('summary');
            heading.textContent = `${target === 'echo' ? 'Echo' : 'Avatar'} | ${snapshot.name} | ${snapshot.rows.length} selected lines`;
            const source = document.createElement('pre');
            source.textContent = formatRows(snapshot.rows);
            attachment.append(heading, source);
            userRow.querySelector('.msg-bubble').appendChild(attachment);
            input.value = '';
            autoResize(input);
            const row = document.createElement('div');
            row.className = 'msg-row assistant';
            row.dataset.source = target;
            row.dataset.mode = 'snippet';
            const body = document.createElement('div');
            body.className = 'msg-bubble snippet-answer';
            const label = document.createElement('strong');
            label.textContent = `${target === 'echo' ? 'Echo' : 'Avatar'} | Fragment explanation`;
            const skillMetadata = document.createElement('div');
            skillMetadata.className = 'snippet-skill-status';
            skillMetadata.setAttribute('role', 'status');
            skillMetadata.setAttribute('aria-live', 'polite');
            skillMetadata.hidden = true;
            const answer = document.createElement('div');
            answer.className = 'markdown-section';
            const tokenUsage = document.createElement('div');
            tokenUsage.className = 'snippet-token-usage';
            tokenUsage.hidden = true;
            pendingAnswer = answer;
            setAnswerText(answer, 'Explaining selected lines...');
            body.append(label, skillMetadata, answer, tokenUsage);
            row.appendChild(body);
            document.getElementById('chat-window').appendChild(row);
            scrollBottom();
            controller = new AbortController();
            try {
                const response = await fetch(`${CHATBOT_API}/explain_snippet`, {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({recipient: target, message: question, snippet: snapshot, history}),
                    signal: controller.signal,
                });
                const payload = await response.json();
                if (!response.ok || !payload.success) throw new Error(payload.error || 'Fragment explanation failed.');
                if (contextGeneration !== generation) return;
                renderSkillMetadata(skillMetadata, target, payload);
                setAnswerText(answer, payload.answer);
                renderTokenUsage(tokenUsage, target, payload.token_usage);
                history.push({role: 'user', content: question}, {role: 'assistant', content: payload.answer});
            } catch (error) {
                if (contextGeneration !== generation) return;
                setAnswerText(answer, error.name === 'AbortError' ? 'Fragment explanation cancelled.' : error.message);
                if (!input.value) input.value = question;
            } finally {
                busy = false;
                controller = null;
                pendingAnswer = null;
                refresh();
                if (row.isConnected) scrollBottom();
            }
        },
    };
})();