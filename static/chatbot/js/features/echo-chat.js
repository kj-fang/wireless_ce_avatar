(function () {
    const area = document.querySelector('.chat-area');
    const template = document.getElementById('echo-chat-template');
    const composer = area.querySelector(':scope > .input-row');
    composer.prepend(template.content.cloneNode(true));
    const status = document.createElement('div');
    status.className = 'echo-chat-switch-status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    status.hidden = true;
    area.insertBefore(status, composer);
    const messages = document.getElementById('chat-window');
    const input = document.getElementById('user-input');
    const send = document.getElementById('send-btn');
    const prefillHint = document.getElementById('prefill-hint');
    window.__chatRecipient = 'avatar';
    const history = [];
    const views = {
        avatar: {content: document.createElement('div'), draft: '', scrollTop: 0, prefillDisplay: 'none'},
        echo: {content: document.createElement('div'), draft: '', scrollTop: 0},
    };

    window.refreshChatRecipientAvailability = function () {
        const responding = isChatStreaming() || window.__echoChatBusy || window.ChatbotSnippets?.busy;
        const loadingHistory = window.__avatarHistoryLoading;
        const current = window.__chatRecipient === 'echo' ? 'Echo' : 'Avatar';
        const other = window.__chatRecipient === 'echo' ? 'Avatar' : 'Echo';
        const message = responding
            ? `${current} is responding. Switch to ${other} when the response finishes.`
            : loadingHistory ? 'Loading Avatar history. Please wait before switching.' : '';
        status.textContent = message;
        status.hidden = !message;
        area.querySelectorAll('[data-chat-mode]').forEach(button => {
            const unavailable = !!message && button.dataset.chatMode !== window.__chatRecipient;
            button.setAttribute('aria-disabled', String(unavailable));
            button.title = unavailable ? message : '';
        });
    };

    function selectMode(mode) {
        if (!views[mode] || isChatStreaming() || window.__echoChatBusy || window.ChatbotSnippets?.busy || window.__avatarHistoryLoading) return false;
        if (mode === window.__chatRecipient) return true;
        const previous = views[window.__chatRecipient];
        previous.draft = input.value;
        previous.scrollTop = messages.scrollTop;
        if (prefillHint && window.__chatRecipient === 'avatar') views.avatar.prefillDisplay = prefillHint.style.display;
        while (messages.firstChild) previous.content.appendChild(messages.firstChild);
        while (views[mode].content.firstChild) messages.appendChild(views[mode].content.firstChild);
        window.__chatRecipient = mode;
        input.value = views[mode].draft;
        autoResize(input);
        messages.scrollTop = views[mode].scrollTop;
        if (prefillHint) prefillHint.style.display = mode === 'avatar' ? views.avatar.prefillDisplay : 'none';
        area.querySelectorAll('[data-chat-mode]').forEach(button => {
            button.setAttribute('aria-pressed', String(button.dataset.chatMode === mode));
        });
        validateUserInput();
        document.dispatchEvent(new Event('chatbot:recipient-changed'));
        return true;
    }
    window.selectChatRecipient = selectMode;
    window.getChatRecipientWindow = mode => mode === window.__chatRecipient ? messages : views[mode]?.content;
    window.getChatRecipientDraft = mode => mode === window.__chatRecipient ? input.value : views[mode]?.draft;
    window.setAvatarPrefillHint = visible => {
        views.avatar.prefillDisplay = visible ? 'block' : 'none';
        if (prefillHint && window.__chatRecipient === 'avatar') prefillHint.style.display = views.avatar.prefillDisplay;
    };
    window.setChatRecipientDraft = (mode, draft) => {
        if (!views[mode]) return;
        if (mode === window.__chatRecipient) {
            input.value = draft;
            autoResize(input);
        } else {
            views[mode].draft = draft;
        }
    };
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
        answer.className = 'echo-chat-answer markdown-section';
        setEchoAnswerText(answer, text);
        body.append(source, answer);
        message.appendChild(body);
        row.append(icon, message);
        messages.appendChild(row);
        scrollBottom();
        return {row, body, answer};
    }

    function setEchoAnswerText(answer, text) {
        answer.textContent = text;
        answer.style.whiteSpace = 'pre-wrap';
    }

    window.sendEchoMessage = async function () {
        const message = input.value.trim();
        if (!message || window.__echoChatBusy) return;
        window.__echoChatBusy = true;
        window.refreshChatRecipientAvailability();
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
            setEchoAnswerText(pending.answer, payload.answer);
        } catch (error) {
            setEchoAnswerText(pending.answer, `Echo: ${error.message}`);
            if (!input.value) input.value = message;
        } finally {
            window.__echoChatBusy = false;
            window.refreshChatRecipientAvailability();
            validateUserInput();
            scrollBottom();
        }
    };
})();