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
    let skillPath = null;   // staged until Save & use; choosing a file is not activation

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
        conversation: 'this conversation only',
        kept: 'your kept copy — new conversations start from it',
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
        // Worth its own badge: it is the one state in which a NEW conversation
        // does not start from the built-in text, so it has to be visible
        // rather than inferred from the live source alone.
        if (doc.kept && doc.source !== 'kept') bits.push('kept for new conversations');
        if (doc.has_local && doc.source === 'local') {
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
        // Also enabled for a kept prompt with no edit in this conversation:
        // that is exactly the state someone needs to get out of, and reading
        // only the live source would lock the button that does it.
        if (revert) revert.disabled = doc.source !== 'conversation' && !doc.kept;
        if (upload) upload.disabled = true; // session edits are never published implicitly
    }

    // ── Keeping an edit ────────────────────────────────────────────────────
    // A save applies to this conversation and writes nothing to disk, which is
    // the point: one person's experiment must not become everyone's agent. The
    // cost is that leaving the page ends the conversation and the edit with it.
    // So the panel closes the loop in two ways that need no server state:
    //   * Download .md — the edit leaves as the file it already is, and comes
    //     back through "Choose .md file…" in the next conversation. This is the
    //     one to use for a prompt worth keeping.
    //   * a draft in this browser — the unsaved text survives a reload or a
    //     misclick. Per viewer, per document, never sent anywhere.
    const draftKey = () => `promptDraft:${API}:${current || ''}`;

    function saveDraft() {
        const box = el('prompt-editor-text');
        if (!box || !current) return;
        try {
            if (textChanged()) localStorage.setItem(draftKey(), box.value);
            else localStorage.removeItem(draftKey());
        } catch (e) { /* private window, or site data blocked */ }
    }

    function readDraft() {
        try { return localStorage.getItem(draftKey()); } catch (e) { return null; }
    }

    function clearDraft() {
        try { localStorage.removeItem(draftKey()); } catch (e) { /* as above */ }
    }

    function offerDraft() {
        const bar = el('prompt-draft-bar');
        if (!bar) return;
        const draft = readDraft();
        const show = !!draft && draft.trim() !== (loaded.live || '').trim();
        bar.style.display = show ? 'flex' : 'none';
    }

    window.restorePromptDraft = function () {
        const draft = readDraft();
        if (!draft) return;
        el('prompt-editor-text').value = draft;
        const bar = el('prompt-draft-bar');
        if (bar) bar.style.display = 'none';
        refreshSaveState();
        status('Restored your unsaved draft — not in use until Save & use.', 'ok');
    };

    window.discardPromptDraft = function () {
        clearDraft();
        const bar = el('prompt-draft-bar');
        if (bar) bar.style.display = 'none';
        status('Draft discarded.');
    };

    window.downloadPromptFile = function () {
        const box = el('prompt-editor-text');
        if (!box || !box.value.trim()) {
            status('There is nothing in the editor to save.', 'err');
            return;
        }
        const stamp = new Date().toISOString().slice(0, 10);
        const base = (loaded.filename || 'agent_prompt.md').replace(/\.md$/i, '');
        const blob = new Blob([box.value], { type: 'text/markdown;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${base}-${stamp}.md`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 0);
        status(`Saved ${a.download}. Load it again with "Choose .md file…" in `
               + `a later conversation.`, 'ok');
    };

    async function selectDoc(docId) {
        current = docId;
        skillChoice = null;
        skillPath = null;
        renderTabs();
        renderMeta();
        status('Loading…');
        try {
            loaded = await call(`/prompts/get?doc=${encodeURIComponent(docId)}`);
            el('prompt-editor-text').value = loaded.live || '';
            el('prompt-editor-file').textContent = loaded.filename || '';
            offerDraft();
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
        skillPath = null;
        const chosen = el('prompt-skill-chosen');
        if (chosen) chosen.textContent = '';
        // Keeping is never carried over from the last time the panel was
        // open: it writes to disk, so it is opted into per edit.
        const keep = el('prompt-keep-check');
        if (keep) keep.checked = false;
        refreshKeepHint();
        refreshSaveState();
        status('Loading…');
        try {
            await refreshStatus(false);
            if (current) await selectDoc(current);
        } catch (e) {
            status(e.message, 'err');
        }
    };

    // ── Keeping, decided on this screen ────────────────────────────────────
    // This used to be a second screen on the way out. It is a checkbox now,
    // sitting under the two steps it depends on: whoever is deciding has the
    // prompt and the skills in front of them, which is when they actually
    // know whether this was an experiment or their new default. Unticked,
    // nothing is written to disk and the edit dies with the conversation --
    // the property that keeps one person's experiment off everyone's agent.
    window.closePromptEditor = function () {
        const modal = el('prompt-editor-modal');
        if (!modal) return;
        modal.classList.remove('open');
        modal.setAttribute('aria-hidden', 'true');
    };

    const wantsKeep = () => !!(el('prompt-keep-check') || {}).checked;

    window.refreshKeepHint = function () {
        const hint = el('prompt-keep-hint');
        const row = el('prompt-keep-row');
        if (!hint) return;
        const on = wantsKeep();
        hint.innerHTML = on
            ? 'On — saved as your own copy on <strong>Save &amp; use</strong>, '
              + 'and every new conversation on this page starts from it. '
              + 'The skills you chose above are saved with it. '
              + 'Undo with <em>Restore original</em>.'
            : 'Off — the prompt applies to this conversation only, and leaving '
              + 'the page discards it. Tick to save it as your own copy so every '
              + 'new conversation on this page starts from it (undo with '
              + '<em>Restore original</em>).';
        if (row) {
            row.style.background = on ? '#eafaf0' : '#f6fbf7';
            row.style.borderColor = on ? '#7ccb96' : '#bbe3c6';
        }
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
        if (box) {
            box.addEventListener('input', () => {
                refreshSaveState();
                saveDraft();
            });
        }
        refreshSaveState();
    });

    // "replace" is the one that matches a domain change: the chosen file
    // becomes the whole set, so the previous domain's skills are not left in
    // the menu this agent's prompt tells it to choose from. "append" extends
    // the set, which is what the sidebar button has always done.
    window.chooseSkills = async function (choice) {
        const chosen = el('prompt-skill-chosen');
        if (choice === 'keep') {
            skillChoice = 'keep';
            skillPath = null;
            if (chosen) chosen.textContent = 'Skills left as they are.';
            refreshSaveState();
            status('Skills left as they are. Save & use when ready.', 'ok');
            return;
        }
        skillChoice = null;
        skillPath = null;
        if (chosen) chosen.textContent = '';
        refreshSaveState();
        status('Opening the skills file picker…');
        try {
            const picked = await call('/browse_yaml');
            const path = String(picked.path || '').trim();
            if (!path) { status('No file selected; skills unchanged.'); return; }
            if (choice === 'replace' && !confirm(
                'Replace this conversation’s skills with the chosen YAML when you press Save & use?')) {
                status('Skills unchanged.');
                return;
            }
            skillPath = path;
            skillChoice = choice;
            refreshSaveState();
            // Name the file on the panel. The picker is a native dialog that
            // closes behind itself, so without this the only record of what
            // was chosen is the person's memory of it.
            const chosen = el('prompt-skill-chosen');
            if (chosen) {
                chosen.textContent =
                    `${choice === 'replace' ? 'Replace with' : 'Add from'}: `
                    + path.split(/[\\/]/).pop();
            }
            status('Skills file selected. Prompt and skills change together on Save & use.', 'ok');
        } catch (e) {
            status('Could not choose skills: ' + e.message, 'err');
        }
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
                body: JSON.stringify({ doc: current, text: text,
                    skill_mode: skillChoice || 'keep', skill_yaml_path: skillPath || '',
                    keep: wantsKeep() }),
            });
            if (data.skills && typeof renderSkills === 'function') renderSkills(data.skills);
            if (data.skills && typeof refreshSkillSourcePanel === 'function') {
                await refreshSkillSourcePanel();
            }
            // The text is in use now, so the draft has nothing left to rescue.
            clearDraft();
            // Both steps are done, so the panel gets out of the way instead of
            // leaving the person to find the Close button. The toast goes
            // first: closing now raises the "keep it for next time?" question,
            // and the toast is what explains why it is being asked.
            if (typeof showToast === 'function') {
                showToast({
                    message: (data.message || 'Saved.')
                        + (skillChoice === 'keep' ? ' Skills left as they are.' : ''),
                    ttlMs: 12000,
                });
            }
            loaded.live = text;
            closePromptEditor();
        } catch (e) {
            status(e.message, 'err');
        }
    };

    window.revertPrompt = async function () {
        if (!confirm('Restore the original prompt for this conversation?')) return;
        status('Reverting…');
        clearDraft();
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
