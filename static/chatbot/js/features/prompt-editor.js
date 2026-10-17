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
    //: null until the skills question is answered for this edit; then
    //: "switch" or "keep". Save & use waits on it, so a retargeted agent
    //: cannot be saved while still reading the old domain's skills.
    let skillChoice = null;

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
        skillChoice = null;
        renderTabs();
        renderMeta();
        status('Loading…');
        try {
            loaded = await call(`/prompts/get?doc=${encodeURIComponent(docId)}`);
            el('prompt-editor-text').value = loaded.live || '';
            el('prompt-editor-file').textContent = loaded.filename || '';
            refreshSaveState();
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
        skillChoice = null;
        refreshSaveState();
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
            refreshSaveState();
            status(`Loaded ${file.name} into the editor — review it, then answer `
                   + `the skills question below.`, 'ok');
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
        refreshSaveState();
        status(`Loaded the ${which === 'builtin' ? 'built-in' : which} text — not saved yet.`);
    };

    // Changing the prompt is only half of retargeting an agent: the prompt
    // decides what it looks for, the skills decide which lines it is given to
    // look at. So the panel is two numbered steps — edit the prompt, then
    // answer the skills question — and Save & use is the end of both rather
    // than something that happens before the second one is considered.
    //
    // The question only appears for a document that decides what the agent IS
    // (the bundle or the prompt, not the report, the issue-time briefing or
    // the engine's shared three) and only once the text has actually moved:
    // fixing a typo is not a change of domain.
    function needsSkillStep() {
        const doc = docs.find((d) => d.doc === current);
        const identity = !!doc && (doc.kind === 'prompt' || doc.kind === 'agent')
            && !doc.shared_across_profiles;
        return identity && textChanged() && typeof appendSkillsFromYaml === 'function';
    }

    function textChanged() {
        const box = el('prompt-editor-text');
        if (!box) return false;
        return (loaded.live || '').trim() !== (box.value || '').trim();
    }

    // One place decides what the footer button may do, so the rule cannot
    // drift between the places that change the state it reads.
    function refreshSaveState() {
        const step = el('prompt-skill-step');
        const save = el('prompt-save-btn');
        const wanted = needsSkillStep();
        if (step) step.style.display = wanted && !skillChoice ? 'flex' : 'none';
        if (!save) return;
        if (!textChanged()) {
            save.disabled = true;
            save.title = 'Edit the prompt first — there is nothing to save yet.';
        } else if (wanted && !skillChoice) {
            save.disabled = true;
            save.title = 'Answer the skills question above first.';
        } else {
            save.disabled = false;
            save.title = 'Save this prompt; the next question uses it';
        }
    }

    // The question and the button follow the text, so typing the prompt back
    // to what it was withdraws both, and loading a .md file raises them.
    document.addEventListener('DOMContentLoaded', () => {
        const box = el('prompt-editor-text');
        if (box) box.addEventListener('input', refreshSaveState);
        refreshSaveState();
    });

    window.chooseSkills = async function (choice) {
        skillChoice = choice === 'switch' ? 'switch' : 'keep';
        refreshSaveState();
        if (skillChoice === 'keep') {
            status('Skills left as they are. Save & use when ready.', 'ok');
            return;
        }
        status('Opening the skills file picker…');
        await appendSkillsFromYaml();
        // appendSkillsFromYaml reports into the sidebar's status line, which
        // is behind this panel, so repeat its outcome where it can be seen.
        const sidebar = document.getElementById('skill-editor-status');
        const said = (sidebar && sidebar.textContent || '').trim();
        status(said || 'Skills unchanged.', said.startsWith('✘') ? 'err' : 'ok');
    };

    // Only the document that decides what the agent IS. Saving a report
    // skeleton, or one of the engine's shared documents, is not a change of
    window.savePrompt = async function () {
        const text = el('prompt-editor-text').value;
        const doc = docs.find((d) => d.doc === current);
        // The button is disabled in both of these cases; the checks stand for
        // a keyboard activation or a call from the console.
        if (!textChanged() || (needsSkillStep() && !skillChoice)) {
            refreshSaveState();
            return;
        }
        if (doc && doc.shared_across_profiles &&
            !confirm('This prompt is used by every agent, not just this one. Save it?')) {
            return;
        }
        status('Saving…');
        try {
            const data = await call('/prompts/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ doc: current, text: text }),
            });
            // Both steps are done, so the panel gets out of the way instead of
            // leaving the person to find the Close button.
            closePromptEditor();
            if (typeof showToast === 'function') {
                showToast({
                    message: (data.message || 'Saved.')
                        + (skillChoice === 'switch' ? '' : ' Skills left as they are.'),
                    ttlMs: 12000,
                });
            }
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
