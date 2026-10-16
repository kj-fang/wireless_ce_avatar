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
            btn.textContent = doc.shared_across_profiles
                ? doc.doc.replace('_', ' ')
                : doc.kind;
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

    window.savePrompt = async function () {
        const text = el('prompt-editor-text').value;
        const doc = docs.find((d) => d.doc === current);
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
            await refreshStatus(true);
            renderMeta();
            loaded = await call(`/prompts/get?doc=${encodeURIComponent(current)}`);
            status(data.message || 'Saved.', 'ok');
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
