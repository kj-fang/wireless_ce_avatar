    // ── Import extra skills from a YAML file ──────────────────────────
    //
    // Its own capability (skill_append), separate from the full skill
    // editor: NW offers this button without the editor panel, which is the
    // shape main shipped. Loaded by page.html for any profile that enables
    // skill_append, so there is exactly one definition.
    //
    // Not wrapped in an IIFE, matching its sibling feature scripts -- they
    // share one global scope, which is how renderSkills (core.js) and
    // refreshSkillSourcePanel (skill-editor.js) are reachable from here.

    // ── Append skills from a user-picked YAML file (ported from main #142) ──
    // Two hops: /browse_yaml opens the native picker and returns a path, then
    // /append_skills_yaml merges that file's skills into the active user YAML.
    // Both endpoints are profile-relative, so this one body serves BT and
    // Wi-Fi — main carried a copy of it in each profile's template instead.
    // `mode` is "append" (the sidebar button, extending a set) or "replace"
    // (the prompt panel retargeting a domain, where leaving the old domain's
    // skills in the menu is the whole problem). The server owns what each
    // one does; this only carries the choice.
    async function appendSkillsFromYaml(mode) {
        const replacing = mode === 'replace';
        const statusEl = document.getElementById('skill-editor-status');
        let yamlPath = '';
        try {
            const pickRes = await fetch(`${CHATBOT_API}/browse_yaml`);
            const pickData = await pickRes.json().catch(() => ({}));
            yamlPath = (pickData && pickData.path) ? String(pickData.path).trim() : '';
        } catch (e) {
            if (statusEl) statusEl.textContent = '✘ Could not open file picker: ' + e.message;
            return false;
        }
        if (!yamlPath) return false;   // user cancelled the dialog

        if (replacing && !confirm(
                'Replace this agent\'s skills with the ones in the chosen file?\n\n'
                + 'The skills it has now are dropped, so a retargeted agent is not '
                + 'offered the previous domain\'s filters. Your current set stays in '
                + 'its own dated file, and the shared baseline is untouched.')) {
            if (statusEl) statusEl.textContent = 'Skills left as they are.';
            return false;
        }
        if (statusEl) statusEl.textContent = replacing ? 'Replacing…' : 'Importing…';
        try {
            const res = await fetch(`${CHATBOT_API}/append_skills_yaml`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({yaml_path: yamlPath, mode: replacing ? 'replace' : 'append'}),
            });
            const data = await res.json().catch(() => ({}));
            if (data && data.success) {
                if (statusEl) statusEl.textContent = '✔ ' + data.message;
                if (data.skills) renderSkills(data.skills);
                // The import always flips the active source to "user", so the
                // badge in the panel above is now stale until we re-read it.
                // NW has the button without the editor panel, so guard it.
                if (typeof refreshSkillSourcePanel === 'function') {
                    await refreshSkillSourcePanel();
                }
                return true;
            } else if (statusEl) {
                statusEl.textContent = '✘ ' + (data && data.error ? data.error : 'Import failed.');
            }
        } catch (e) {
            if (statusEl) statusEl.textContent = '✘ Network error: ' + e.message;
        }
        return false;
    }
    window.appendSkillsFromYaml = appendSkillsFromYaml;
