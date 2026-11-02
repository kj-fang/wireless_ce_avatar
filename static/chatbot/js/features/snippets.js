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
        document.querySelectorAll('[data-chat-mode]').forEach(button => { button.disabled = busy; });
        if (typeof validateUserInput === 'function') validateUserInput();
    }
    clear.addEventListener('click', () => {
        generation++;
        if (controller) controller.abort();
        if (pendingAnswer?.isConnected) pendingAnswer.textContent = 'Fragment explanation cancelled.';
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
            const answer = document.createElement('div');
            pendingAnswer = answer;
            answer.textContent = 'Explaining selected lines...';
            body.append(label, answer);
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
                answer.textContent = payload.answer;
                history.push({role: 'user', content: question}, {role: 'assistant', content: payload.answer});
            } catch (error) {
                if (contextGeneration !== generation) return;
                answer.textContent = error.name === 'AbortError' ? 'Fragment explanation cancelled.' : error.message;
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