    // Flip the sidebar between the live controls and the saved history.
    function switchSidebarView(view) {
        const cv = document.getElementById('sidebar-controls-view');
        const hv = document.getElementById('sidebar-history-view');
        const tc = document.getElementById('tab-controls');
        const th = document.getElementById('tab-history');
        if (view === 'history') {
            if (cv) cv.style.display = 'none';
            if (hv) hv.style.display = 'block';
            if (tc) tc.classList.remove('active');
            if (th) th.classList.add('active');
            if (typeof _updateRestoreBtn === 'function') _updateRestoreBtn();
            loadHistoryList();
        } else {
            if (cv) cv.style.display = 'block';
            if (hv) hv.style.display = 'none';
            if (th) th.classList.remove('active');
            if (tc) tc.classList.add('active');
        }
    }

    // ── New conversation / restore the pre-history working session ──────
    // The "live session" is whatever the user is working on (e.g. the log +
    // issue time + chat-box text carried in from the case-number flow).
    // We snapshot it the first time they open a saved history conversation so
    // "↩ Back to my session" can bring it all back.
    window.__liveSessionSnapshot = window.__liveSessionSnapshot || null;

    // True while the user is viewing a SAVED history conversation (vs. working
    // in a fresh, unsent "live" session). Loading a log or starting a new
    // session is still live work; only openHistoryConversation flips this on.
    // Used to decide when to (re)snapshot the live session: we always re-snap
    // the latest live state on leaving it, but never overwrite that snapshot
    // while merely hopping between saved conversations.
    window.__viewingSavedConv = window.__viewingSavedConv || false;

    // Per-conversation chat-box draft text (typed but not yet sent). Keyed by
    // conversation id; the not-yet-created draft uses the '__draft__' key.
    // Each entry is an object { text, issueTime, logPath } so unsent edits to
    // the chat box, the sidebar Issue Time AND the log source are preserved
    // when switching away.
    // In-memory only — naturally cleared when the app/tab is closed or reloaded.
    window.__draftByConv = window.__draftByConv || {};

    // Key for the conversation the chat box currently belongs to.
    function _draftKey() { return window.__feedbackConversationId || '__draft__'; }

    // Stash whatever is currently typed in the chat box, the current Issue
    // Time AND the current log source under the active conversation, so
    // switching away and back keeps each conversation's unsent draft separate.
    function saveCurrentDraft() {
        const inp = document.getElementById('user-input');
        const logInp = document.getElementById('log-path-input');
        const issueTime = (typeof getIssueTimeString === 'function')
            ? (getIssueTimeString() || '') : '';
        const text = inp ? (inp.value || '') : '';
        const logPath = logInp ? (logInp.value || '') : '';
        const key = _draftKey();
        // Don't keep empty drafts around: an all-blank entry means "no draft,
        // use the server-restored state", which is identical to having no key
        // at all. Pruning it keeps the in-memory map from accumulating empty
        // objects for every conversation the user merely clicks through.
        if (!text && !issueTime && !logPath) {
            delete window.__draftByConv[key];
            return;
        }
        window.__draftByConv[key] = { text: text, issueTime: issueTime, logPath: logPath };
    }

    // Load the saved UNSENT CHAT TEXT for a conversation key into the chat
    // box (empty if none was saved). Returns true if a saved draft existed.
    //
    // IMPORTANT: this intentionally does NOT re-apply a draft's logPath or
    // issueTime anymore. openHistoryConversation() already restores those
    // AUTHORITATIVELY from the server's /history/load response right before
    // calling this. Re-invoking setLog() here to "sync" a stale/leftover
    // draft log path used to silently STEAL the conversation right back:
    // CHATBOT_API/set_log mints a brand-new conversation id on every single
    // call (even a same-path one) and resets the agent's conversation, so an
    // incidental setLog() call after opening a history item immediately
    // rotated window.__feedbackConversationId away from the item the user
    // just clicked — the history entry never appeared to "load" at all.
    // Restoring only the chat-box text avoids ever re-invoking setLog() here.
    async function restoreDraftFor(key) {
        const inp = document.getElementById('user-input');
        const entry = window.__draftByConv[key || '__draft__'];
        const hasEntry = (entry !== undefined && entry !== null);
        // Normalise: a legacy string entry only carried the chat text.
        const draft = (typeof entry === 'string') ? { text: entry } : (entry || {});
        if (inp) {
            inp.value = draft.text || '';
            if (typeof onUserInputChange === 'function') onUserInputChange(inp);
        }
        return hasEntry;
    }

    function _historyWelcomeHtml() {
        return '<div class="welcome-msg" id="welcome-msg">' +
                 '<div class="big-icon">🤖</div>' +
                 '<strong>' + escapeHtml((window.CHATBOT && window.CHATBOT.title) || 'Chatbot') + '</strong><br>' +
                 'Load a log file on the left, then ask me anything about it.<br>' +
                 'I will apply the relevant skill filter and analyse the logs for you.' +
               '</div>';
    }

    // Show / hide the "Back to my session" button. Shown whenever an unsent
    // live-session snapshot exists (the log / issue time / chat-box draft the
    // user was preparing before they browsed history).
    //
    // IMPORTANT: this gates on the SNAPSHOT's existence, NOT the global
    // `firstRoundSent`. The snapshot is only ever captured while the live
    // session was still unsent (see openHistoryConversation), so its mere
    // presence already means "there's an unsent draft to resume". Gating on
    // `firstRoundSent` here was the bug: sending a FOLLOW-UP inside a SAVED
    // conversation flips that global flag true, which used to wrongly hide
    // this button and strand the still-unsent live draft.
    function _updateRestoreBtn() {
        const btn = document.getElementById('hist-restore-btn');
        if (!btn) return;
        const s = window.__liveSessionSnapshot;
        const useful = !!(s && (s.logPath || s.issueTime || s.draft));
        btn.style.display = useful ? 'block' : 'none';
    }

    // Capture the current working session so it can be restored later. Only
    // snapshots a not-yet-sent first conversation (loaded log / issue time /
    // chat-box draft); a session that already sent its first question is a
    // real history conversation and has nothing "unsent" to return to.
    function captureLiveSessionSnapshot() {
        if (firstRoundSent) { window.__liveSessionSnapshot = null; _updateRestoreBtn(); return; }
        const logInp = document.getElementById('log-path-input');
        const draftInp = document.getElementById('user-input');
        window.__liveSessionSnapshot = {
            logPath: logInp ? (logInp.value || '') : '',
            issueTime: (typeof getIssueTimeString === 'function') ? (getIssueTimeString() || '') : '',
            draft: draftInp ? (draftInp.value || '') : '',
        };
        _updateRestoreBtn();
    }

    // ➕ New Conversation: reset everything to the fresh, empty page state the
    // user sees when first entering the agent (no log, no issue time, no chat).
    async function startNewConversation() {
        if (typeof _abortActiveStream === 'function') _abortActiveStream();
        // Dismiss any blocking modal left open by the previous send (the
        // no-issue-time confirm prompt or the time-capture popup). A stuck
        // modal overlay would otherwise swallow clicks/typing in the fresh
        // session, making the chat box look frozen.
        if (typeof closeConfirmModal === 'function') closeConfirmModal(false);
        if (typeof closeTimeCaptureModal === 'function') closeTimeCaptureModal();
        // Keep the current conversation's chat-box draft before leaving it.
        saveCurrentDraft();
        // Reset the server-side agent in the BACKGROUND — do NOT await it.
        // Another conversation may still be analysing (the server thread is
        // busy), and awaiting here would stall the whole reset, leaving the
        // new session's chat box unusable until that analysis frees the
        // server. Fire-and-forget so the fresh, typeable session appears
        // instantly; the previous analysis keeps running untouched.
        try { fetch(`${CHATBOT_API}/reset`, {method: 'POST'}).catch(function () {}); } catch (e) { /* ignore */ }

        window.__feedbackConversationId = '';
        window.__turnContext = {};
        window.__lastFeedback = {};
        // Brand-new draft area starts empty (drop any stale '__draft__' entry).
        delete window.__draftByConv['__draft__'];
        // A fresh session has nothing sent yet, so the next time the user
        // jumps into history their new unsent work (log / issue time / draft)
        // can be snapshotted for "↩ Resume draft".
        firstRoundSent = false;
        window.__liveSessionSnapshot = null;
        // Back in a fresh live session (not viewing a saved conversation).
        window.__viewingSavedConv = false;

        // Drop any in-flight send state left over from the PREVIOUS (possibly
        // still-running or just-aborted) analysis. Without this, the next Send
        // in the fresh session sees a lingering multi-time chain and treats
        // itself as a continuation — popping the old queue and IGNORING the
        // user's newly typed message, which looks like "the input box is
        // frozen / won't accept what I type".
        window.__multiTimeContext = null;
        window.__skipTimeCaptureOnce = false;
        // The previous analysis left Send disabled while it streamed; the fresh
        // session must own its own enabled state, so unstick it here and let
        // validateUserInput() recompute it from the empty-session rules below.
        const _sendBtn = document.getElementById('send-btn');
        if (_sendBtn) _sendBtn.disabled = false;

        // Clear the log path + status.
        const logInp = document.getElementById('log-path-input');
        if (logInp) logInp.value = '';
        logLoaded = false;
        const st = document.getElementById('log-status');
        if (st) {
            // Mirror showStatus()'s own reset (not just innerHTML) so a
            // pending auto-hide timer from the previous conversation can't
            // fire later and hide a message the new conversation just set.
            if (st._hideTimer) { clearTimeout(st._hideTimer); st._hideTimer = null; }
            st.innerHTML = '';
            st.className = 'log-status';
            st.style.display = 'none';
        }

        // Clear the issue time (primary + extra rows).
        if (typeof clearAllIssueTimes === 'function') clearAllIssueTimes();

        // Clear the chat-box draft text.
        const draftInp = document.getElementById('user-input');
        if (draftInp) {
            draftInp.value = '';
            if (typeof onUserInputChange === 'function') onUserInputChange(draftInp);
        }

        // Reset the chat window back to the initial welcome message.
        const cw = document.getElementById('chat-window');
        if (cw) cw.innerHTML = _historyWelcomeHtml();

        // Nothing unsent yet → keep "↩ Resume draft" hidden. It reappears only
        // after the user fills in new work and then clicks a history record
        // (captured in openHistoryConversation).
        _updateRestoreBtn();
        // Guarantee the chat box is typeable again, regardless of any state the
        // previous (possibly still-running) analysis left behind.
        _unlockChatInput();
        switchSidebarView('controls');
        loadHistoryList();
    }

    // Force the chat input back into a clean, typeable state. Used after
    // leaving a (possibly still-running) analysis — New session, Resume draft,
    // or opening a history record — so no leftover state from the previous
    // send can make the chat box look frozen: removes the typing indicator,
    // dismisses any blocking modal, clears in-flight multi-time send state,
    // re-enables Send, and recomputes the input gate.
    function _unlockChatInput() {
        if (typeof removeTyping === 'function') { try { removeTyping(); } catch (e) {} }
        if (typeof closeConfirmModal === 'function') closeConfirmModal(false);
        if (typeof closeTimeCaptureModal === 'function') closeTimeCaptureModal();
        window.__multiTimeContext = null;
        window.__skipTimeCaptureOnce = false;
        const sb = document.getElementById('send-btn');
        if (sb) sb.disabled = false;
        if (typeof validateUserInput === 'function') validateUserInput();
    }

    // ↩ Resume my draft: restore the unsent draft (log path, issue time and
    // chat-box text) the user was preparing before they started browsing
    // history. Only meaningful while the first question hasn't been sent yet.
    async function restoreLiveSession() {
        const snap = window.__liveSessionSnapshot;
        // Gate on the snapshot only — NOT firstRoundSent. A follow-up sent in a
        // SAVED conversation flips firstRoundSent true, but the unsent live
        // draft captured earlier must still be resumable.
        if (!snap) return;
        if (typeof _abortActiveStream === 'function') _abortActiveStream();
        // Dismiss any blocking modal left open by the previous send so it
        // can't swallow input in the restored session.
        if (typeof closeConfirmModal === 'function') closeConfirmModal(false);
        if (typeof closeTimeCaptureModal === 'function') closeTimeCaptureModal();

        // The restored live session is genuinely UNSENT again, so clear the
        // global first-round flag: its next Send must re-apply the first-round
        // gate (and any earlier saved-conversation follow-up that set it true
        // must not leak into this fresh draft).
        firstRoundSent = false;

        window.__feedbackConversationId = '';
        window.__turnContext = {};
        window.__lastFeedback = {};
        // Clear any in-flight send state from the analysis we just left, so the
        // restored session's next Send isn't mistaken for a multi-time
        // continuation (which would ignore the user's typed message).
        window.__multiTimeContext = null;
        window.__skipTimeCaptureOnce = false;

        // Empty chat window — the arrived-from-case-number page starts with an
        // empty conversation and the description pre-filled in the input box.
        const cw = document.getElementById('chat-window');
        if (cw) cw.innerHTML = _historyWelcomeHtml();

        // ── Restore the DOM (log path field, issue time, draft text) and
        // unlock the input SYNCHRONOUSLY, BEFORE any server round-trip. When
        // the previous analysis is still occupying the server, awaiting
        // /reset or /set_log here would stall the whole restore and leave the
        // chat box looking frozen. Doing the DOM work first guarantees the
        // user can type immediately; the server re-prime runs in the
        // background below.
        const logInp = document.getElementById('log-path-input');
        if (logInp) logInp.value = snap.logPath || '';
        if (snap.issueTime && typeof setIssueTimeFromString === 'function') {
            setIssueTimeFromString(snap.issueTime);
        }
        const draftInp = document.getElementById('user-input');
        if (draftInp) {
            draftInp.value = snap.draft || '';
            if (typeof onUserInputChange === 'function') onUserInputChange(draftInp);
        }
        // The draft area now reflects the restored draft (text + issue time +
        // log source), kept in the same object shape saveCurrentDraft() uses.
        window.__draftByConv['__draft__'] = {
            text: snap.draft || '',
            issueTime: snap.issueTime || '',
            logPath: snap.logPath || '',
        };

        // Back in the live session — drop the snapshot so a later history jump
        // re-snapshots the (possibly changed) working session.
        window.__liveSessionSnapshot = null;
        // No longer viewing a saved conversation.
        window.__viewingSavedConv = false;
        _updateRestoreBtn();
        // Guarantee the chat box is typeable again RIGHT NOW, regardless of any
        // state the previous (possibly still-running) analysis left behind.
        _unlockChatInput();
        // Put the caret back in the chat box so the user can keep typing the
        // restored draft without a click — makes "it's typeable again" obvious.
        if (draftInp) { try { draftInp.focus(); } catch (e) { /* ignore */ } }
        switchSidebarView('controls');
        loadHistoryList();

        // Snapshot the restored draft so the background re-prime (which assigns
        // a fresh conversation id) can re-file it under the new key instead of
        // orphaning it — otherwise jumping back to history loses the draft.
        const _restored = {
            text: snap.draft || '',
            issueTime: snap.issueTime || '',
            logPath: snap.logPath || '',
        };

        // ── Re-prime the server in the BACKGROUND (do NOT await) ──────────
        // Re-load the log so follow-up questions work. Fire-and-forget so a
        // busy analysis can never stall the UI; the Send button re-enables
        // itself once setLog() resolves and logLoaded flips.
        (async () => {
            if (window.CHATBOT && window.CHATBOT.history_reset_before_set_log) {
                try { await fetch(`${CHATBOT_API}/reset`, {method: 'POST'}); } catch (e) { /* ignore */ }
            }
            if (snap.logPath && typeof setLog === 'function') {
                // NOTE: no standalone /reset here on purpose. BT's /set_log
                // already calls agent.reset_conversation() server-side, so an
                // extra sequential /reset round trip was pure added latency
                // before the session became send-ready — and BT's set_log also
                // scans the event log, making that wait noticeable. Skipping it
                // roughly halves the "resume draft feels laggy" delay.
                // Suppress setLog's async issue-time auto-fill when the draft
                // carries its own time, so it can't clobber the restored time.
                try { await setLog({ skipIssueTimeAutofill: !!snap.issueTime }); } catch (e) { /* ignore */ }
            } else {
                // No log to (re)load — reset the agent directly so the restored
                // session still starts from a clean server-side conversation.
                try { await fetch(`${CHATBOT_API}/reset`, {method: 'POST'}); } catch (e) { /* ignore */ }
                logLoaded = false;
                if (typeof validateUserInput === 'function') validateUserInput();
            }
            // setLog() reassigns window.__feedbackConversationId to a brand-new
            // id, which changes _draftKey(). Re-file the restored draft under
            // BOTH the live-draft key and the resolved conversation key so a
            // later jump to history (and back) can always recover it.
            try {
                window.__draftByConv['__draft__'] = { ..._restored };
                const k = (typeof _draftKey === 'function') ? _draftKey() : null;
                if (k && k !== '__draft__') {
                    window.__draftByConv[k] = { ..._restored };
                }
            } catch (e) { /* ignore */ }
        })();
    }

    function _fmtHistoryMeta(c) {
        if (c.running) return '⏳ Running…';
        let when = '';
        if (c.updated_at) {
            try {
                const d = new Date(c.updated_at);
                if (!isNaN(d)) {
                    when = d.toLocaleString([], {
                        month: 'short', day: 'numeric',
                        hour: '2-digit', minute: '2-digit',
                    });
                }
            } catch (e) { /* ignore */ }
        }
        const n = c.turn_count || 0;
        const msgs = n + ' msg' + (n === 1 ? '' : 's');
        return when ? (when + ' · ' + msgs) : msgs;
    }

    // Fetch + render the list of saved conversations, newest first.
    // Self-rescheduling poll: while any analysis is running we keep refreshing
    // the list so the ⏳ entry shows up (and disappears) on its own. Cleared
    // automatically once nothing is running.
    let _historyPollTimer = null;
    function _scheduleHistoryPoll(anyRunning) {
        if (_historyPollTimer) { clearTimeout(_historyPollTimer); _historyPollTimer = null; }
        if (anyRunning) {
            _historyPollTimer = setTimeout(function () { loadHistoryList(); }, 3000);
        }
    }

    async function loadHistoryList() {
        const listEl = document.getElementById('history-list');
        const emptyEl = document.getElementById('history-empty');
        if (!listEl) return;
        try {
            const res = await fetch(`${CHATBOT_API}/history/list`);
            const data = await res.json();
            const convs = (data && data.success && data.conversations) ? data.conversations : [];
            // Keep refreshing on a short timer while any analysis is running so
            // its ⏳ entry appears (and clears) on its own — without the user
            // having to click another record to force a reload.
            _scheduleHistoryPoll(convs.some(function (c) { return c.running; }));
            if (!convs.length) {
                listEl.innerHTML = '';
                if (emptyEl) emptyEl.style.display = 'block';
                return;
            }
            if (emptyEl) emptyEl.style.display = 'none';
            const activeId = window.__feedbackConversationId || '';
            listEl.innerHTML = convs.map(function (c) {
                const id = c.conversation_id;
                const active = (id === activeId) ? ' active' : '';
                const running = c.running ? ' running' : '';
                const title = escapeHtml(c.title || 'Conversation');
                const meta = escapeHtml(_fmtHistoryMeta(c));
                const runDot = c.running
                    ? '<span class="history-run-dot" title="Analysis in progress"></span>'
                    : '';
                const pinned = !!c.pinned;
                const pinIcon = pinned
                    ? '<span class="history-pin-icon" title="Pinned">📌</span>'
                    : '';
                // Case record line: case number (+ issue type) shown beneath
                // the chat title so a conversation is identifiable by case.
                const caseLine = c.case_nbr
                    ? '<div class="history-item-case" title="Case number">📋 ' + escapeHtml(c.case_nbr)
                        + (c.issue_type ? ' · ' + escapeHtml(c.issue_type) : '') + '</div>'
                    : '';
                const pinLabel = pinned ? '📌 Unpin' : '📌 Pin';
                return '<div class="history-item' + active + running + (pinned ? ' pinned' : '') + '" data-id="' + id + '"' +
                       ' data-title="' + title + '" data-pinned="' + (pinned ? '1' : '0') + '"' +
                       ' onclick="openHistoryConversation(\'' + id + '\')" title="' + title + '">' +
                       '<div class="history-item-main">' +
                         '<div class="history-item-title">' + pinIcon + runDot + title + '</div>' +
                         caseLine +
                         '<div class="history-item-meta">' + meta + '</div>' +
                       '</div>' +
                       '<div class="history-menu-wrap">' +
                         '<button class="history-kebab" title="More actions"' +
                         ' onclick="toggleHistoryMenu(event, \'' + id + '\')">⋮</button>' +
                         '<div class="history-menu" id="history-menu-' + id + '">' +
                           '<button class="history-menu-item" onclick="pinHistoryConversation(\'' + id + '\', event)">' + pinLabel + '</button>' +
                           '<button class="history-menu-item" onclick="renameHistoryConversation(\'' + id + '\', event)">✏️ Rename</button>' +
                           '<button class="history-menu-item danger" onclick="deleteHistoryConversation(\'' + id + '\', event)">🗑 Delete</button>' +
                         '</div>' +
                       '</div>' +
                       '</div>';
            }).join('');
        } catch (e) {
            console.warn('[history] list failed:', e);
        }
    }

    // ── Per-item actions menu (Claude-style ⋮ kebab) ─────────────────────
    // Close any open history menu (and clear the "menu open" highlight).
    function closeHistoryMenus(exceptId) {
        document.querySelectorAll('.history-menu.open').forEach(function (m) {
            if (exceptId && m.id === 'history-menu-' + exceptId) return;
            m.classList.remove('open');
        });
        document.querySelectorAll('.history-item.menu-open').forEach(function (it) {
            if (exceptId && it.getAttribute('data-id') === exceptId) return;
            it.classList.remove('menu-open');
        });
    }

    // Toggle the ⋮ dropdown for one conversation.
    function toggleHistoryMenu(ev, id) {
        if (ev) ev.stopPropagation();
        const menu = document.getElementById('history-menu-' + id);
        if (!menu) return;
        const willOpen = !menu.classList.contains('open');
        closeHistoryMenus(willOpen ? id : null);
        menu.classList.toggle('open', willOpen);
        const item = menu.closest('.history-item');
        if (item) item.classList.toggle('menu-open', willOpen);
    }

    // Close menus when clicking anywhere else.
    document.addEventListener('click', function (e) {
        if (!e.target.closest('.history-menu-wrap')) closeHistoryMenus();
    });

    // ── Safety net: recover from a stuck/invisible modal overlay ─────────
    // Existing comments elsewhere ("A stuck modal overlay would otherwise
    // swallow clicks/typing... making the chat box look frozen") already
    // acknowledge this failure mode — the no-issue-time confirm prompt / the
    // time-capture popup / the AI-time-consent popup can be left open by an
    // interrupted flow (e.g. an aborted stream, a fast double-click, or a
    // race between two async paths), and each is a full-viewport
    // `position:fixed` overlay. Previously the ONLY recovery was clicking
    // "New session" / "Resume draft" (which call closeConfirmModal /
    // closeTimeCaptureModal). This makes EVERY click on the page self-heal:
    // if the click landed outside the modal's own dialog card, the modal is
    // stuck/irrelevant to what the user is doing, so close it. A click that
    // lands INSIDE the dialog card is untouched — the modal's own buttons
    // already handle that, and its backdrop already closes on outside
    // clicks, so this only extends that same behavior to clicks that hit
    // some OTHER element instead of the backdrop.
    document.addEventListener('click', function (e) {
        if (!e.target.closest('.cm-dialog')) {
            const cm = document.getElementById('confirm-modal');
            if (cm && cm.style.display && cm.style.display !== 'none') {
                closeConfirmModal(false);
            }
        }
        if (!e.target.closest('.fbd-card')) {
            const tcm = document.getElementById('time-capture-modal');
            if (tcm && tcm.classList.contains('open') && typeof closeTimeCaptureModal === 'function') {
                closeTimeCaptureModal();
            }
            const consent = document.getElementById('llm-time-consent-modal');
            if (consent && consent.classList.contains('open') && typeof closeLlmTimeConsent === 'function') {
                closeLlmTimeConsent();
            }
        }
    }, true);   // capture phase: runs even if the click target is otherwise inert

    // 📌 Pin / Unpin a conversation.
    async function pinHistoryConversation(id, ev) {
        if (ev) ev.stopPropagation();
        closeHistoryMenus();
        const item = document.querySelector('.history-item[data-id="' + id + '"]');
        const currentlyPinned = item && item.getAttribute('data-pinned') === '1';
        try {
            const res = await fetch(`${CHATBOT_API}/history/pin`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({conversation_id: id, pinned: !currentlyPinned}),
            });
            const data = await res.json();
            if (data && data.success) loadHistoryList();
        } catch (e) {
            console.warn('[history] pin failed:', e);
        }
    }

    // ✏️ Rename a conversation (inline prompt, prefilled with current title).
    async function renameHistoryConversation(id, ev) {
        if (ev) ev.stopPropagation();
        closeHistoryMenus();
        const item = document.querySelector('.history-item[data-id="' + id + '"]');
        const current = item ? (item.getAttribute('data-title') || '') : '';
        const next = window.prompt('Rename conversation:', current);
        if (next === null) return;            // cancelled
        const title = next.trim();
        if (!title || title === current) return;
        try {
            const res = await fetch(`${CHATBOT_API}/history/rename`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({conversation_id: id, title: title}),
            });
            const data = await res.json();
            if (data && data.success) loadHistoryList();
        } catch (e) {
            console.warn('[history] rename failed:', e);
        }
    }

    // Re-render saved turns into the chat window, mirroring the SSE 'done'
    // rendering path so a loaded conversation looks identical to a live one.
    function renderHistoryTurns(turns) {
        hideWelcome();
        (turns || []).forEach(function (t) {
            if (t.user_message) appendUserMsg(t.user_message);
            const r = t.result || {};
            const turnId = t.turn_id || null;
            if (r.type === 'report') {
                appendReport(r.data || {}, '📊 Analysis Report', r.issue_time || null, turnId);
            } else if (r.type === 'partial_report' && r.data) {
                appendReport(r.data, '⚠️ Partial Analysis (Step Limit)', r.issue_time || null, turnId);
            } else if (r.type === 'text') {
                appendAssistantText(r.data || '_No response._', turnId);
            } else {
                appendAssistantText('⚠️ ' + (r.data || 'Unknown response.'), turnId);
            }
        });
        scrollBottom();
    }

    // ── Live-analysis reconnect ──────────────────────────────────────────
    // window.__streamCtl holds the AbortController for whatever stream is
    // currently rendering into #chat-window. Switching conversations aborts it
    // — the SERVER job keeps running, only the local rendering stops.
    function _abortActiveStream() {
        if (window.__streamCtl) {
            try { window.__streamCtl.abort(); } catch (e) { /* ignore */ }
            window.__streamCtl = null;
        }
    }

    // Self-contained "Agent Processing Steps" card + step renderer, mirroring
    // the live send path so a reconnected stream renders identically.
    function __makeStepCard() {
        const steps = [];
        let bodyEl = null, cardId = null;
        const startTs = Date.now();
        let lastTs = startTs;
        function fmtElapsed(ms) {
            if (ms < 1000) return ms + 'ms';
            if (ms < 60000) return (ms / 1000).toFixed(1) + 's';
            const m = Math.floor(ms / 60000), s = Math.floor((ms % 60000) / 1000);
            return m + 'm' + String(s).padStart(2, '0') + 's';
        }
        function timeBadge(elapsedMs, deltaMs) {
            const delta = (deltaMs >= 50)
                ? '<span class="step-time-delta">Δ' + fmtElapsed(deltaMs) + '</span>' : '';
            return '<span class="step-time" title="Elapsed since start · delta from previous step">+'
                   + fmtElapsed(elapsedMs) + delta + '</span>';
        }
        function ensureCard() {
            if (bodyEl) return;
            removeTyping();
            hideWelcome();
            cardId = 'agent-process-' + Date.now();
            const html =
                '<div class="agent-process-card">' +
                  '<div class="agent-process-header" onclick="' +
                    "this.classList.toggle('collapsed');" +
                    "document.getElementById('" + cardId + "').classList.toggle('hidden');" +
                    "var icon=this.querySelector('.toggle-icon');" +
                    "icon.textContent=this.classList.contains('collapsed')?'\\u25BC':'\\u25B2';" +
                  '">Agent Processing Steps' +
                    '<span id="' + cardId + '-summary" style="font-weight:400;font-size:0.75rem;opacity:0.75;margin-left:8px;"></span>' +
                    '<span class="toggle-icon">▲</span>' +
                  '</div>' +
                  '<div class="agent-process-body" id="' + cardId + '"></div>' +
                '</div>';
            const wrapper = document.createElement('div');
            wrapper.className = 'msg-row assistant';
            wrapper.style.maxWidth = '95%';
            wrapper.innerHTML = '<div class="avatar-icon">🤖</div><div style="flex:1;">' + html + '</div>';
            document.getElementById('chat-window').appendChild(wrapper);
            bodyEl = document.getElementById(cardId);
            scrollBottom();
        }
        function appendStep(step) {
            ensureCard();
            steps.push(step);
            const content = step.content || '';
            const role = step.role || 'agent';
            let stepClass = 'step-info', label = '', isDivider = false, stepNum = null;
            if (role === 'token_usage') { stepClass = 'step-token'; label = '📊 Tokens'; }
            else if (role === 'error') { stepClass = 'step-error'; label = '⛔ Error'; }
            else if (/Reasoning Step (\d+)/.test(content)) {
                const m = content.match(/Reasoning Step (\d+)/);
                stepNum = m ? m[1] : steps.length; isDivider = true;
            }
            else if (/🧠.*Thinking/i.test(content)) { stepClass = 'step-thinking'; label = '🧠 Thinking'; }
            else if (/Invoking skill|fetch_filtered_logs/i.test(content)) { stepClass = 'step-skill'; label = '🔬 Skill'; }
            else if (/Tool call|Tool cap|🧭/i.test(content)) { stepClass = 'step-tool'; label = '🧭 Tools'; }
            else if (/Conclusion reached|submit_final_report|✅/i.test(content)) { stepClass = 'step-done'; label = '✅ Done'; }
            else { stepClass = 'step-info'; label = 'ℹ️'; }
            const now = Date.now();
            const elapsedMs = now - startTs, deltaMs = now - lastTs;
            lastTs = now;
            const badge = timeBadge(elapsedMs, deltaMs);
            let html;
            if (isDivider) {
                html = '<div class="agent-step-divider"><span class="step-num">' + stepNum + '</span>Reasoning Step ' + stepNum + badge + '</div>';
            } else {
                html = '<div class="agent-step ' + stepClass + '"><span class="step-label">' + label + '</span>' +
                       '<div class="agent-step-content">' + marked.parse(content) + '</div>' + badge + '</div>';
            }
            bodyEl.insertAdjacentHTML('beforeend', html);
            const skillsUsed = steps.filter(s => s.content && s.content.includes('Invoking skill'))
                .map(s => { const m = s.content.match(/`([^`]+)`/); return m ? m[1] : ''; }).filter(Boolean);
            const summaryEl = document.getElementById(cardId + '-summary');
            if (summaryEl) {
                summaryEl.textContent = steps.length + ' steps · ' + fmtElapsed(elapsedMs) +
                    (skillsUsed.length ? ' · Skills: ' + skillsUsed.join(', ') : '');
            }
            scrollBottom();
        }
        return {
            appendStep,
            get cardId() { return cardId; },
            get steps() { return steps; },
        };
    }

    // Reconnect to a conversation's live analysis and follow it to done/error,
    // rendering identically to a live send. /history/stream replays the whole
    // step buffer (snapshotted atomically at subscribe time, so nothing is
    // missed) and then streams new steps — we do NOT pre-render here, otherwise
    // the replayed steps would be drawn twice.
    async function followJobStream(conversationId) {
        _abortActiveStream();
        const ctl = new AbortController();
        window.__streamCtl = ctl;
        const card = __makeStepCard();

        const processLine = (line) => {
            if (!line.startsWith('data:')) return;
            const jsonStr = line.slice(5).trim();
            if (!jsonStr) return;
            let evt;
            try { evt = JSON.parse(jsonStr); } catch (e) { return; }
            // Superseded stream → stop rendering immediately (prevents a
            // backgrounded analysis from lagging the new session's typing).
            if (window.__streamCtl !== ctl) return;
            if (evt.type === 'step') {
                card.appendStep(evt.step);
            } else if (evt.type === 'done') {
                // If this stream was superseded (user opened another
                // conversation / resumed a draft / started a new session),
                // a late 'done' must NOT rebind the global conversation id or
                // render into the now-different chat window — doing so corrupts
                // the current draft's context. Bail out; the job is already
                // persisted server-side and the sidebar poll will reflect it.
                if (window.__streamCtl !== ctl) return;
                removeTyping();
                const result = evt.result;
                const turnId = evt.turn_id || null;
                if (evt.conversation_id) window.__feedbackConversationId = evt.conversation_id;
                if (turnId) captureTurnContext(turnId, card.steps);
                if (card.cardId) collapseAgentProcessCard(card.cardId);
                if (result && result.type === 'report') {
                    appendReport(result.data, '📊 Analysis Report', result.issue_time || null, turnId);
                } else if (result && result.type === 'partial_report' && result.data) {
                    appendReport(result.data, '⚠️ Partial Analysis (Step Limit)', result.issue_time || null, turnId);
                } else if (result && result.type === 'text') {
                    appendAssistantText(result.data || '_No response._', turnId);
                } else if (result) {
                    appendAssistantText('⚠️ ' + (result.data || 'Unknown response.'), turnId);
                }
                if (typeof loadHistoryList === 'function') loadHistoryList();
            } else if (evt.type === 'error') {
                if (window.__streamCtl !== ctl) return;
                removeTyping();
                appendAssistantText('❌ Error: ' + (evt.content || 'Unknown error'));
                if (typeof loadHistoryList === 'function') loadHistoryList();
            }
            // evt.type === 'idle' → nothing live to follow; saved turns already render.
        };

        try {
            const res = await fetch(`${CHATBOT_API}/history/stream?conversation_id=` + encodeURIComponent(conversationId),
                                    { signal: ctl.signal });
            if (!res.ok || !res.body) return;
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
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
            if (e.name !== 'AbortError') console.warn('[history] stream failed:', e);
        } finally {
            if (window.__streamCtl === ctl) window.__streamCtl = null;
        }
    }

    // Load a saved conversation: re-render its turns AND resume it server-side
    // (restore the log file + context) so follow-up questions keep working.
    // If the analysis is still running, reconnect to its live stream.
    async function openHistoryConversation(id) {
        // Stop rendering whatever stream is currently on screen (its server
        // job, if any, keeps running and can be re-attached to later).
        _abortActiveStream();
        // Preserve the chat-box draft of the conversation we're leaving so it
        // reappears when we switch back to it (kept per-conversation in memory).
        saveCurrentDraft();
        // Snapshot the unsent draft when leaving the live session, but only if
        // no question has been sent yet — "↩ Resume my draft" can then restore
        // the log / issue time / chat-box text the user was preparing (e.g.
        // carried in from the case-number flow, or just loaded/typed after a
        // New session). We re-snapshot EVERY time we leave the live session
        // (not just once) so the latest log + issue-time + draft are captured;
        // we skip it only when hopping between already-saved conversations
        // (__viewingSavedConv), which must not clobber the live snapshot.
        if (!firstRoundSent && !window.__viewingSavedConv) captureLiveSessionSnapshot();
        try {
            const res = await fetch(`${CHATBOT_API}/history/load`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({conversation_id: id}),
            });
            const data = await res.json();
            if (!data || !data.success) {
                if (typeof showToast === 'function') {
                    showToast({message: '⚠️ Could not load conversation.', ttlMs: 4000});
                }
                return;
            }

            // Rebuild the chat window from scratch.
            const cw = document.getElementById('chat-window');
            cw.innerHTML = '';
            window.__feedbackConversationId = data.conversation_id || id;
            window.__turnContext = {};
            window.__lastFeedback = {};
            renderHistoryTurns(data.turns);
            // We're now viewing a saved conversation — clicking another saved
            // conversation must not re-snapshot (and overwrite) the live one.
            window.__viewingSavedConv = true;

            // Restore log / skills / issue-time context so the user can continue.
            const st = document.getElementById('log-status');
            if (data.log_exists) {
                const inp = document.getElementById('log-path-input');
                if (inp) inp.value = data.log_path || '';
                logLoaded = true;
                if (data.skills) renderSkills(data.skills);
                window.__logHasDate = (data.log_has_date !== false);
                window.__logSpanMinutes = (typeof data.log_span_minutes === 'number'
                                           && data.log_span_minutes > 0)
                                          ? data.log_span_minutes : null;
                if (typeof refreshIssueWindowBounds === 'function') refreshIssueWindowBounds();
                // Repopulate the sidebar issue time so a follow-up message
                // re-uses the same anchor instead of clearing it.
                if (data.issue_time && typeof setIssueTimeFromString === 'function') {
                    setIssueTimeFromString(data.issue_time);
                }
                if (st) showStatus(st, data.running
                    ? '⏳ Analysis in progress — following live…'
                    : '✔ Resumed conversation — log re-loaded.',
                    data.running ? 'warn' : 'ok',
                    // Keep the "in progress" line up while it streams; the
                    // "resumed" confirmation is transient — auto-hide after 3s.
                    data.running ? 0 : 3000);
            } else if (data.log_path) {
                if (st) showStatus(st, '⚠ Original log not found — viewing history only.', 'warn');
            }

            // Restore this conversation's own unsent draft (chat-box text +
            // Issue Time + log source). Called AFTER the server-side restore so
            // any unsent edits the user made before switching away win.
            await restoreDraftFor(window.__feedbackConversationId);

            // Still analysing → show the in-flight question and follow the
            // live stream so progress catches up in real time.
            if (data.running) {
                hideWelcome();
                if (data.running_user_message) appendUserMsg(data.running_user_message);
                // No pre-render: /history/stream replays the full step buffer.
                followJobStream(data.conversation_id || id);
            }

            // Refresh the list so the active item highlights.
            loadHistoryList();
        } catch (e) {
            console.warn('[history] load failed:', e);
        }
    }

    async function deleteHistoryConversation(id, ev) {
        if (ev) ev.stopPropagation();
        if (!confirm('Delete this conversation? This cannot be undone.')) return;
        try {
            const res = await fetch(`${CHATBOT_API}/history/delete`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({conversation_id: id}),
            });
            const data = await res.json();
            if (data && data.success) {
                // If we deleted the conversation currently on screen, clear it.
                if ((window.__feedbackConversationId || '') === id) {
                    window.__feedbackConversationId = '';
                    const cw = document.getElementById('chat-window');
                    if (cw) cw.innerHTML =
                        '<div class="welcome-msg" id="welcome-msg">' +
                        '<div class="big-icon">🗑️</div>' +
                        '<strong>Conversation deleted.</strong><br>' +
                        'Pick another from History, or load a log to start a new one.' +
                        '</div>';
                }
                loadHistoryList();
            }
        } catch (e) {
            console.warn('[history] delete failed:', e);
        }
    }

    // Populate the list on first load so it's ready when the user flips tabs.
    document.addEventListener('DOMContentLoaded', loadHistoryList);
