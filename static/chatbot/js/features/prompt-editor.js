// Prompt editor: read, edit, switch and publish the prompts that decide what
// this agent is an expert in.
//
// The server owns every rule (which documents this page may touch, which
// source each one reads, what happens on save). This file is the panel: it
// renders what /prompts/status reports and sends the four actions back. Kept
// deliberately small next to skill-editor.js — a prompt is one text area, not
// a structured form.
(function () {
    'use strict';

    // Same source every feature script reads its prefix from (see core.js).
    const API = (window.CHATBOT && window.CHATBOT.api) || '';
    let docs = [];          // from /prompts/status
    let current = null;     // the selected document's id, e.g. "linux_prompt"
    let loaded = {};        // the four texts of the selected document

    const el = (id) => document.getElementById(id);
    const status = (msg, kind) => {
        const node = el('prompt-editor-status');
        if (!node) return;
        node.textContent = msg || '';
        node.style.color = kind === 'err' ? '#b42318'
            : kind === 'ok' ? '#067647' : '#555';
    };

    // A document's live source, said the way a person would.
    const sourceLabel = (source) => ({
        local: 'your copy',
        shared: 'the shared folder',
        'built-in': 'the text this agent ships with',
    }[source] || source);

    async function call(path, options) {
        const res = await fetch(API + path, options);
        let data = {};
        try { data = await res.json(); } catch (e) { /* non-JSON error page */ }
        if (!res.ok || data.success === false) {
            throw new Error(data.error || `${res.status} ${res.statusText}`);
        }
        return data;
    }

    function renderTabs() {
        const wrap = el('prompt-doc-tabs');
        if (!wrap) return;
        wrap.innerHTML = '';
        docs.forEach((doc) => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'prompt-doc-tab' + (doc.doc === current ? ' active' : '');
            // "prompt" / "report" for this agent; the engine's own documents
            // keep their full name so they cannot be mistaken for this one's.
            // The bundle is named for what it is rather than for its kind.
            btn.textContent = doc.shared_across_profiles
                ? doc.doc.replace('_', ' ')
                : (doc.kind === 'agent' ? 'all three, one file' : doc.kind);
            if (doc.source === 'local') {
                const dot = document.createElement('span');
                dot.className = 'prompt-doc-dot';
                dot.title = 'Reading your own copy';
                btn.appendChild(dot);
            }
            btn.onclick = () => selectDoc(doc.doc);
            wrap.appendChild(btn);
        });
    }

    function renderMeta() {
        const doc = docs.find((d) => d.doc === current);
        const meta = el('prompt-doc-meta');
        if (!doc || !meta) return;
        const bits = [`<strong>${doc.filename}</strong>`,
                      `live: ${sourceLabel(doc.source)}`];
        // Otherwise a single-document file sitting next to a bundle that
        // outranks it looks like the file in use, and it is not.
        if (doc.via_bundle) bits.push('text comes from the one-file bundle');
        if (doc.has_local) {
            bits.push(`your copy saved ${doc.local_mtime || 'earlier'}`);
        }
        if (doc.has_shared) {
            bits.push(`shared copy ${doc.shared_mtime || ''}`.trim());
        }
        meta.innerHTML = bits.join(' &nbsp;·&nbsp; ');

        const warn = el('prompt-shared-warning');
        if (warn) warn.style.display = doc.shared_across_profiles ? 'block' : 'none';
        // Nothing to revert or upload without a copy of your own.
        const revert = el('prompt-revert-btn');
        const upload = el('prompt-upload-btn');
        if (revert) revert.disabled = !doc.has_local;
        if (upload) upload.disabled = !doc.has_local;
    }

    async function selectDoc(docId) {
        current = docId;
        renderTabs();
        renderMeta();
        status('Loading…');
        try {
            loaded = await call(`/prompts/get?doc=${encodeURIComponent(docId)}`);
            el('prompt-editor-text').value = loaded.live || '';
            el('prompt-editor-file').textContent = loaded.filename || '';
            status('');
        } catch (e) {
            status(e.message, 'err');
        }
    }

    async function refreshStatus(keepSelection) {
        const data = await call('/prompts/status');
        docs = data.documents || [];
        if (!keepSelection || !docs.some((d) => d.doc === current)) {
            current = docs.length ? docs[0].doc : null;
        }
        renderTabs();
        renderMeta();
    }

    window.openPromptEditor = async function () {
        const modal = el('prompt-editor-modal');
        if (!modal) return;
        modal.classList.add('open');
        modal.setAttribute('aria-hidden', 'false');
        const advanced = el('prompt-advanced');
        if (advanced) advanced.open = false;
        status('Loading…');
        try {
            await refreshStatus(false);
            if (current) await selectDoc(current);
        } catch (e) {
            status(e.message, 'err');
        }
    };

    window.closePromptEditor = function () {
        const modal = el('prompt-editor-modal');
        if (!modal) return;
        modal.classList.remove('open');
        modal.setAttribute('aria-hidden', 'true');
    };

    // Browsers deliberately expose only the chosen file's contents, never its
    // path. Loading is a preview: the existing Save & use action performs the
    // same server-side bundle validation as a hand-edited prompt.
    window.choosePromptFile = async function (input) {
        const file = input.files && input.files[0];
        if (!file) return;
        input.value = ''; // choosing the same file again should still fire change
        if (!/\.md$/i.test(file.name) || file.size > 1024 * 1024) {
            status('Choose a .md file smaller than 1 MB.', 'err');
            return;
        }
        const bundle = docs.find((d) => d.kind === 'agent' && !d.shared_across_profiles);
        if (!bundle) {
            status('This agent has no three-section prompt document.', 'err');
            return;
        }
        try {
            const content = (await file.text()).replace(/^\uFEFF/, '');
            if (!/^##\s+prompt\s*$/im.test(content) ||
                !/^##\s+report\s*$/im.test(content) ||
                !/^##\s+suggest_time\s*$/im.test(content)) {
                status('The file needs ## prompt, ## report and ## suggest_time sections.', 'err');
                return;
            }
            if (current !== bundle.doc) await selectDoc(bundle.doc);
            el('prompt-editor-text').value = content;
                status(`Loaded ${file.name} into the editor — review it, then Save & use.`, 'ok');
        } catch (e) {
            status(`Could not read ${file.name}: ${e.message}`, 'err');
        }
    };

    // Swap the editor's contents for another source, without saving: this is
    // how you start from the shared text or from the built-in one.
    window.loadPromptText = function (which) {
        const text = loaded[which];
        if (!text) {
            status(`There is no ${which === 'builtin' ? 'built-in' : which} copy of this prompt.`, 'err');
            return;
        }
        el('prompt-editor-text').value = text;
        status(`Loaded the ${which === 'builtin' ? 'built-in' : which} text — not saved yet.`);
    };

    // Changing the prompt is only half of retargeting an agent: the prompt
    // decides what it looks for, the skills decide which lines it is given to
    // look at. A new domain almost always needs both, so the saved prompt
    // offers the other half — but the panel has just closed, so the offer is
    // a toast rather than a banner inside it. Its action switches the skills;
    // dismissing it (or letting it expire) keeps them, which is the right
    // default: tuning the wording of one prompt needs no skill change.
    function offerSkillSwap(lead, offerSkills) {
        const canSwitch = offerSkills && typeof appendSkillsFromYaml === 'function';
        if (typeof showToast !== 'function') return;
        showToast({
            message: canSwitch
                ? `${lead} The skills decide which log lines it reads — switch those too?`
                : lead,
            actionLabel: canSwitch ? 'Switch skills…' : undefined,
            onAction: canSwitch ? switchSkillsFromPrompt : undefined,
            // Long enough to be a decision rather than something to catch.
            ttlMs: canSwitch ? 45000 : 12000,
        });
    }

    async function switchSkillsFromPrompt() {
        await appendSkillsFromYaml();
        // appendSkillsFromYaml reports into the sidebar's status line; say the
        // outcome where the person is looking instead.
        const sidebar = document.getElementById('skill-editor-status');
        const said = (sidebar && sidebar.textContent || '').trim();
        if (typeof showToast === 'function') {
            showToast({ message: said || 'Skills unchanged.', ttlMs: 12000 });
        }
    }
    window.switchSkillsFromPrompt = switchSkillsFromPrompt;

    // Only the document that decides what the agent IS. Saving a report
    // skeleton, or one of the engine's shared documents, is not a change of
    // domain and must not nag.
    const isIdentityDoc = (docId) => {
        const doc = docs.find((d) => d.doc === docId);
        return !!doc && (doc.kind === 'prompt' || doc.kind === 'agent')
            && !doc.shared_across_profiles;
    };

    window.savePrompt = async function () {
        const text = el('prompt-editor-text').value;
        const doc = docs.find((d) => d.doc === current);
        if (doc && doc.shared_across_profiles &&
            !confirm('This prompt is used by every agent, not just this one. Save it?')) {
            return;
        }
        // Whether the live text really moved decides whether this was a change
        // of domain or a wording tweak -- and so whether to offer the skills.
        const changed = (loaded.live || '').trim() !== text.trim();
        const identity = isIdentityDoc(current);
        status('Saving…');
        try {
            const data = await call('/prompts/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ doc: current, text: text }),
            });
            await refreshStatus(true);
            renderMeta();
            loaded = await call(`/prompts/get?doc=${encodeURIComponent(current)}`);
            const saved = data.message || 'Saved.';
            // Save & use is the end of the job: the prompt is live, so the
            // panel gets out of the way instead of leaving the person to
            // find the Close button.
            closePromptEditor();
            offerSkillSwap(saved, identity && changed);
        } catch (e) {
            status(e.message, 'err');
        }
    };

    window.revertPrompt = async function () {
        if (!confirm('Delete your copy of this prompt and go back to the shared text?')) return;
        status('Reverting…');
        try {
            const data = await call('/prompts/revert', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ doc: current }),
            });
            await refreshStatus(true);
            await selectDoc(current);
            status(data.message || 'Reverted.', 'ok');
        } catch (e) {
            status(e.message, 'err');
        }
    };

    window.uploadPrompt = async function () {
        if (!confirm('Copy your version to the shared folder? Everyone loads it from there.')) return;
        status('Uploading…');
        try {
            const data = await call('/prompts/upload', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ doc: current }),
            });
            await refreshStatus(true);
            status(data.message || 'Uploaded.', 'ok');
        } catch (e) {
            status(e.message, 'err');
        }
    };

    window.refreshSharedPrompts = async function () {
        status('Refreshing from the share…');
        try {
            const data = await call('/prompts/refresh_shared', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: '{}',
            });
            await refreshStatus(true);
            if (current) await selectDoc(current);
            status(data.message || 'Refreshed.', 'ok');
        } catch (e) {
            status(e.message, 'err');
        }
    };
}());
