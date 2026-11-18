/* Beacon assistant page.

   One conversation at a time on screen. A turn is POSTed to /assistant/chat
   and comes back as Server-Sent Events: any number of "step" frames (what the
   assistant is doing), then one "done" (the answer) or "error".

   Everything the assistant says can contain text that came from somebody
   else's mail or from a customer's comment, so nothing reaches the page as
   HTML unless it has been through safeHtml() below. */
(function () {
    'use strict';

    const API = '/assistant';
    const $ = (id) => document.getElementById(id);
    const chatWindow = $('chat-window');
    const input = $('user-input');
    const sendBtn = $('send-btn');

    const state = {
        conversationId: '',
        busy: false,
        stream: null,          // AbortController of the stream being read
    };

    // ── Safe rendering ────────────────────────────────────────────────────

    function escapeHtml(text) {
        return String(text == null ? '' : text)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    const ALLOWED_TAGS = new Set(['P', 'BR', 'STRONG', 'B', 'EM', 'I', 'DEL', 'S', 'CODE', 'PRE',
        'UL', 'OL', 'LI', 'TABLE', 'THEAD', 'TBODY', 'TR', 'TH', 'TD', 'H1', 'H2', 'H3', 'H4',
        'H5', 'H6', 'BLOCKQUOTE', 'HR', 'A', 'SPAN', 'DIV']);
    const DROPPED_WITH_CONTENT = new Set(['SCRIPT', 'STYLE', 'IFRAME', 'OBJECT', 'EMBED', 'TEMPLATE',
        'NOSCRIPT', 'SVG', 'MATH', 'TEXTAREA', 'SELECT', 'TITLE']);

    /* Markup produced by the Markdown renderer, reduced to an allowlist: known
       tags only, no attributes except a checked link target. The markup is
       parsed inside a <template>, where nothing runs and nothing loads. */
    function safeHtml(html) {
        const tpl = document.createElement('template');
        tpl.innerHTML = String(html || '');
        const clean = (node) => {
            for (const child of Array.from(node.childNodes)) {
                if (child.nodeType === Node.TEXT_NODE) continue;
                if (child.nodeType !== Node.ELEMENT_NODE) { child.remove(); continue; }
                const tag = child.tagName.toUpperCase();
                if (DROPPED_WITH_CONTENT.has(tag)) { child.remove(); continue; }
                if (!ALLOWED_TAGS.has(tag)) {
                    // Unknown element: keep what it says, drop what it is.
                    clean(child);
                    child.replaceWith(...Array.from(child.childNodes));
                    continue;
                }
                let href = '';
                if (tag === 'A') href = child.getAttribute('href') || '';
                for (const attr of Array.from(child.attributes)) child.removeAttribute(attr.name);
                if (tag === 'A') {
                    if (/^(https?:|mailto:)/i.test(href.trim())) {
                        child.setAttribute('href', href.trim());
                        child.setAttribute('target', '_blank');
                        child.setAttribute('rel', 'noopener noreferrer');
                    }
                }
                clean(child);
            }
        };
        clean(tpl.content);
        // Wide tables scroll inside the bubble instead of stretching it.
        for (const table of Array.from(tpl.content.querySelectorAll('table'))) {
            const wrap = document.createElement('div');
            wrap.className = 'as-table-wrap';
            table.replaceWith(wrap);
            wrap.appendChild(table);
        }
        const holder = document.createElement('div');
        holder.appendChild(tpl.content);
        return holder.innerHTML;
    }

    function markdownInto(el, text) {
        const raw = String(text || '');
        if (window.marked && typeof window.marked.parse === 'function') {
            try {
                el.innerHTML = safeHtml(window.marked.parse(raw, { gfm: true, breaks: true }));
                return;
            } catch (e) { /* fall through to plain text */ }
        }
        el.classList.add('plain');
        el.textContent = raw;
    }

    function inlineMarkdown(text) {
        // Step lines: only `code` spans are formatted; the rest is plain text.
        return escapeHtml(text).replace(/`([^`]+)`/g, '<code>$1</code>');
    }

    // ── Chat window ───────────────────────────────────────────────────────

    function scrollDown() { chatWindow.scrollTop = chatWindow.scrollHeight; }

    function clearChat() {
        chatWindow.innerHTML = '';
        const welcome = document.createElement('div');
        welcome.className = 'welcome-msg';
        welcome.id = 'welcome-msg';
        welcome.innerHTML = '<div class="big-icon">🛰️</div><strong>Beacon Assistant</strong><br>' +
            'Ask about your IPS cases, today\'s mail, or a technical question for Echo.<br>' +
            'I look things up and start analyses. I never post to IPS and never send mail.';
        chatWindow.appendChild(welcome);
    }

    function hideWelcome() { const w = $('welcome-msg'); if (w) w.remove(); }

    function addUserRow(text) {
        hideWelcome();
        const row = document.createElement('div');
        row.className = 'msg-row user';
        row.innerHTML = '<div class="avatar-icon">🧑</div><div class="msg-bubble"></div>';
        row.querySelector('.msg-bubble').textContent = text;
        chatWindow.appendChild(row);
        scrollDown();
    }

    /* The assistant's side of one turn: a steps card that fills while the
       turn runs, then the answer. Returns the handles the stream writes to. */
    function addAssistantRow() {
        hideWelcome();
        const row = document.createElement('div');
        row.className = 'msg-row assistant';
        row.innerHTML =
            '<div class="avatar-icon">🛰️</div>' +
            '<div class="msg-body">' +
            '  <div class="agent-process-card" hidden>' +
            '    <div class="agent-process-header"><span class="as-steps-title">Working…</span>' +
            '      <span class="toggle-icon">▾</span></div>' +
            '    <div class="agent-process-body"></div>' +
            '  </div>' +
            '  <div class="typing-indicator"><span class="dot"></span><span class="dot"></span>' +
            '    <span class="dot"></span><span class="as-typing-text">Thinking…</span></div>' +
            '</div>';
        chatWindow.appendChild(row);
        const card = row.querySelector('.agent-process-card');
        const header = row.querySelector('.agent-process-header');
        const body = row.querySelector('.agent-process-body');
        header.addEventListener('click', () => {
            header.classList.toggle('collapsed');
            body.classList.toggle('hidden');
        });
        scrollDown();
        return { row, card, header, body, bodyEl: row.querySelector('.msg-body'), steps: 0, calls: 0 };
    }

    function stepClass(content) {
        if (content.startsWith('🧠')) return 'as-step-head';
        if (content.startsWith('⚠️')) return 'as-step-error';
        if (content.startsWith('⏳')) return 'as-step-wait';
        return '';
    }

    function addStep(turn, step) {
        const content = String((step && step.content) || '');
        if (!content) return;
        turn.card.hidden = false;
        const line = document.createElement('div');
        line.className = 'agent-step ' + stepClass(content);
        const seconds = typeof step.ts_ms === 'number' ? (step.ts_ms / 1000).toFixed(1) + ' s' : '';
        line.innerHTML = '<div class="agent-step-content">' + inlineMarkdown(content) + '</div>' +
            '<div class="as-step-time">' + escapeHtml(seconds) + '</div>';
        turn.body.appendChild(line);
        turn.body.scrollTop = turn.body.scrollHeight;
        turn.steps += 1;
        if (content.startsWith('🔧')) turn.calls += 1;
        const typing = turn.row.querySelector('.as-typing-text');
        if (typing) {
            typing.textContent = content.startsWith('⏳') ? content.replace(/^⏳\s*/, '')
                : content.startsWith('🔧') ? 'Looking things up…' : 'Thinking…';
        }
        scrollDown();
    }

    function finishSteps(turn, collapsed) {
        const title = turn.row.querySelector('.as-steps-title');
        if (title) {
            title.textContent = turn.calls
                ? 'Looked up ' + turn.calls + ' thing' + (turn.calls === 1 ? '' : 's')
                : 'Steps';
        }
        if (collapsed) {
            turn.header.classList.add('collapsed');
            turn.body.classList.add('hidden');
        }
        if (!turn.steps) turn.card.hidden = true;
    }

    function removeTyping(turn) {
        const t = turn.row.querySelector('.typing-indicator');
        if (t) t.remove();
    }

    function showAnswer(turn, result) {
        removeTyping(turn);
        finishSteps(turn, true);
        const data = result || {};
        if (data.internal) {
            const badge = document.createElement('div');
            badge.className = 'as-internal-badge';
            badge.textContent = 'INTERNAL · contains Echo content · do not forward to customers';
            turn.bodyEl.appendChild(badge);
        }
        const bubble = document.createElement('div');
        bubble.className = 'msg-bubble' + (data.internal ? ' as-internal' : '');
        const answer = document.createElement('div');
        answer.className = 'as-answer';
        markdownInto(answer, typeof data.data === 'string' ? data.data : JSON.stringify(data.data || ''));
        bubble.appendChild(answer);
        turn.bodyEl.appendChild(bubble);
        if (Array.isArray(data.pending_actions) && data.pending_actions.length) {
            turn.bodyEl.appendChild(renderActions(data.pending_actions));
        }
        scrollDown();
    }

    function showError(turn, text) {
        removeTyping(turn);
        finishSteps(turn, false);
        const bubble = document.createElement('div');
        bubble.className = 'msg-bubble as-error-bubble';
        bubble.textContent = '⚠ ' + (text || 'Something went wrong.');
        turn.bodyEl.appendChild(bubble);
        scrollDown();
    }

    // Actions the assistant proposed but only the user may start (stage 2
    // fills this in; the server decides what an action is, the page only
    // sends back its id).
    function renderActions(actions) {
        const box = document.createElement('div');
        box.className = 'as-actions';
        for (const action of actions) {
            const line = document.createElement('div');
            line.className = 'as-action';
            const text = document.createElement('div');
            text.className = 'as-action-text';
            text.textContent = action.label || action.kind || 'Action';
            const yes = document.createElement('button');
            yes.className = 'confirm';
            yes.textContent = action.confirm_label || 'Confirm';
            const no = document.createElement('button');
            no.className = 'dismiss';
            no.textContent = 'Dismiss';
            const settle = (message) => {
                yes.disabled = true; no.disabled = true;
                if (message) text.textContent = message;
            };
            yes.addEventListener('click', async () => {
                yes.disabled = true; no.disabled = true;
                try {
                    const res = await postJson('/actions/confirm', { action_id: action.id });
                    settle(res.message || (res.success ? 'Done.' : (res.error || 'That did not work.')));
                    loadTasks();
                } catch (e) {
                    settle('Could not start: ' + e.message);
                }
            });
            no.addEventListener('click', () => {
                postJson('/actions/dismiss', { action_id: action.id }).catch(() => {});
                settle('Dismissed.');
            });
            line.append(text, yes, no);
            box.appendChild(line);
        }
        return box;
    }

    // ── Server calls ──────────────────────────────────────────────────────

    async function postJson(path, body) {
        const res = await fetch(API + path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
        let data = {};
        try { data = await res.json(); } catch (e) { /* not JSON */ }
        if (!res.ok && !data.error) data.error = 'HTTP ' + res.status;
        return data;
    }

    async function getJson(path) {
        const res = await fetch(API + path);
        return res.json();
    }

    /* Read one Server-Sent-Events response to its end, handing every data
       frame to onEvent. Comment frames (": keep-alive") are skipped. */
    async function readEvents(response, onEvent) {
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let cut;
            while ((cut = buffer.indexOf('\n\n')) >= 0) {
                const frame = buffer.slice(0, cut);
                buffer = buffer.slice(cut + 2);
                const data = frame.split('\n').filter((l) => l.startsWith('data:'))
                    .map((l) => l.slice(5).replace(/^ /, '')).join('\n');
                if (!data) continue;
                let event;
                try { event = JSON.parse(data); } catch (e) { continue; }
                onEvent(event);
            }
        }
    }

    // ── A turn ────────────────────────────────────────────────────────────

    function setBusy(busy) {
        state.busy = busy;
        sendBtn.textContent = busy ? 'Stop ■' : 'Send ↑';
        sendBtn.classList.toggle('stopping', busy);
        input.disabled = false;
    }

    /* Follow a stream (a new turn's, or a running turn re-attached from
       History) and draw it into `turn`. */
    async function follow(response, turn) {
        let finished = false;
        await readEvents(response, (event) => {
            if (event.type === 'step') {
                addStep(turn, event.step);
            } else if (event.type === 'done') {
                finished = true;
                if (event.conversation_id) state.conversationId = event.conversation_id;
                showAnswer(turn, event.result);
            } else if (event.type === 'error') {
                finished = true;
                showError(turn, event.content);
            } else if (event.type === 'idle') {
                finished = true;
                removeTyping(turn);
                turn.row.remove();
            }
        });
        if (!finished) showError(turn, 'The connection closed before the answer arrived.');
    }

    async function send(text) {
        const message = String(text == null ? input.value : text).trim();
        if (!message || state.busy) return;
        input.value = '';
        autoGrow();
        addUserRow(message);
        const turn = addAssistantRow();
        setBusy(true);
        state.stream = new AbortController();
        try {
            const response = await fetch(API + '/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message, conversation_id: state.conversationId }),
                signal: state.stream.signal,
            });
            const type = response.headers.get('Content-Type') || '';
            if (!type.includes('text/event-stream')) {
                let data = {};
                try { data = await response.json(); } catch (e) { /* not JSON */ }
                showError(turn, data.error || ('HTTP ' + response.status));
            } else {
                await follow(response, turn);
            }
        } catch (e) {
            if (e.name !== 'AbortError') showError(turn, e.message);
        } finally {
            state.stream = null;
            setBusy(false);
            input.focus();
            loadHistory();
        }
    }

    async function stop() {
        sendBtn.disabled = true;
        try {
            await postJson('/chat/stop', { conversation_id: state.conversationId });
        } finally {
            sendBtn.disabled = false;
        }
    }

    // ── History ───────────────────────────────────────────────────────────

    function shortWhen(iso) {
        if (!iso) return '';
        const d = new Date(iso);
        if (isNaN(d)) return '';
        const now = new Date();
        const sameDay = d.toDateString() === now.toDateString();
        return sameDay
            ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
            : d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    }

    async function loadHistory() {
        const list = $('history-list');
        let data;
        try { data = await getJson('/history/list'); } catch (e) { return; }
        const items = (data && data.conversations) || [];
        list.innerHTML = '';
        if (!items.length) {
            list.innerHTML = '<span class="as-muted">No conversations yet.</span>';
            return;
        }
        for (const conv of items) {
            const item = document.createElement('div');
            item.className = 'as-history-item' + (conv.conversation_id === state.conversationId ? ' active' : '');
            const title = document.createElement('span');
            title.className = 'as-history-title';
            title.textContent = (conv.running ? '⏳ ' : '') + (conv.title || 'Conversation');
            title.title = conv.title || '';
            const when = document.createElement('span');
            when.className = 'as-history-when';
            when.textContent = shortWhen(conv.updated_at);
            const del = document.createElement('button');
            del.className = 'as-history-del';
            del.title = 'Delete this conversation';
            del.textContent = '✕';
            del.addEventListener('click', async (ev) => {
                ev.stopPropagation();
                if (!window.confirm('Delete this conversation?')) return;
                const res = await postJson('/history/delete', { conversation_id: conv.conversation_id });
                if (res.error) { window.alert(res.error); return; }
                if (conv.conversation_id === state.conversationId) newChat();
                else loadHistory();
            });
            item.append(title, when, del);
            item.addEventListener('click', () => openConversation(conv.conversation_id));
            list.appendChild(item);
        }
    }

    async function openConversation(conversationId) {
        if (state.busy || !conversationId) return;
        const res = await postJson('/history/load', { conversation_id: conversationId });
        if (!res.success) {
            if (res.error) showBanner(res.error, true);
            return;
        }
        state.conversationId = res.conversation_id;
        chatWindow.innerHTML = '';
        for (const turn of res.turns || []) {
            addUserRow(turn.user_message || '');
            const drawn = addAssistantRow();
            for (const step of turn.steps || []) addStep(drawn, step);
            showAnswer(drawn, turn.result || { data: '' });
        }
        if (!(res.turns || []).length && !res.running) clearChat();
        loadHistory();
        if (res.running) attach(conversationId, res.running_message);
    }

    /* The conversation is still answering (the page was reloaded, or the
       conversation was opened from History mid-turn): follow it to the end. */
    async function attach(conversationId, title) {
        if (title) addUserRow(title);
        const turn = addAssistantRow();
        setBusy(true);
        state.stream = new AbortController();
        try {
            const response = await fetch(API + '/history/stream?conversation_id=' +
                encodeURIComponent(conversationId), { signal: state.stream.signal });
            await follow(response, turn);
        } catch (e) {
            if (e.name !== 'AbortError') showError(turn, e.message);
        } finally {
            state.stream = null;
            setBusy(false);
            loadHistory();
        }
    }

    async function newChat() {
        if (state.busy) return;
        await postJson('/reset', {});
        state.conversationId = '';
        clearChat();
        loadHistory();
        input.focus();
    }

    // ── Sidebar bits ──────────────────────────────────────────────────────

    function showBanner(text, isError) {
        const banner = $('banner');
        if (!text) { banner.hidden = true; return; }
        banner.textContent = text;
        banner.classList.toggle('error', !!isError);
        banner.hidden = false;
    }

    function showIdentity(identity) {
        const chip = $('identity-chip');
        const who = identity || {};
        if (who.signed_in) {
            chip.textContent = 'IPS: ' + who.owner_name;
            chip.classList.remove('warn');
            chip.title = 'IPS sign-in of this PC' + (who.mailbox ? ' · mailbox ' + who.mailbox : '');
        } else if (who.sign_in_state === 'signing_in') {
            chip.textContent = 'IPS: signing in…';
            chip.classList.remove('warn');
        } else {
            chip.textContent = 'IPS: not signed in · retry';
            chip.classList.add('warn');
            chip.title = 'Case lookups need the IPS sign-in. Click to retry the silent sign-in; ' +
                'if it keeps failing, sign in on the Beacon queue page.';
        }
    }

    async function loadState(openCurrent) {
        let data;
        try { data = await getJson('/state'); } catch (e) { return; }
        showIdentity(data.identity);
        showBanner(data.llm_error || '', true);
        if (openCurrent && data.conversation_id) await openConversation(data.conversation_id);
        if (data.identity && data.identity.sign_in_state === 'signing_in') {
            setTimeout(() => loadState(false), 3000);
        }
    }

    async function loadConnectors() {
        const box = $('connector-list');
        let data;
        try { data = await getJson('/connectors'); } catch (e) { return; }
        box.innerHTML = '';
        for (const conn of (data && data.connectors) || []) {
            const line = document.createElement('div');
            line.className = 'as-connector';
            line.title = conn.description || '';
            const name = document.createElement('code');
            name.textContent = conn.name;
            line.appendChild(name);
            if (conn.internal) line.appendChild(tag('internal', 'internal'));
            if (conn.kind === 'act') line.appendChild(tag('act', 'action'));
            box.appendChild(line);
        }
        if (!box.children.length) box.innerHTML = '<span class="as-muted">None available.</span>';
        const problems = (data && data.problems) || {};
        for (const family of Object.keys(problems)) {
            const line = document.createElement('div');
            line.className = 'as-muted';
            line.textContent = family + ': not available (' + problems[family] + ')';
            box.appendChild(line);
        }
    }

    function tag(cls, text) {
        const el = document.createElement('span');
        el.className = 'as-tag ' + cls;
        el.textContent = text;
        return el;
    }

    // ── Scheduled tasks ───────────────────────────────────────────────────

    function taskWhen(iso) {
        const d = new Date(iso);
        if (isNaN(d)) return '';
        const now = new Date();
        const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        if (d.toDateString() === now.toDateString()) return time;
        return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + time;
    }

    function taskButton(label, path, id) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = label;
        btn.addEventListener('click', async (ev) => {
            ev.stopPropagation();
            btn.disabled = true;
            const res = await postJson(path, { id });
            if (res.error) showTaskError(res.error);
            loadTasks();
        });
        return btn;
    }

    function taskLine(task) {
        const line = document.createElement('div');
        line.className = 'as-task ' + (task.status || '');
        const when = document.createElement('span');
        when.className = 'as-task-when';
        when.textContent = taskWhen(task.run_at);
        const what = document.createElement('span');
        what.className = 'as-task-what';
        what.textContent = task.instruction || '';
        what.title = (task.instruction || '') +
            (task.result_preview ? '\n\n' + task.result_preview : '') +
            (task.error ? '\n\n' + task.error : '');
        line.append(when, what);
        if (task.status === 'scheduled') {
            line.appendChild(taskButton('Cancel', '/tasks/cancel', task.id));
        } else if (task.status === 'missed') {
            line.appendChild(taskButton('Run now', '/tasks/run_now', task.id));
            line.appendChild(taskButton('Skip', '/tasks/skip', task.id));
        } else {
            const stateEl = document.createElement('span');
            stateEl.className = 'as-task-state';
            stateEl.textContent = task.status === 'running' ? 'running…' : task.status;
            line.appendChild(stateEl);
        }
        if (task.status === 'done' && task.conversation_id) {
            line.addEventListener('click', () => openConversation(task.conversation_id));
        }
        return line;
    }

    function showTaskError(text) {
        const el = $('task-error');
        el.textContent = text || '';
        el.hidden = !text;
    }

    async function loadTasks() {
        let data;
        try { data = await getJson('/tasks'); } catch (e) { return; }
        const list = $('task-list');
        list.innerHTML = '';
        const rows = ((data && data.open) || []).concat(((data && data.recent) || []).slice(0, 3));
        for (const task of rows) list.appendChild(taskLine(task));
        if (!rows.length) list.innerHTML = '<span class="as-muted">Nothing scheduled.</span>';
        $('task-clock').textContent = data && data.ticker_running === false ? '· clock stopped' : '';
    }

    // ── Notifications ─────────────────────────────────────────────────────

    let lastUnread = 0;

    async function loadNotifications() {
        let data;
        try { data = await getJson('/notifications'); } catch (e) { return; }
        const unread = (data && data.unread) || 0;
        const count = $('bell-count');
        count.textContent = String(unread);
        count.hidden = unread === 0;
        document.title = (unread ? '(' + unread + ') ' : '') + 'Beacon Assistant';
        const list = $('bell-list');
        list.innerHTML = '';
        for (const note of (data && data.items) || []) {
            const item = document.createElement('div');
            item.className = 'as-note' + (note.read ? '' : ' unread');
            const title = document.createElement('div');
            title.className = 'as-note-title';
            title.textContent = note.title || '';
            const text = document.createElement('div');
            text.className = 'as-note-text';
            text.textContent = (note.internal ? '[INTERNAL] ' : '') + (note.text || '');
            const when = document.createElement('div');
            when.className = 'as-note-when';
            when.textContent = shortWhen(note.at) + (note.kind === 'task_missed' ? ' · missed'
                : note.kind === 'task_failed' ? ' · failed' : '');
            item.append(title, text, when);
            item.addEventListener('click', async () => {
                await postJson('/notifications/read', { ids: [note.id] });
                $('bell-panel').hidden = true;
                if (note.conversation_id) await openConversation(note.conversation_id);
                loadNotifications();
            });
            list.appendChild(item);
        }
        if (!list.children.length) list.innerHTML = '<div class="as-note as-muted">Nothing yet.</div>';
        // A task just finished: its line and the history list changed too.
        if (unread !== lastUnread) { loadTasks(); loadHistory(); }
        lastUnread = unread;
    }

    // ── Voice input ───────────────────────────────────────────────────────
    // Click the microphone to start, click again to stop (or hold it down
    // and let go). The clip is posted to this PC's own /assistant/transcribe
    // and the text is put in the box - never sent: the user reads it first.

    const voice = { status: null, recorder: null, chunks: [], stream: null, startedAt: 0,
                    timer: null, pressAt: 0, cancelled: false };
    const micBtn = $('mic-btn');
    const micLang = $('mic-lang');
    const HOLD_MS = 500;
    // Which language the user speaks is their own habit: remembered in this
    // browser only. The page works the same when storage is unavailable.
    try { micLang.value = window.localStorage.getItem('assistant.micLang') || ''; } catch (e) { /* ignore */ }
    micLang.addEventListener('change', () => {
        try { window.localStorage.setItem('assistant.micLang', micLang.value); } catch (e) { /* ignore */ }
    });

    function voiceHint(text, opts) {
        const el = $('voice-hint');
        el.innerHTML = '';
        if (!text) { el.hidden = true; return; }
        el.classList.toggle('error', !!(opts && opts.error));
        const span = document.createElement('span');
        span.textContent = text;
        el.appendChild(span);
        if (opts && opts.button) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.textContent = opts.button;
            btn.addEventListener('click', opts.onClick);
            el.appendChild(btn);
        }
        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'as-voice-close';
        close.textContent = '✕';
        close.addEventListener('click', () => voiceHint(''));
        el.appendChild(close);
        el.hidden = false;
    }

    async function loadVoice() {
        if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder)) {
            return;                                   // this browser cannot record: no button
        }
        try { voice.status = await getJson('/voice/status'); } catch (e) { return; }
        micBtn.hidden = false;
        micLang.hidden = !voice.status.available;
        micBtn.classList.toggle('setup', !voice.status.available);
        micBtn.title = voice.status.available
            ? 'Voice input: click to start, click again to stop (or hold while speaking)'
            : 'Voice input is not set up yet - click for details';
        if (voice.status.state === 'downloading') {
            voiceHint(voice.status.hint);
            setTimeout(loadVoice, 4000);
        } else if (voice.downloading) {
            voice.downloading = false;
            voiceHint(voice.status.available ? 'The speech model is ready. Click the microphone to talk.'
                : (voice.status.download_error || voice.status.hint), { error: !voice.status.available });
        }
    }

    function explainVoiceSetup() {
        const st = voice.status || {};
        if (st.state === 'model_missing') {
            voiceHint(st.hint + (st.download_error ? ' Last attempt failed: ' + st.download_error : ''), {
                button: 'Download (' + st.model_size_mb + ' MB)',
                onClick: async () => {
                    voice.downloading = true;
                    voiceHint('Starting the download…');
                    await postJson('/voice/prepare', {});
                    loadVoice();
                },
            });
        } else {
            voiceHint(st.hint || 'Voice input is not available.');
        }
    }

    function micState(name) {
        micBtn.classList.toggle('recording', name === 'recording');
        micBtn.classList.toggle('working', name === 'working');
        micBtn.textContent = name === 'recording' ? '■' : name === 'working' ? '…' : '🎤';
    }

    function stopTracks() {
        if (voice.stream) voice.stream.getTracks().forEach((t) => t.stop());
        voice.stream = null;
        clearInterval(voice.timer);
    }

    async function startRecording() {
        if (voice.recorder) return;
        let stream;
        try {
            stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        } catch (e) {
            voiceHint('The microphone could not be opened (' + (e.name || e.message) +
                '). Allow microphone access for this page in the browser.', { error: true });
            return;
        }
        voice.stream = stream;
        voice.chunks = [];
        voice.cancelled = false;
        const type = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus']
            .find((t) => MediaRecorder.isTypeSupported(t)) || '';
        const recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
        voice.recorder = recorder;
        recorder.addEventListener('dataavailable', (ev) => { if (ev.data.size) voice.chunks.push(ev.data); });
        recorder.addEventListener('stop', () => finishRecording(recorder.mimeType || type));
        recorder.start();
        voice.startedAt = Date.now();
        micState('recording');
        const limit = ((voice.status && voice.status.max_seconds) || 120) * 1000;
        voice.timer = setInterval(() => {
            const sec = Math.floor((Date.now() - voice.startedAt) / 1000);
            voiceHint('Listening… ' + sec + ' s  (click ■ to finish, Esc to discard)');
            if (Date.now() - voice.startedAt > limit) stopRecording();
        }, 250);
    }

    function stopRecording(cancel) {
        if (!voice.recorder) return;
        voice.cancelled = !!cancel;
        try { voice.recorder.stop(); } catch (e) { /* already stopped */ }
    }

    async function finishRecording(type) {
        const cancelled = voice.cancelled;
        const blob = new Blob(voice.chunks, { type: type || 'audio/webm' });
        voice.recorder = null;
        voice.chunks = [];
        stopTracks();
        if (cancelled || blob.size < 800) {
            micState('idle');
            voiceHint(cancelled ? '' : 'That was too short - hold a little longer.');
            return;
        }
        micState('working');
        voiceHint('Recognising…');
        try {
            const lang = micLang.value;
            const res = await fetch(API + '/transcribe' + (lang ? '?lang=' + encodeURIComponent(lang) : ''), {
                method: 'POST', headers: { 'Content-Type': type || 'audio/webm' }, body: blob,
            });
            const data = await res.json();
            if (!data.success) {
                voiceHint(data.error || 'Recognition failed.', { error: true });
                if (data.code && data.code !== 'bad_audio') loadVoice();
            } else if (!data.text) {
                voiceHint('Nothing was heard. Try again a little closer to the microphone.');
            } else {
                const lead = input.value && !/\s$/.test(input.value) ? ' ' : '';
                input.value += lead + data.text;
                autoGrow();
                input.focus();
                voiceHint('');
            }
        } catch (e) {
            voiceHint('Recognition failed: ' + e.message, { error: true });
        } finally {
            micState('idle');
        }
    }

    function autoGrow() {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    }

    // ── Wiring ────────────────────────────────────────────────────────────

    sendBtn.addEventListener('click', () => (state.busy ? stop() : send()));
    input.addEventListener('input', autoGrow);
    input.addEventListener('keydown', (ev) => {
        // Enter sends; Shift+Enter is a new line; Enter while an IME is
        // composing (Chinese input) only confirms the candidate.
        if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing && ev.keyCode !== 229) {
            ev.preventDefault();
            if (!state.busy) send();
        }
    });
    $('new-chat-btn').addEventListener('click', newChat);
    $('sidebar-toggle').addEventListener('click', () =>
        document.body.classList.toggle('as-sidebar-collapsed'));
    $('identity-chip').addEventListener('click', async () => {
        if (!$('identity-chip').classList.contains('warn')) return;
        const res = await postJson('/sign_in', {});
        showIdentity(res.identity);
        setTimeout(() => loadState(false), 3000);
    });
    for (const btn of document.querySelectorAll('.as-quick-btn')) {
        btn.addEventListener('click', () => {
            input.value = btn.dataset.prompt || '';
            autoGrow();
            input.focus();
        });
    }

    $('task-form').addEventListener('submit', async (ev) => {
        ev.preventDefault();
        showTaskError('');
        const when = $('task-when').value.trim();
        const what = $('task-what').value.trim();
        if (!when || !what) { showTaskError('Give both a time and what to do.'); return; }
        const res = await postJson('/tasks/add', { when, instruction: what });
        if (!res.success) { showTaskError(res.error || 'Could not schedule that.'); return; }
        $('task-when').value = '';
        $('task-what').value = '';
        loadTasks();
    });
    $('bell-btn').addEventListener('click', (ev) => {
        ev.stopPropagation();
        $('bell-panel').hidden = !$('bell-panel').hidden;
    });
    $('bell-panel').addEventListener('click', (ev) => ev.stopPropagation());
    document.addEventListener('click', () => { $('bell-panel').hidden = true; });
    $('bell-read-all').addEventListener('click', async () => {
        await postJson('/notifications/read', {});
        loadNotifications();
    });

    micBtn.addEventListener('pointerdown', () => {
        voice.pressAt = Date.now();
        if (!voice.status || !voice.status.available) return;
        if (!voice.recorder && !micBtn.classList.contains('working')) {
            voice.startedByPress = true;
            startRecording();
        } else {
            voice.startedByPress = false;
        }
    });
    micBtn.addEventListener('pointerup', () => {
        if (!voice.status || !voice.status.available) { explainVoiceSetup(); return; }
        const held = Date.now() - voice.pressAt;
        // Held down while speaking: letting go finishes. A short click that
        // started the recording leaves it running until the next click.
        if (voice.startedByPress ? held >= HOLD_MS : true) stopRecording();
    });
    micBtn.addEventListener('pointerleave', () => {
        if (voice.startedByPress && voice.recorder && Date.now() - voice.pressAt >= HOLD_MS) stopRecording();
    });
    document.addEventListener('keydown', (ev) => {
        if (ev.key === 'Escape' && voice.recorder) stopRecording(true);
    });

    // For the page checks (and for stage add-ons loaded after this file).
    window.BeaconAssistant = { send, newChat, openConversation, safeHtml, state, postJson, getJson,
                               loadTasks, loadNotifications, loadVoice, voice };
    loadVoice();

    loadConnectors();
    loadHistory();
    loadState(true);
    loadTasks();
    loadNotifications();
    // The clock that runs tasks is in the app; the page only looks.
    setInterval(() => { loadNotifications(); loadTasks(); }, 30000);
    input.focus();
})();
