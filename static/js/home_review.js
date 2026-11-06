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

    function setQueueLocked(locked) {
        queueLocked = locked;
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

    async function request(url, method = 'GET', body) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 45000);
        try {
            const response = await fetch('/handsfree/' + url, {
                method, signal: controller.signal,
                ...(body !== undefined ? {
                    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
                } : {}),
            });
            const result = await response.json();
            if (!response.ok || result.error || result.ok === false) {
                throw new Error(result.error || 'Request failed (' + response.status + ')');
            }
            return result;
        } finally {
            clearTimeout(timeout);
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
        add('visibility', publicModes.includes(draft.mode) ? 'PUBLIC reply to customer' : 'Private to Intel',
            publicModes.includes(draft.mode) ? 'warn' : '');
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

    function updateButtons() {
        const closed = !draft || ['posted', 'rejected'].includes(draft.status);
        element('approve').disabled = busy || closed || !!draft?.trial;
        element('save').disabled = busy || closed;
        element('reject').disabled = busy || closed;
        element('folder').disabled = busy;
        element('avatar').disabled = busy || handoffBusy || !draft;
        element('draft-text').readOnly = busy || closed;
        ['save-close', 'discard', 'continue'].forEach(id => { element(id).disabled = busy; });
    }

    async function openDraft(item, row) {
        if (queueLocked || openingDraft || busy) return;
        openingDraft = true;
        try {
            const record = await request('queue/' + encodeURIComponent(item.draft_id));
            draft = record;
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
            element('post-status').textContent = '';
            element('unsaved').hidden = true;
            renderChips(record.analysis || {});
            updateButtons();
            modal.modal('show');
        } catch (error) {
            element('queue-message').textContent = 'Could not open draft: ' + error;
        } finally {
            openingDraft = false;
        }
    }
            });
            if (!result.url) throw new Error('Avatar did not return a destination URL.');
            window.location.assign(result.url);
        } catch (error) {
            element('post-status').textContent = 'Could not open in Avatar: ' + error;
        } finally {
            handoffBusy = false;
            updateButtons();
        }
    }
