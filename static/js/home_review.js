(() => {
    'use strict';

    const element = id => document.getElementById('home-' + id);
    const modal = $('#home-draft-modal');
    const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g,
        character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]));
    const publicModes = ['request_logs', 'request_info', 'first_response'];
    const actionOwners = {intel: 'Intel (case owner)', customer: 'Customer'};
    let login = {status: 'signed_out', identity: null};
    let draft = null;
    let savedText = '';
    let busy = false;
    let allowClose = false;
    let queueRequest = 0;
    let openingDraft = false;
    let opener = null;
    let queueLocked = false;
    let handoffBusy = false;
    let folderBusy = false;
    let avatarUnavailable = false;

    function setQueueLocked(locked) {
        queueLocked = locked;
        const dashboard = element('queue').closest('.home-review-unframed');
        dashboard.inert = locked;
        dashboard.setAttribute('aria-disabled', String(locked));
        dashboard.classList.toggle('is-locked', locked);
        const scroll = element('queue').closest('.review-scroll');
        scroll.inert = locked;
        scroll.setAttribute('aria-disabled', String(locked));
        scroll.classList.toggle('is-locked', locked);
        element('queue').querySelectorAll('tbody tr[data-draft]').forEach(row => {
            row.tabIndex = locked ? -1 : 0;
            if (locked) row.setAttribute('aria-disabled', 'true');
            else row.removeAttribute('aria-disabled');
        });
    }

    // An approval is not an ordinary request: the server may renew the IPS
    // sign-in or fall back to the browser-driven post, which takes minutes,
    // and giving up on the wait does NOT stop it. So it gets its own, longer
    // wait, and when no answer arrives the outcome is read back from the
    // queue before anything is unlocked (see approveDraft).
    const timing = Object.assign({requestMs: 45000, approveMs: 180000, reconcileMs: 120000, pollMs: 5000},
        window.homeReviewTiming || {});
    const closedStatuses = ['posted', 'rejected', 'unconfirmed'];

    async function request(url, method = 'GET', body, timeoutMs = timing.requestMs) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), timeoutMs);
        try {
            const response = await fetch('/handsfree/' + url, {
                method, signal: controller.signal,
                ...(body !== undefined ? {
                    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
                } : {}),
            });
            const result = await response.json();
            if (!response.ok || result.error || result.ok === false) {
                // The server answered: whatever it was doing is over.
                const refused = new Error(result.error || 'Request failed (' + response.status + ')');
                refused.answered = true;
                throw refused;
            }
            return result;
        } finally {
            clearTimeout(timeout);
        }
    }

    // A token naming one click on Approve. The server records it on the
    // draft with that attempt's state and result (approve_attempt).
    function newAttemptId() {
        const random = window.crypto?.randomUUID?.() ||
            (Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12));
        return ('a-' + random).replace(/[^A-Za-z0-9_-]/g, '').slice(0, 64);
    }

    // What became of an approval that never answered: 'posted', 'failed'
    // (with the server's error) or 'unknown'. Only THIS attempt's own record
    // counts. The draft's status and post_result are not evidence: when a
    // post_failed draft is approved again they keep describing the earlier
    // attempt until the retry finishes, and reading that stale failure as
    // this one's would unlock the draft while the retry is still posting.
    // No record of this attempt (the server has not reached it, or never
    // got the request) and "still running" both mean: keep waiting.
    async function approvalOutcome(draftId, attemptId) {
        const deadline = Date.now() + timing.reconcileMs;
        for (;;) {
            try {
                const record = await request('queue/' + encodeURIComponent(draftId));
                const attempt = record.approve_attempt || {};
                if (attempt.id === attemptId && attempt.state === 'done') {
                    return attempt.ok ? {status: 'posted'}
                        : {status: 'failed', error: attempt.error || 'the post failed'};
                }
            } catch (error) { /* the server is busy or briefly unreachable: ask again */ }
            if (Date.now() >= deadline) return {status: 'unknown'};
            await new Promise(resolve => setTimeout(resolve, timing.pollMs));
        }
    }

    function caseLink(number, caseId) {
        const id = String(caseId || '');
        if (!/^500[A-Za-z0-9]{12}([A-Za-z0-9]{3})?$/.test(id)) return escapeHtml(number);
        return '<a href="https://intel.lightning.force.com/lightning/r/Case/' + id +
            '/view" target="_blank" rel="noopener" title="Open case in IPS">' + escapeHtml(number) + '</a>';
    }

    function pill(text, style = '', title = '') {
        return '<span class="review-pill ' + style + '" title="' + escapeHtml(title) + '">' + escapeHtml(text) + '</span>';
    }

    function renderLogin() {
        const signedIn = login.status === 'signed_in' && login.identity;
        element('login-badge').textContent = signedIn ? 'IPS: ' + login.identity.name
            : login.status === 'signing_in' ? 'IPS: signing in...' : 'IPS: not signed in';
        element('login-badge').className = 'review-pill ' + (signedIn ? 'good' : 'warn');
        element('login-badge').title = login.error || (signedIn ? 'Comments post as ' + login.identity.name : '');
        element('login').hidden = signedIn || login.status === 'signing_in';
    }

    async function pollLogin() {
        try {
            login = await request('login');
            renderLogin();
            if (login.status === 'signing_in') setTimeout(pollLogin, 2000);
            else await loadQueue();
        } catch (error) {
            login = {status: 'signed_out', error: String(error)};
            renderLogin();
            element('queue-message').textContent = 'IPS login unavailable: ' + error;
        }
    }

    async function signIn(interactive) {
        element('login').disabled = true;
        try {
            login = await request('login', 'POST', {interactive});
            renderLogin();
            await loadQueue();
            if (login.status === 'signing_in') setTimeout(pollLogin, 2000);
        } catch (error) {
            login = {status: 'signed_out', error: String(error)};
            renderLogin();
            await loadQueue();
            element('queue-message').textContent = 'IPS login unavailable: ' + error;
        } finally {
            element('login').disabled = false;
        }
    }

    async function loadQueue() {
        if (queueLocked) return;
        const sequence = ++queueRequest;
        const params = new URLSearchParams();
        if (element('show-closed').checked) params.set('all', '1');
        if (element('everyone').checked) {
            params.set('everyone', '1');
            const owner = element('owner').value.trim();
            if (owner) params.set('owner', owner);
        }
        element('queue-message').textContent = 'Loading cases...';
        try {
            const result = await request('queue' + (params.size ? '?' + params : ''));
            if (sequence !== queueRequest) return;
            const tbody = element('queue').querySelector('tbody');
            tbody.replaceChildren();
            element('queue-message').textContent = result.signed_out
                ? (login.status === 'signing_in' ? 'Signing in to IPS...'
                    : 'Sign in to IPS to view your cases, or select everyone\'s cases.')
                : (result.items || []).length + ' cases';
            for (const item of result.items || []) {
                const row = document.createElement('tr');
                const skipped = item.status === 'skipped';
                const statusStyle = item.status === 'posted' ? 'good'
                    : ['post_failed', 'rejected'].includes(item.status) ? 'bad'
                    : item.status === 'pending_review' ? 'warn' : '';
                row.innerHTML = '<td>' + caseLink(item.case_nbr, item.case_id) + '</td><td>' +
                    escapeHtml(item.owner_name || '—') + '</td><td>' + escapeHtml(item.subject || '') + '</td><td>' + pill(item.status, statusStyle,
                        skipped ? item.skip_reason : '') + '</td>';
                row.querySelector('a')?.addEventListener('click', event => {
                    if (queueLocked) event.preventDefault();
                    event.stopPropagation();
                });
                if (!skipped && item.draft_id) {
                    row.dataset.draft = item.draft_id;
                    row.tabIndex = queueLocked ? -1 : 0;
                    row.setAttribute('aria-label', 'Review case ' + item.case_nbr);
                    if (queueLocked) row.setAttribute('aria-disabled', 'true');
                    row.addEventListener('click', () => {
                        if (!queueLocked) openDraft(item, row);
                    });
                    row.addEventListener('keydown', event => {
                        if (!queueLocked && event.target === row && ['Enter', ' '].includes(event.key)) {
                            event.preventDefault();
                            openDraft(item, row);
                        }
                    });
                }
                tbody.appendChild(row);
            }
            if (!tbody.children.length) {
                tbody.innerHTML = '<tr><td colspan="4">' +
                    (result.signed_out ? 'No cases loaded.' : 'Queue is empty.') + '</td></tr>';
            }
        } catch (error) {
            if (sequence === queueRequest) element('queue-message').textContent = 'Could not refresh queue: ' + error;
        }
    }

    function renderChips(analysis) {
        const chips = [];
        const add = (label, text, style, title) => chips.push(pill(label + ': ' + text, style, title));
        const reader = analysis.case_reader || {};
        if (!draft.email) add('visibility', draftVisibility() === 'external' ? 'PUBLIC reply to customer' : 'Private to Intel',
            draftVisibility() === 'external' ? 'warn' : '');
        if (draft.trial) add('mode', 'TRIAL - posting disabled', 'warn');
        if (draft.auto_posted) add('posting', 'AUTO');
        if (analysis.next_action) add('next action', (actionOwners[analysis.action_owner] || 'owner unclear') +
            ' - ' + analysis.next_action, 'next-action ' + (analysis.action_owner === 'intel' ? 'warn' : ''));
        const images = analysis.comment_images || [];
        if (images.length) add('images', images.reduce((total, image) => total + (image.descriptions || []).length, 0) +
            ' screenshots read', 'good', images.map(image => (image.date || '') + ' ' + (image.author || '') +
                ': ' + (image.descriptions || []).join(' | ')).join('\n'));
        if (analysis.issue_domain) add('domain', analysis.issue_domain +
            (analysis.issue_subcategory && analysis.issue_subcategory !== analysis.issue_domain ? ' / ' + analysis.issue_subcategory : ''));
        const missing = analysis.missing_info || [];
        if (missing.length) add('case info', missing.length + ' items missing', 'warn',
            missing.map(item => item.item + ': ' + (item.reason || '')).join(' / '));
        const mismatch = analysis.time_mismatch || {};
        if (mismatch.log_first) add('time check', 'log does NOT cover issue time', 'bad',
            'Reported: ' + (mismatch.issue_times || []).join(', ') + '; log: ' + mismatch.log_first +
            ' - ' + mismatch.log_last + '; grace: ' + (mismatch.grace_minutes || 10) + ' min');
        const times = reader.issue_times?.length ? reader.issue_times : (analysis.issue_times || []);
        if (times.length) add('issue time', times.join(' / '), '', reader.issue_time_source);
        if (analysis.chosen_attachment) add('log', analysis.chosen_attachment, '', reader.attachment_reason);
        const incidents = (analysis.incidents || []).filter(incident => typeof incident.report?.confidence_score === 'number');
        const best = incidents.reduce((selected, incident) => !selected ||
            incident.report.confidence_score > selected.report.confidence_score ? incident : selected, null);
        if (best) {
            const confidence = best.report.confidence_score;
            add('confidence', confidence + '/100', confidence >= 80 ? 'good' : confidence >= 50 ? 'warn' : 'bad');
            (best.report.involved_skills || []).forEach(skill => add('skill', skill));
        }
        const stages = analysis.stages || [];
        if (stages.length) {
            const passed = stages.filter(stage => stage.ok).length;
            add('stages', passed + '/' + stages.length + ' OK', passed === stages.length ? 'good' : 'warn');
        }
        const insights = (analysis.echo_insights || []).filter(insight => insight?.answer);
        if (insights.length) add('echo kb', insights.length + ' root-cause insights', 'good',
            insights.map(insight => insight.kind === 'assert' ? 'assert ' + insight.code : 'yellow bang').join(' / '));
        element('draft-chips').innerHTML = chips.join('');
    }

    function draftVisibility() {
        return element('visibility-external').checked ? 'external' : 'internal';
    }

    function setPostStatus(message, state = '') {
        element('post-status').textContent = message;
        element('post-status').dataset.state = state;
        element('post-close').hidden = state !== 'success';
    }

    function updateVisibility() {
        const external = draftVisibility() === 'external' && !draft?.email;
        element('visibility').hidden = !!draft?.email;
        element('visibility-note').textContent = external ? 'The customer will see this comment.' : '';
        element('approve').textContent = draft?.email ? 'Save reply draft in Outlook' : 'Approve & post to IPS';
        element('approve').title = draft?.email
            ? 'Saves a reply in Outlook Drafts; nothing is sent'
            : external ? 'Posts a comment visible to the customer' : 'Posts a Private-to-Intel comment';
        element('approve').classList.toggle('btn-danger', external);
        element('approve').classList.toggle('btn-success', !external);
        if (draft) renderChips(draft.analysis || {});
    }

    function updateButtons() {
        const closed = !draft || closedStatuses.includes(draft.status);
        element('visibility').disabled = busy || closed || !!draft?.trial;
        element('approve').disabled = busy || closed || !!draft?.trial;
        element('save').disabled = busy || closed;
        element('reject').disabled = busy || closed;
        element('folder').disabled = busy || folderBusy || !draft;
        element('post-close').disabled = busy;
        const hasLog = !!(draft?.shared_log_path || draft?.analysis?.log_path) && !avatarUnavailable;
        element('avatar').disabled = busy || handoffBusy || !hasLog;
        element('avatar').title = hasLog
            ? "Opens Avatar's analysis chatbot (Wi-Fi or Bluetooth, as the case was analysed) on this case's decoded log, with the issue filled in the chat, to continue by hand"
            : 'no log to analysis';
        element('draft-text').readOnly = busy || closed;
        ['save-close', 'discard', 'continue'].forEach(id => { element(id).disabled = busy; });
    }

    // The reviewer's text differs from what the queue holds. A posted or
    // rejected draft is read-only, so it is never dirty.
    function isDirty() {
        return !!draft && !closedStatuses.includes(draft.status) &&
            element('draft-text').value !== savedText;
    }

    // PUT the reviewer's text to the queue. Returns true when it was saved.
    // The server may normalise it (it prepends the AI marker when missing),
    // so the box and savedText take what was actually stored.
    async function saveDraft() {
        if (!draft || busy || closedStatuses.includes(draft.status)) return false;
        busy = true;
        updateButtons();
        setPostStatus('Saving edits...');
        try {
            const result = await request('queue/' + encodeURIComponent(draft.draft_id), 'PUT', {
                draft_plain: element('draft-text').value,
            });
            savedText = result.draft?.draft_plain ?? element('draft-text').value;
            element('draft-text').value = savedText;
            element('unsaved').hidden = true;
            setPostStatus('Edits saved. Nothing was posted.');
            return true;
        } catch (error) {
            setPostStatus('Could not save edits: ' + error, 'error');
            return false;
        } finally {
            busy = false;
            updateButtons();
        }
    }

    async function rejectDraft() {
        if (!draft || element('reject').disabled) return;
        const reason = window.prompt('Reject this draft? Nothing will be posted.\n\nReason (optional):', '');
        if (reason === null) return;                       // Cancel
        busy = true;
        updateButtons();
        setPostStatus('Rejecting draft...');
        try {
            await request('queue/' + encodeURIComponent(draft.draft_id) + '/reject', 'POST', {reason});
            savedText = element('draft-text').value;      // nothing left to save
            draft.status = 'rejected';
            allowClose = true;
            element('unsaved').hidden = true;
            setPostStatus('Draft rejected; nothing was posted. Please close this window.', 'success');
            await loadQueue();
        } catch (error) {
            setPostStatus('Could not reject draft: ' + error, 'error');
        } finally {
            busy = false;
            updateButtons();
        }
    }

    // Escape, the backdrop, × and "Close window" all end up here. A request
    // in flight (approve, save, reject) must finish first; unsaved edits ask
    // before they are dropped.
    function onModalHide(event) {
        if (busy) {
            event.preventDefault();
            setPostStatus('Please wait - the request is still running.', 'posting');
            return;
        }
        if (allowClose || !isDirty()) return;
        event.preventDefault();
        element('unsaved').hidden = false;
        element('unsaved').scrollIntoView({block: 'nearest'});
        element('save-close').focus();
    }

    function onModalHidden() {
        allowClose = false;
        element('unsaved').hidden = true;
        if (opener && document.contains(opener)) opener.focus();
        opener = null;
    }

    function closeModal() {
        allowClose = true;
        modal.modal('hide');
    }

    async function openDraft(item, row) {
        if (queueLocked || openingDraft || busy) return;
        openingDraft = true;
        try {
            const record = await request('queue/' + encodeURIComponent(item.draft_id));
            draft = record;
            avatarUnavailable = false;
            element('visibility-internal').checked = true;
            element('visibility-external').checked = false;
            opener = row;
            allowClose = false;
            savedText = record.draft_plain || '';
            element('draft-title').innerHTML = 'Draft review - ' + caseLink(record.case_nbr, record.case_id);
            const posted = record.post_result || {};
            const postedAs = posted.author?.name || posted.posted_by?.name || record.posted_by || '';
            element('draft-meta').textContent = [item.subject, record.owner_name, record.mode, record.status,
                posted.error ? 'Last post attempt: ' + posted.error : postedAs ? 'Posted as ' + postedAs : ''].filter(Boolean).join(' / ');
            element('draft-text').value = savedText;
            element('draft-analysis').textContent = JSON.stringify(record.analysis || {}, null, 2);
            element('analysis-details').open = false;
            element('folder').hidden = !item.log_dir;
            element('folder').title = item.log_dir || '';
            setPostStatus('');
            element('unsaved').hidden = true;
            updateVisibility();
            updateButtons();
            modal.modal('show');
        } catch (error) {
            element('queue-message').textContent = 'Could not open draft: ' + error;
        } finally {
            openingDraft = false;
        }
    }

    async function approveDraft() {
        if (!draft || element('approve').disabled || handoffBusy) return;
        if (!draft.email && !(login.status === 'signed_in' && login.identity)) {
            setPostStatus('Sign in to IPS before posting.', 'error');
            return;
        }
        const visibility = draftVisibility();
        const message = draft.email
            ? 'Save this reply in Outlook Drafts? Nothing will be sent or posted to IPS.'
            : (visibility === 'external'
                ? 'Post this comment as EXTERNAL, visible to the customer?'
                : 'Post this comment as Internal to Intel? The customer will not see it.')
                + '\n\nPosted as ' + login.identity.name + '.';
        if (!window.confirm(message)) return;
        busy = true;
        updateButtons();
        setPostStatus(draft.email ? 'Saving Outlook reply draft...' : 'Posting to IPS. Please wait...', 'posting');
        const draftId = draft.draft_id;
        const attemptId = newAttemptId();
        try {
            let outcome = {status: 'posted'};
            try {
                await request('queue/' + encodeURIComponent(draftId) + '/approve', 'POST', {
                    draft_plain: element('draft-text').value, visibility, attempt: attemptId,
                }, timing.approveMs);
            } catch (error) {
                if (error.answered) throw error;       // the server refused: nothing is running
                // No answer (the wait ran out, the connection dropped): the
                // server may still be posting. Editing, Reject and a second
                // Approve stay locked until the queue says how it ended.
                setPostStatus('No answer yet - the post may still be running. Checking its result...', 'posting');
                outcome = await approvalOutcome(draftId, attemptId);
            }
            if (outcome.status === 'failed') throw new Error(outcome.error);
            savedText = element('draft-text').value;
            allowClose = true;
            if (outcome.status === 'unknown') {
                // Still pending after the wait: do NOT hand the draft back for
                // editing or rejection while a post may be in flight.
                draft.status = 'unconfirmed';
                setPostStatus('The post did not report back and may still be running. Close this window ' +
                    'and refresh the queue before doing anything else with this draft.', 'error');
                return;
            }
            draft.status = 'posted';
            setPostStatus(draft.email
                ? 'Reply draft saved in Outlook; nothing was sent. Please close this window.'
                : 'Posted to IPS: ' + (visibility === 'external' ? 'visible to the customer' : 'Private to Intel')
                    + '. Please close this window.', 'success');
            await loadQueue();
        } catch (error) {
            setPostStatus('Could not approve draft: ' + error, 'error');
        } finally {
            busy = false;
            updateButtons();
        }
    }

    async function openFolder() {
        if (!draft || element('folder').hidden || element('folder').disabled || folderBusy) return;
        folderBusy = true;
        updateButtons();
        setPostStatus('Opening folder...');
        try {
            await request('queue/' + encodeURIComponent(draft.draft_id) + '/open_log_folder', 'POST');
            setPostStatus('Folder opened in File Explorer.');
        } catch (error) {
            setPostStatus('Could not open folder: ' + error, 'error');
        } finally {
            folderBusy = false;
            updateButtons();
        }
    }

    async function openInAvatar() {
        if (element('avatar').disabled || !draft || handoffBusy) return;
        // This navigates away from the page: unsaved edits would go with it.
        if (isDirty() && !window.confirm('This draft has unsaved changes. Leave without saving them?')) return;
        handoffBusy = true;
        updateButtons();
        setPostStatus('Navigating to new page...', 'navigating');
        try {
            const result = await request('chatbot_handoff', 'POST', {
                case_nbr: draft.case_nbr, draft_id: draft.draft_id || '',
            });
            if (!result.url) throw new Error('Avatar did not return a destination URL.');
            window.location.assign(result.url);
        } catch (error) {
            if (error.message === 'no log to analysis') avatarUnavailable = true;
            setPostStatus('Could not open in Avatar: ' + error, 'error');
        } finally {
            handoffBusy = false;
            updateButtons();
        }
    }

    window.addEventListener('home:submission-state', event => {
        const wasLocked = queueLocked;
        setQueueLocked(!!event.detail?.locked);
        if (wasLocked && !queueLocked) loadQueue();
    });
    element('queue').closest('.home-review-unframed').addEventListener('click', event => {
        if (!queueLocked) return;
        event.preventDefault();
        event.stopImmediatePropagation();
    }, true);
    if (window.homeSubmissionLocked) setQueueLocked(true);

    element('folder').addEventListener('click', openFolder);
    element('avatar').addEventListener('click', openInAvatar);
    element('approve').addEventListener('click', approveDraft);
    element('save').addEventListener('click', saveDraft);
    element('reject').addEventListener('click', rejectDraft);
    element('save-close').addEventListener('click', async () => {
        if (await saveDraft()) closeModal();
    });
    element('discard').addEventListener('click', closeModal);
    element('continue').addEventListener('click', () => {
        element('unsaved').hidden = true;
        element('draft-text').focus();
    });
    modal.on('hide.bs.modal', onModalHide);
    modal.on('hidden.bs.modal', onModalHidden);
    element('visibility-internal').addEventListener('change', updateVisibility);
    element('visibility-external').addEventListener('change', updateVisibility);
    element('login').addEventListener('click', () => signIn(true));
    element('refresh').addEventListener('click', loadQueue);
    element('show-closed').addEventListener('change', loadQueue);
    element('everyone').addEventListener('change', () => {
        element('owner-wrap').hidden = !element('everyone').checked;
        loadQueue();
    });
    element('owner').addEventListener('change', loadQueue);
    signIn(false);
})();
