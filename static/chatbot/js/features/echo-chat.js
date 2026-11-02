(function () {
    const area = document.querySelector('.chat-area');
    const template = document.getElementById('echo-chat-template');
    const composer = area.querySelector(':scope > .input-row');
    composer.prepend(template.content.cloneNode(true));
    const messages = document.getElementById('chat-window');
    const input = document.getElementById('user-input');
    const send = document.getElementById('send-btn');
    window.__chatRecipient = 'avatar';
    const history = [];

    function selectMode(mode) {
        if (isChatStreaming() || window.__echoChatBusy || window.ChatbotSnippets?.busy) return;
        window.__chatRecipient = mode;
        area.querySelectorAll('[data-chat-mode]').forEach(button => {
            button.setAttribute('aria-pressed', String(button.dataset.chatMode === mode));
        });
        validateUserInput();
        document.dispatchEvent(new Event('chatbot:recipient-changed'));
    }
    area.querySelectorAll('[data-chat-mode]').forEach(button => {
        button.addEventListener('click', () => selectMode(button.dataset.chatMode));
    });

    function appendEchoMessage(text) {
        hideWelcome();
        const row = document.createElement('div');
        row.className = 'msg-row assistant';
        row.dataset.source = 'echo';
        const icon = document.createElement('div');
        icon.className = 'avatar-icon';
        icon.textContent = 'E';
        const message = document.createElement('div');
        message.className = 'msg-body';
        const body = document.createElement('div');
        body.className = 'msg-bubble';
        const source = document.createElement('span');
        source.className = 'echo-chat-source';
        source.textContent = 'Echo';
        const answer = document.createElement('div');
        answer.className = 'echo-chat-answer';
        answer.textContent = text;
        body.append(source, answer);
        message.appendChild(body);
        row.append(icon, message);
        messages.appendChild(row);
        scrollBottom();
        return {row, body, answer};
    }

    window.sendEchoMessage = async function () {
        const message = input.value.trim();
        if (!message || window.__echoChatBusy) return;
        window.__echoChatBusy = true;
        send.disabled = true;
        appendUserMsg(message);
        input.value = '';
        autoResize(input);
        const pending = appendEchoMessage('Echo is responding...');
        try {
            const response = await fetch(`${CHATBOT_API}/echo_chat`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({message, history}),
            });
            const payload = await response.json();
            if (!response.ok || !payload.success) throw new Error(payload.error || 'Echo request failed.');
            history.push({role: 'user', content: message}, {role: 'assistant', content: payload.answer});
            history.splice(0, Math.max(0, history.length - 12));
            pending.answer.textContent = payload.answer;
        } catch (error) {
            pending.answer.textContent = `Echo: ${error.message}`;
            if (!input.value) input.value = message;
        } finally {
            window.__echoChatBusy = false;
            validateUserInput();
            scrollBottom();
        }
    };
})();