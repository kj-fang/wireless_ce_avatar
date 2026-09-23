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
    async function appendSkillsFromYaml() {
        const statusEl = document.getElementById('skill-editor-status');
        let yamlPath = '';
        try {
            const pickRes = await fetch(`${CHATBOT_API}/browse_yaml`);
            const pickData = await pickRes.json().catch(() => ({}));
            yamlPath = (pickData && pickData.path) ? String(pickData.path).trim() : '';
        } catch (e) {
            if (statusEl) statusEl.textContent = '✘ Could not open file picker: ' + e.message;
            return;
        }
        if (!yamlPath) return;   // user cancelled the dialog

        if (statusEl) statusEl.textContent = 'Importing…';
        try {
            const res = await fetch(`${CHATBOT_API}/append_skills_yaml`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({yaml_path: yamlPath}),
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
            } else if (statusEl) {
                statusEl.textContent = '✘ ' + (data && data.error ? data.error : 'Import failed.');
            }
        } catch (e) {
            if (statusEl) statusEl.textContent = '✘ Network error: ' + e.message;
        }
    }
    window.appendSkillsFromYaml = appendSkillsFromYaml;
