    // ── Two related but DISTINCT skill-YAML flags ─────────────────────
    //
    // __yamlModified
    //   "Did the user modify the skill YAML *this session*?"
    //   Transient. Set TRUE after a successful Save in the skill
    //   editor (line ~6230) or when /feedback/vote echoes back
    //   yaml_modified=true (the server-side session flag). Reset to
    //   false on log switch / by the user explicitly switching back
    //   to the cloud baseline isn't relevant — that's the OTHER flag.
    //   Read by: isYamlModified() (feedback attach-yaml default),
    //            uploadModifiedYamlSilently() (auto-fire after 👍).
    //
    // __usingUserYaml
    //   "Is the currently active YAML the user/ customised one (vs
    //   cloud baseline)?"
    //   Persistent — survives sessions because it reflects on-disk
    //   state. Set from /skills_yaml/status.effective_source. Used
    //   to drive UI hints / source-badge logic.
    //
    // Previously a single window.__yamlModified flag was overloaded
    // with both meanings, which let stale "active source = user" from
    // a previous session silently trigger an upload on the next 👍
    // even when nothing was modified this session.
    window.__yamlModified = false;
    window.__usingUserYaml = false;

    // ── Skill source state (cloud baseline ⇄ user customised) ─────────
    window.__skillYamlStatus = null;

    async function refreshSkillSourcePanel() {
        try {
            const res = await fetch(`${CHATBOT_API}/skills_yaml_status`);
            const data = await res.json().catch(() => ({}));
            if (!data || !data.success) {
                renderSkillSourcePanel(null);
                return null;
            }
            window.__skillYamlStatus = data;
            // /skills_yaml/status reports which YAML is ACTIVE on disk
            // (persistent state). It does NOT tell us whether the user
            // modified anything this session — only a Save in the
            // editor (or a server-echoed yaml_modified=true) does.
            window.__usingUserYaml = (data.effective_source === 'user');
            renderSkillSourcePanel(data);
            // Populate the Available Skills list on page load so it doesn't
            // stay empty until the user loads a log.
            if (data.skills && data.skills.length > 0) {
                renderSkills(data.skills);
            }
            return data;
        } catch (e) {
            console.warn('skills_yaml_status fetch failed:', e);
            renderSkillSourcePanel(null);
            return null;
        }
    }

    function renderSkillSourcePanel(status) {
        const badgeEl   = document.getElementById('skill-source-badge');
        const fileEl    = document.getElementById('skill-source-file');
        const actionsEl = document.getElementById('skill-source-actions');
        const panelEl   = document.getElementById('skill-source-panel');
        if (!badgeEl || !fileEl || !actionsEl) return;
        actionsEl.innerHTML = '';

        if (!status) {
            // Status not resolved yet — render as a muted, near-invisible
            // placeholder so the sidebar doesn't draw the user's eye to a
            // transient state. The badge animates back to its normal
            // colour as soon as a real status payload arrives.
            if (panelEl) panelEl.classList.add('unknown');
            badgeEl.className = 'skill-source-badge unknown';
            badgeEl.textContent = '…';
            fileEl.textContent = '';
            return;
        }
        if (panelEl) panelEl.classList.remove('unknown');

        const effective = status.effective_source || 'cloud';
        const cloudFile = (status.cloud_local && status.cloud_local.filename) || '';
        const userFile  = (status.user_local  && status.user_local.filename)  || '';

        // Both states share the SAME visual layout:
        //   line 1 — coloured badge + filename inline
        //   line 2 — a single subtle ghost-button action. The button has a
        //            light blue 1-px border + white background so it reads
        //            as clickable without competing with the prominent
        //            "Edit Skills Configuration" button below.
        if (effective === 'user') {
            badgeEl.className = 'skill-source-badge user';
            badgeEl.textContent = 'Customised (user)';
            fileEl.textContent = userFile;

            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'skill-source-btn';
            btn.textContent = 'Use cloud version';
            btn.title = 'Switch the agent back to the cloud baseline configuration';
            btn.onclick = switchToCloudBaseline;
            actionsEl.appendChild(btn);
        } else {
            badgeEl.className = 'skill-source-badge cloud';
            badgeEl.textContent = 'Cloud baseline';
            fileEl.textContent = cloudFile || '—';

            if (userFile) {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'skill-source-btn';
                btn.textContent = 'Use customised version';
                btn.title = 'Switch the agent to your customised file (' + userFile + ')';
                btn.onclick = switchToUserOverride;
                actionsEl.appendChild(btn);
            }
        }
    }

    async function switchToUserOverride() {
        try {
            const res = await fetch(`${CHATBOT_API}/skills_yaml_use_user`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: '{}',
            });
            const data = await res.json().catch(() => ({}));
            if (data && data.success) {
                if (data.skills) renderSkills(data.skills);
                await refreshSkillSourcePanel();
            } else {
                alert('Switch failed: ' + (data && data.error ? data.error : 'Unknown error'));
                await refreshSkillSourcePanel();
            }
        } catch (e) {
            alert('Network error: ' + e.message);
        }
    }
    async function switchToCloudBaseline() {
        try {
            const res = await fetch(`${CHATBOT_API}/skills_yaml_use_cloud`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: '{}',
            });
            const data = await res.json().catch(() => ({}));
            if (data && data.success) {
                if (data.skills) renderSkills(data.skills);
                // We just switched the ACTIVE source to cloud baseline.
                // refreshSkillSourcePanel() will re-read /status and
                // set __usingUserYaml = false anyway, but set it
                // eagerly here so any code between this point and the
                // refresh resolving sees the consistent state.
                window.__usingUserYaml = false;
                await refreshSkillSourcePanel();
            } else {
                alert('Switch failed: ' + (data && data.error ? data.error : 'Unknown error'));
                await refreshSkillSourcePanel();
            }
        } catch (e) {
            alert('Network error: ' + e.message);
        }
    }

    // ── Side-panel skill editor (per-skill structured form) ────────────
    async function openSkillEditor() {
        const statusEl = document.getElementById('skill-editor-status');
        const saveBtn = document.getElementById('skill-editor-save-btn');
        if (saveBtn) saveBtn.disabled = false;
        statusEl.textContent = 'Loading…';
        statusEl.className = 'log-status';
        statusEl.style.display = 'block';
        try {
            // Load whichever source is currently active so the editor shows
            // exactly what the agent is using.
            const res = await fetch(`${CHATBOT_API}/load_local_skills_yaml`);
            const data = await res.json().catch(() => ({}));
            if (!data || !data.success) {
                showStatus(statusEl, '✘ ' + (data && data.error ? data.error : 'Failed to load YAML.'), 'err');
                return;
            }
            renderSkillEditor(
                data.skills || {},
                data.local_path || '',
                data.filename || '',
                data.source || 'cloud'
            );
            statusEl.style.display = 'none';

            const modal = document.getElementById('skill-editor-modal');
            modal.classList.add('open');
            modal.setAttribute('aria-hidden', 'false');

            // Now the modal is laid out, rule textareas can read their real
            // scrollHeight — re-grow each one so multi-line content reveals
            // itself without the user having to click into the field.
            // Skip textareas inside collapsed skill rows — they're
            // `display:none`, so scrollHeight reads as 0 and would lock
            // the field to a 1-line height. They re-grow when the row is
            // expanded (see toggleSkillEditorRow).
            modal.querySelectorAll(
                '.skill-edit-row:not(.collapsed) .preamble-wrap:not(.collapsed) textarea.rules-preamble'
            ).forEach(ta => autoGrowPreamble(ta));
            modal.querySelectorAll(
                '.skill-edit-row:not(.collapsed) .rule-point > textarea.rule-text'
            ).forEach(ta => autoGrowRule(ta));
        } catch (e) {
            showStatus(statusEl, '✘ Network error: ' + e.message, 'err');
        }
    }
    function closeSkillEditor() {
        const modal = document.getElementById('skill-editor-modal');
        const saveBtn = document.getElementById('skill-editor-save-btn');
        if (saveBtn) saveBtn.disabled = false;
        modal.classList.remove('open');
        modal.setAttribute('aria-hidden', 'true');
    }
    function renderSkillEditor(skillsDict, localPath, filename, source) {
        const fileLabel = document.getElementById('skill-editor-file');
        const label = filename || (localPath ? localPath.split(/[\\\/]/).pop() : '');
        if (label) {
            fileLabel.textContent = (source === 'user' ? 'Customised · ' : 'Cloud baseline · ') + label;
        } else {
            fileLabel.textContent = '';
        }

        const list = document.getElementById('skill-editor-list');
        list.innerHTML = '';
        Object.keys(skillsDict).forEach(key => {
            list.appendChild(buildSkillEditorRow(key, skillsDict[key] || {}));
        });
        if (!list.children.length) addSkillEditorRow();
    }

    // ── Description strength evaluator (WiFi-debug + PDF hybrid) ──────
    //
    // The Anthropic "Building Skills for Claude" guide is written for
    // chat-style skills whose triggering depends entirely on the
    // description. This project triggers skills primarily on LOG keywords
    // (the `keywords:` and `exclusive:` fields), so the PDF's strict
    // requirement that every description carries "Use when user asks …"
    // would be overkill here. The rubric below keeps the PDF's HARD rules
    // (no XML brackets, 1024-char cap, no vague openers) but pushes the
    // chat-style trigger signals into a bonus tier.
    //
    //   Base (each independent, +1):
    //     • Length sits in a healthy band (20 ≤ len ≤ 1024 chars)
    //     • Concrete content — an action verb OR a domain noun
    //       (Wi-Fi, BIOS, DSM, MCC, EAPOL, AP, roaming, scan, …)
    //     • Scope hint — mentions a specific symptom, event, or context
    //       ("disconnection", "after resume", "country-specific", …)
    //
    //   Bonus (+1, capped):
    //     • Explicit chat-style trigger: "Use when …", quoted phrases,
    //       "asks about", "mentions" — OR a negative trigger
    //       ("Do NOT use for …") that suppresses over-triggering.
    //
    //   Penalties:
    //     −1  Opens with a vague verb (Helps / Does / Works with)
    //     −2  Length > 1024 (hard PDF limit)
    //     CAP Weak when text contains `<` or `>` (PDF security rule)
    //
    // Non-blocking: nothing here stops the user from saving.
    function evaluateDescription(text) {
        const t = (text || '').trim();
        const LEN = t.length;

        if (!t) {
            return {
                score: 0, label: 'Empty', cls: 'weak', length: 0,
                hints: ['Add a description.'],
            };
        }

        // Action verbs — generous whitelist, both PDF examples and
        // log-analysis vocabulary.
        const verbRe = new RegExp(
            '^\\s*(' +
                'analy[sz]es?|identif|check|inspect|diagnos|find|detect|' +
                'determin|extract|track|comput|measur|verif|validat|' +
                'assess|review|summari[sz]|categori[sz]|generat|creat|' +
                'used to|provides?|handles?|manages?|monitors?|audits?|' +
                'orchestrat|automat|enables?|applies?|runs?|parses?|' +
                'processes?|walks?|derives?|maps?|flags?|reports?|' +
                'triages?|surveys?|correlat|traces?|covers?|spots?|' +
                'end-to-end|multi-step' +
            ')\\b', 'i');

        // Generic "concrete content" signals — works across any debug
        // domain (Wi-Fi, Bluetooth, USB, audio, …), no hard-coded
        // vocabulary. ANY one of these counts as concrete content even
        // without an action-verb opener.
        //   * acronym       — 2+ contiguous capitals (BT, BLE, GATT, BIOS, AP, RF…)
        //   * camelOrSnake  — mixed-case or underscored identifier
        //                     (TASK_DISCONNECT, prvLarStoreCurrentMcc,
        //                      bssVifClcConnected, profileSwitch, …)
        //   * numericTech   — alphanumeric blends (6GHz, 11ax, 11be, A2DP, USB3)
        //   * markedTerm    — explicitly marked tokens: `term`, [TAG], "TERM"
        //   * debugDomain   — generic debug-vocabulary words
        const acronymRe     = /\b[A-Z]{2,}(?:[_-][A-Z]+)*\b/;
        const camelOrSnake  = /\b(?:[a-z]+_[a-zA-Z0-9_]+|[a-z]+[A-Z][a-zA-Z0-9]*)\b/;
        const numericTech   = /\b(?:\d+[A-Za-z][A-Za-z0-9]*|[A-Za-z]+\d+[A-Za-z0-9]*)\b/;
        const markedTerm    = /`[^`]+`|\[[A-Za-z0-9_-]+\]/;
        const debugDomainRe = /\b(log|logs|debug|trace|error|errors|issue|issues|failure|failures|fault|crash|crashes|connect|disconnect|disconnection|connection|associat|authent|handshake|pair|pairing|profile|register|registers|frame|frames|packet|packets|event|events|interface|controller|host|device|driver|firmware|stack|protocol|spec|specification|standard|config|configuration|setting|setup|init|reset|state|state\s+machine|roaming|scan|signal|coverage|timeout|interference|drop|kick[_-]?off|wake|resume|suspend)\b/i;

        // Scope hint — symptom, event, condition, audience. Same
        // domain-agnostic vocabulary as above plus structural cues
        // ("when …", "after …", "for cases where …").
        const scopeRe = /\b(when|after|during|before|while|whenever|on\s+\w+|for\s+\w+|in\s+cases?|distinguish|categori[sz]e|fallback|range\s+of|driven\s+by|set[_-]?up|reconnection|drops?|kick[_-]?off|failure|timeout|crash|reset|resume|suspend|wake|specific|country[_-]?specific|platform[_-]?specific|signal|coverage|interference|profile|pairing|initiali[sz]ation|negotiation)\b/i;

        // Bonus signals (any one counts).
        const triggerRe = /"[^"]+"|'[^']+'|\basks?\s+about\b|\bmentions?\b|\brequests?\s+about\b|\bsays?\b/i;
        const whenRe    = /\b(use\s+when|use\s+for|when\s+the?\s*user|if\s+user|triggers?\s+on|for\s+cases?\s+where|on\s+requests?\s+about)/i;
        const negRe     = /\b(do\s+not\s+use|don['’]?t\s+use|not\s+for|skip\s+when|avoid\s+when|exclude(?:\s+when)?)\b/i;

        const vagueRe   = /^\s*(helps?(?:\s+with)?|does|works?\s+with|deals?\s+with|takes?\s+care\s+of|is\s+for)\b/i;
        const xmlRe     = /[<>]/;

        const HAS_XML      = xmlRe.test(t);
        const HAS_VAGUE    = vagueRe.test(t);
        const HAS_VERB     = verbRe.test(t);
        const HAS_DOMAIN   = (
            acronymRe.test(t) ||
            camelOrSnake.test(t) ||
            numericTech.test(t) ||
            markedTerm.test(t)  ||
            debugDomainRe.test(t)
        );
        const HAS_SCOPE    = scopeRe.test(t);
        const HAS_TRIGGER  = whenRe.test(t) || triggerRe.test(t) || negRe.test(t);
        const LENGTH_OK    = LEN >= 20 && LEN <= 1024;
        const TOO_SHORT    = LEN < 15;
        const TOO_LONG     = LEN > 1024;

        // Concrete content = real verb OR a domain noun (and not vague).
        const HAS_CONCRETE = (HAS_VERB || HAS_DOMAIN) && !HAS_VAGUE;

        let score = 0;
        if (LENGTH_OK)     score += 1;
        if (HAS_CONCRETE)  score += 1;
        if (HAS_SCOPE)     score += 1;
        if (HAS_TRIGGER)   score += 1;   // bonus tier
        if (HAS_VAGUE)     score -= 1;
        if (TOO_LONG)      score -= 2;
        score = Math.max(0, Math.min(score, 4));
        if (HAS_XML) score = Math.min(score, 1);

        // Hints surface only what would lift the description to the NEXT
        // grade — never floods the user with everything at once.
        const hints = [];
        if (HAS_XML) {
            hints.push('⚠ Remove "<" / ">" — angle brackets are forbidden in skill descriptions.');
        } else if (HAS_VAGUE) {
            hints.push('Replace the vague verb with something concrete (Analyses, Identifies, Checks …).');
        } else if (!HAS_CONCRETE) {
            hints.push('Add an action verb OR a concrete debug term (a log keyword, a protocol acronym, a code identifier, etc.).');
        } else if (!HAS_SCOPE) {
            hints.push('Hint at when this applies (e.g. "after resume", "during pairing", "for unexpected drops").');
        } else if (!HAS_TRIGGER) {
            hints.push('Optional: add a chat trigger ("Use when …") or a negative scope ("Not for …").');
        }
        if (TOO_SHORT)
            hints.push('A bit more detail would help the agent and other engineers.');
        if (TOO_LONG)
            hints.push('Exceeds 1024 chars — shorten it.');

        let label = 'Weak', cls = 'weak';
        if (HAS_XML)          { label = 'Invalid'; cls = 'weak';   }
        else if (score <= 1)  { label = 'Weak';    cls = 'weak';   }
        else if (score === 2) { label = 'Fair';    cls = 'fair';   }
        else if (score === 3) { label = 'Good';    cls = 'good';   }
        else                  { label = 'Strong';  cls = 'strong'; }

        return { score, label, cls, length: LEN, hints };
    }

    function updateDescStrength(skillRow) {
        const input = skillRow.querySelector('.skill-desc');
        const wrap  = skillRow.querySelector('.desc-strength');
        if (!input || !wrap) return;
        const r = evaluateDescription(input.value);

        const segs = wrap.querySelectorAll('.desc-strength-bar .seg');
        segs.forEach((s, i) => {
            s.classList.remove('weak', 'fair', 'good', 'strong');
            if (i < r.score) s.classList.add(r.cls);
        });

        const labelEl = wrap.querySelector('.desc-strength-label');
        labelEl.className = 'desc-strength-label ' + r.cls;
        labelEl.textContent = r.label;

        const countEl = wrap.querySelector('.desc-strength-count');
        countEl.textContent = '· ' + r.length + '/1024';

        const hintEl = wrap.querySelector('.desc-strength-hints');
        hintEl.textContent = r.hints.length ? '— ' + r.hints.join(' ') : '';
    }

    function syncSkillNameFromKey(skillRow) {
        const keyEl = skillRow.querySelector('.skill-key');
        const nameEl = skillRow.querySelector('.skill-name');
        const syncEl = skillRow.querySelector('.skill-name-sync');
        if (!keyEl || !nameEl || !syncEl || !syncEl.checked) return;
        nameEl.value = keyEl.value.trim();
    }

    function wireSkillNameSync(skillRow, initialName) {
        const keyEl = skillRow.querySelector('.skill-key');
        const nameEl = skillRow.querySelector('.skill-name');
        const syncEl = skillRow.querySelector('.skill-name-sync');
        if (!keyEl || !nameEl || !syncEl) return;

        const startSynced = !initialName || initialName === (keyEl.value || '').trim();
        syncEl.checked = startSynced;
        if (startSynced) {
            syncSkillNameFromKey(skillRow);
        }
        nameEl.disabled = syncEl.checked;

        keyEl.addEventListener('input', () => syncSkillNameFromKey(skillRow));
        syncEl.addEventListener('change', () => {
            if (syncEl.checked) {
                syncSkillNameFromKey(skillRow);
                nameEl.disabled = true;
            } else {
                nameEl.disabled = false;
                nameEl.focus();
            }
        });
    }

    function buildSkillEditorRow(key, val) {
        const tpl = document.getElementById('skill-edit-row-template');
        const node = tpl.content.firstElementChild.cloneNode(true);
        node.querySelector('.skill-key').value  = key || '';
        node.querySelector('.skill-name').value = val.name || '';
        wireSkillNameSync(node, val.name || '');

        const descEl = node.querySelector('.skill-desc');
        descEl.value = val.description || '';
        descEl.addEventListener('input', () => updateDescStrength(node));
        // Initial paint so the bar / hint reflect loaded content immediately.
        // Runs again after the modal opens, when layout is final.
        updateDescStrength(node);

        // Chip-style keywords / exclusive
        const kwBox = node.querySelector('.keywords-box');
        const exBox = node.querySelector('.exclusive-box');
        wireChipBox(kwBox);
        wireChipBox(exBox);
        // rstrip only on load: a leading space inside a keyword
        // (e.g. " ------- RESUME FLOW") is part of the literal log
        // prefix the keyword is meant to match and must survive the
        // round trip. Trailing space is stripped — that's always noise.
        const _rstripChip = (s) => String(s).replace(/\s+$/, '');
        const kw = Array.isArray(val.keywords) ? val.keywords
                  : (val.keywords ? [String(val.keywords)] : []);
        kw.forEach(k => addChip(kwBox, _rstripChip(k)));
        const ex = Array.isArray(val.exclusive) ? val.exclusive
                  : (val.exclusive ? [String(val.exclusive)] : []);
        ex.forEach(k => addChip(exBox, _rstripChip(k)));

        // Expert rules → { preamble, items[] }. The YAML on disk is a
        // single string; everything BEFORE the first numbered "N." line
        // is the preamble, the numbered items become individual list rows.
        const raw = val.expert_rules;
        let parsed = { preamble: '', items: [] };
        if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
            parsed = {
                preamble: String(raw.preamble || '').trim(),
                items: Array.isArray(raw.items)
                       ? raw.items.map(s => String(s).trim()).filter(Boolean)
                       : [],
            };
        } else if (Array.isArray(raw)) {
            // Legacy: an unwrapped list of items, no preamble.
            parsed = {
                preamble: '',
                items: raw.map(item => {
                    if (item && typeof item === 'object') {
                        return String(item.text || '').trim();
                    }
                    return String(item).trim();
                }).filter(Boolean),
            };
        } else if (typeof raw === 'string' && raw.trim()) {
            parsed = parseRulesString(raw);
        }

        const preambleEl  = node.querySelector('textarea.rules-preamble');
        const preambleBox = node.querySelector('.preamble-wrap');
        if (preambleEl && preambleBox) {
            preambleEl.value = parsed.preamble || '';
            preambleEl.addEventListener('input', () => autoGrowPreamble(preambleEl));
            // Collapse the description box whenever the loaded YAML had no
            // preamble — the user can still hit "+ Add description" to bring
            // it back. When there IS content, expand so it is immediately
            // visible and editable.
            if (parsed.preamble) {
                preambleBox.classList.remove('collapsed');
                autoGrowPreamble(preambleEl);
            } else {
                preambleBox.classList.add('collapsed');
            }
        }
        parsed.items.forEach(text => addRulePoint(node, text));

        return node;
    }

    function addSkillEditorRow() {
        const list = document.getElementById('skill-editor-list');
        const row = buildSkillEditorRow('', {});
        // Brand-new rows start EXPANDED so the user can fill in fields
        // immediately. Existing skills loaded from YAML stay collapsed
        // (see renderSkillEditor) — that's where the clutter comes from
        // when many skills are present.
        row.classList.remove('collapsed');
        list.appendChild(row);
    }
    function removeSkillEditorRow(btn) {
        const row = btn.closest('.skill-edit-row');
        if (row) row.remove();
    }
    // Toggle a single skill row between collapsed (header-only) and
    // expanded (full editor). Rotation of the ▾ triangle is purely CSS.
    // On expand, re-grow the textareas inside — while collapsed they had
    // scrollHeight=0 and any earlier auto-grow pass left them at 1 line.
    function toggleSkillEditorRow(btn) {
        const row = btn.closest('.skill-edit-row');
        if (!row) return;
        const wasCollapsed = row.classList.contains('collapsed');
        row.classList.toggle('collapsed');
        if (wasCollapsed) {
            row.querySelectorAll('.preamble-wrap:not(.collapsed) textarea.rules-preamble')
               .forEach(ta => autoGrowPreamble(ta));
            row.querySelectorAll('.rule-point > textarea.rule-text')
               .forEach(ta => autoGrowRule(ta));
        }
    }

    // --- Chip-input helpers ----------------------------------------------
    function wireChipBox(box) {
        if (!box || box.__wired) return;
        box.__wired = true;
        const input = box.querySelector('.chip-input');
        box.addEventListener('click', e => {
            if (e.target === box) input.focus();
        });
        input.addEventListener('focus', () => box.classList.add('focused'));
        input.addEventListener('blur',  () => {
            const v = input.value.trim();
            if (v) addChip(box, v);
            input.value = '';
            box.classList.remove('focused');
        });
        input.addEventListener('keydown', e => {
            if (e.key === 'Enter' || e.key === ',') {
                e.preventDefault();
                const v = input.value.trim();
                if (v) addChip(box, v);
                input.value = '';
            } else if (e.key === 'Backspace' && !input.value) {
                const chips = box.querySelectorAll('.chip');
                if (chips.length) chips[chips.length - 1].remove();
            }
        });
        input.addEventListener('paste', e => {
            const text = (e.clipboardData || window.clipboardData).getData('text');
            if (text && /[\n,]/.test(text)) {
                e.preventDefault();
                text.split(/[\n,]+/).forEach(t => {
                    const v = t.trim();
                    if (v) addChip(box, v);
                });
                input.value = '';
            }
        });
    }
    function addChip(box, value) {
        if (!box || !value) return;
        // rstrip only — preserve any leading whitespace passed in by the
        // caller. Callers that DON'T want leading whitespace (e.g. fresh
        // input from the keydown / blur / paste handlers) already strip
        // their own input before calling. This is the path that keeps
        // cloud-baseline keywords like " ------- RESUME FLOW" intact
        // through edit + save.
        const v = String(value).replace(/\s+$/, '');
        if (!v) return;
        // Deduplicate within the same box.
        const existing = Array.from(box.querySelectorAll('.chip'))
            .map(c => c.dataset.value);
        if (existing.includes(v)) return;
        const chip = document.createElement('span');
        chip.className = 'chip';
        chip.dataset.value = v;
        const text = document.createElement('span');
        text.textContent = v;
        const x = document.createElement('button');
        x.type = 'button';
        x.className = 'chip-x';
        x.textContent = '✕';
        x.title = 'Remove';
        x.onclick = () => chip.remove();
        chip.appendChild(text);
        chip.appendChild(x);
        const input = box.querySelector('.chip-input');
        box.insertBefore(chip, input);
    }
    function readChips(box) {
        return Array.from(box.querySelectorAll('.chip')).map(c => c.dataset.value);
    }

    // --- Numbered rules helpers ------------------------------------------
    // Rule rows are two-level: top-level "1., 2., 3." with optional
    // Word-style nested "a., b., c." sub-steps under each one. The data
    // model passed between frontend ↔ backend is:
    //
    //   expert_rules: [
    //     { text: "Main rule …", subs: ["sub a", "sub b"] },
    //     { text: "Next main …", subs: [] }
    //   ]
    //
    // The backend joins this back into the single multi-line string that
    // is stored under `expert_rules:` in YAML.

    // Parse a single-string expert_rules block stored in YAML into the
    // {preamble, items} shape the editor uses. Rules:
    //
    //   * Lines BEFORE the first "N." item become the preamble (kept as a
    //     single multi-line string in the main description box).
    //   * Each line starting with "N." or "N)" — at any indentation —
    //     opens a new numbered item. Subsequent continuation lines fall
    //     into whichever item is currently open.
    //   * If the text has no numbered items at all, the entire string is
    //     preamble and there are zero list items.
    //
    // No 2-level nesting any more — this matches the simplified
    // "one description + a flat list" model the editor exposes.
    function parseRulesString(raw) {
        const trimmed = String(raw || '').replace(/\s+$/, '');
        if (!trimmed) return { preamble: '', items: [] };

        // A new numbered item only starts on a line whose number sits at
        // the beginning (after yaml.load has already stripped the block
        // scalar's base indent). Continuation lines that happen to start
        // with their own indentation must NOT match — otherwise an
        // indented sub-bullet like "  1) sub" would be misread as a new
        // top-level item.
        //
        // The captured prefix supports hyphenated section numbering used
        // by the cloud baseline (`2-1.`, `2-2.`, `3-1.`, …). The prefix
        // is preserved verbatim through the round trip so the original
        // author's section structure (e.g. "step 2 has sub-parts 2-1 / 2-2")
        // survives editing.
        const ITEM_RE = /^(\d+(?:-\d+)*)[.)]\s*(.*)$/;

        // Leading whitespace on continuation lines is PRESERVED verbatim
        // so the indented log examples / nested bullets that the cloud
        // baseline relies on survive the round trip and re-emerge under
        // the same numbered item on save.
        const preambleLines = [];
        const items = [];   // { prefix: "2-1", text: "..." }
        let current = null;

        const rstrip = (s) => s.replace(/\s+$/, '');

        for (const rawLine of trimmed.split(/\r?\n/)) {
            const line = rstrip(rawLine);
            const m = line.match(ITEM_RE);
            if (m) {
                if (current !== null) items.push(current);
                current = { prefix: m[1], text: m[2] };
                continue;
            }
            if (current !== null) {
                current.text += '\n' + line;
            } else {
                preambleLines.push(line);
            }
        }
        if (current !== null) items.push(current);

        // Items keep their trailing newline if one was there in the source —
        // that newline represents an intentional blank line between this
        // numbered item and the next one (the cloud baseline uses these
        // gaps as paragraph separators). Per-line rstrip already removed
        // line-internal trailing whitespace, so anything left at the end
        // is structural.
        //
        // Preamble: cap trailing whitespace to AT MOST one "\n". A trailing
        // newline encodes "the cloud baseline left a blank line between
        // my preamble and item 1"; no newline means "no gap". Both shapes
        // round-trip back to YAML faithfully via the dumper.
        const _capTrailingNewline = (s) =>
            s.replace(/\s+$/, (m) => (m.includes('\n') ? '\n' : ''));
        return {
            preamble: _capTrailingNewline(preambleLines.join('\n')),
            items: items
                .map((it) => ({
                    prefix: it.prefix,
                    text: _capTrailingNewline(it.text),
                }))
                .filter((it) => it.text.replace(/\s/g, '').length > 0),
        };
    }

    // The expert_rules editor is one preamble textarea + a flat list of
    // numbered items. Wire-shape between frontend ↔ backend:
    //
    //   expert_rules: { preamble: "free-form text", items: ["1st", "2nd"] }
    //
    // The backend joins this back into the single multi-line string
    // stored under `expert_rules:` in YAML (preamble lines first, then
    // "1. …", "2. …" on the lines that follow).

    function addRulePoint(skillRow, textOrItem) {
        const list = skillRow.querySelector('.rules-list');
        const tpl  = document.getElementById('rule-point-template');
        const node = tpl.content.firstElementChild.cloneNode(true);
        const ta   = node.querySelector('.rule-text');

        // Accept either a plain string (new item the user just clicked
        // "+ Add" for) or an object {prefix, text} from the parser. When
        // a prefix is supplied (e.g. cloud baseline's "2-1"), pin it onto
        // the row via dataset so renumberRules() leaves it alone.
        let prefix = null;
        let text = '';
        if (textOrItem && typeof textOrItem === 'object' && !Array.isArray(textOrItem)) {
            prefix = (textOrItem.prefix != null) ? String(textOrItem.prefix) : null;
            text = String(textOrItem.text || '');
        } else {
            text = String(textOrItem || '');
        }
        if (prefix !== null) {
            node.dataset.prefix = prefix;
        }
        ta.value = text;
        ta.addEventListener('input', () => autoGrowRule(ta));
        list.appendChild(node);
        autoGrowRule(ta);
        renumberRules(skillRow);
    }

    function removeRulePoint(btn) {
        const row = btn.closest('.rule-point');
        const skillRow = btn.closest('.skill-edit-row');
        if (!row) return;
        row.remove();
        if (skillRow) renumberRules(skillRow);
    }

    function autoGrowRule(ta) {
        if (!ta) return;
        ta.style.height = 'auto';
        ta.style.height = Math.min(ta.scrollHeight, 220) + 'px';
    }
    function autoGrowPreamble(ta) {
        if (!ta) return;
        ta.style.height = 'auto';
        ta.style.height = Math.min(ta.scrollHeight, 240) + 'px';
    }

    function showPreamble(skillRow) {
        const box = skillRow.querySelector('.preamble-wrap');
        const ta  = skillRow.querySelector('textarea.rules-preamble');
        if (!box) return;
        box.classList.remove('collapsed');
        if (ta) {
            autoGrowPreamble(ta);
            ta.focus();
        }
    }
    function hidePreamble(skillRow) {
        const box = skillRow.querySelector('.preamble-wrap');
        const ta  = skillRow.querySelector('textarea.rules-preamble');
        if (!box) return;
        // Only collapse when the textarea is empty; otherwise the action
        // would silently hide non-empty content the user just typed.
        if (ta && ta.value.trim()) {
            alert('Please clear the description before hiding the box.');
            ta.focus();
            return;
        }
        box.classList.add('collapsed');
    }
    function renumberRules(skillRow) {
        const items = skillRow.querySelectorAll('.rules-list > .rule-point');
        // Items with an explicit `data-prefix` (loaded from cloud, e.g.
        // "2-1") keep their original label so the cloud baseline's
        // section structure survives editing. Items without a prefix —
        // typically rows the user just added via "+ Add analysis step"
        // — get the next integer after the largest integer-only prefix
        // currently in use.
        let maxInt = 0;
        items.forEach((r) => {
            const p = r.dataset.prefix;
            if (p && /^\d+$/.test(p)) {
                maxInt = Math.max(maxInt, parseInt(p, 10));
            }
        });
        items.forEach((r) => {
            let label = r.dataset.prefix;
            if (!label) {
                maxInt += 1;
                label = String(maxInt);
                r.dataset.prefix = label;
            }
            const num = r.querySelector('.rule-number');
            if (num) num.textContent = label;
        });
    }
    function _cleanRuleField(s) {
        // Per-line rstrip (kill trailing spaces / tabs but keep newlines),
        // strip leading whitespace, and cap trailing whitespace to AT MOST
        // one "\n" so an intentional blank line at the end survives the
        // round trip but stray multi-blank-line typos are cleaned up.
        let v = String(s || '');
        v = v.split('\n').map((l) => l.replace(/[ \t]+$/, '')).join('\n');
        v = v.replace(/^\s+/, '');
        v = v.replace(/\s+$/, (m) => (m.includes('\n') ? '\n' : ''));
        return v;
    }
    function readRules(skillRow) {
        const preambleEl = skillRow.querySelector('textarea.rules-preamble');
        const preamble = _cleanRuleField(preambleEl ? preambleEl.value : '');
        const items = Array.from(
            skillRow.querySelectorAll('.rules-list > .rule-point')
        )
            .map((row) => {
                const ta = row.querySelector('.rule-text');
                return {
                    prefix: row.dataset.prefix || null,
                    text: _cleanRuleField(ta ? ta.value : ''),
                };
            })
            .filter((it) => it.text.replace(/\s/g, '').length > 0);
        return { preamble, items };
    }

    async function saveSkillEditor() {
        const rows = document.querySelectorAll('#skill-editor-list .skill-edit-row');
        const skills = {};
        const seenKeys = new Set();

        // Clear any prior invalid markers.
        document.querySelectorAll('#skill-editor-list .invalid')
            .forEach(el => el.classList.remove('invalid'));

        let firstError = null;
        for (const r of rows) {
            const keyEl  = r.querySelector('.skill-key');
            const nameEl = r.querySelector('.skill-name');
            const descEl = r.querySelector('.skill-desc');

            const key  = keyEl.value.trim();
            const name = nameEl.value.trim();
            // <input type="text"> can still hold a multi-line value when
            // pasted from outside; collapse any newline/tab/multi-space
            // runs into a single space so the saved YAML stays on one line
            // and no double-space artefacts survive in the description.
            const desc = descEl.value.replace(/\s+/g, ' ').trim();

            if (!key) {
                keyEl.classList.add('invalid');
                firstError = firstError || 'Skill ID is required.';
                continue;
            }
            if (!name) {
                nameEl.classList.add('invalid');
                firstError = firstError || ('Skill "' + key + '": display name is required.');
                continue;
            }
            if (!desc) {
                descEl.classList.add('invalid');
                firstError = firstError || ('Skill "' + key + '": description is required.');
                continue;
            }
            if (seenKeys.has(key)) {
                keyEl.classList.add('invalid');
                firstError = firstError || ('Duplicate skill key: "' + key + '".');
                continue;
            }
            seenKeys.add(key);

            const rulesPayload = readRules(r);
            const hasRules = rulesPayload.preamble.trim().length > 0
                          || rulesPayload.items.length > 0;
            if (!hasRules) {
                // Flag whichever box is currently visible so the user knows
                // where to type — the preamble textarea if it's expanded,
                // otherwise the "+ Add overview" button.
                const wrap = r.querySelector('.preamble-wrap');
                const preEl = r.querySelector('textarea.rules-preamble');
                if (wrap && wrap.classList.contains('collapsed')) {
                    const showBtn = r.querySelector('.preamble-show-btn');
                    if (showBtn) showBtn.classList.add('invalid');
                } else if (preEl) {
                    preEl.classList.add('invalid');
                }
                firstError = firstError || ('Skill "' + key + '": expert rules are required (add an overview or at least one analysis step).');
                continue;
            }

            skills[key] = {
                name:         name,
                description:  desc,
                keywords:     readChips(r.querySelector('.keywords-box')),
                exclusive:    readChips(r.querySelector('.exclusive-box')),
                expert_rules: rulesPayload,
            };
        }

        if (firstError) { alert(firstError); return; }
        if (!Object.keys(skills).length) {
            alert('Please define at least one skill before saving.');
            return;
        }

        const saveBtn = document.getElementById('skill-editor-save-btn');
        saveBtn.disabled = true;
        const statusEl = document.getElementById('skill-editor-status');
        try {
            const res = await fetch(`${CHATBOT_API}/save_local_skills_yaml`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({skills}),
            });
            const data = await res.json().catch(() => ({}));
            if (data && data.success) {
                if (data.skills) renderSkills(data.skills);
                window.__yamlModified = true;
                showStatus(statusEl, '✔ ' + data.message, 'ok');
                statusEl.style.display = 'block';
                closeSkillEditor();
                await refreshSkillSourcePanel();
            } else {
                alert('Save failed: ' + (data && data.error ? data.error : 'Unknown error'));
                saveBtn.disabled = false;
            }
        } catch (e) {
            alert('Network error: ' + e.message);
            saveBtn.disabled = false;
        }
    }

    // ── Silent upload of the user's modified YAML after a 👍 ──────────
    // Fires only when window.__yamlModified is true (i.e. the user
    // actually edited their skill config this session). Runs in the
    // background — no confirmation dialog, no UI interruption.
    // Re-triggers are de-duplicated so refreshing a turn / clicking 👍
    // twice doesn't upload twice.
    async function uploadModifiedYamlSilently() {
        if (!window.CHATBOT || !window.CHATBOT.allow_modified_yaml_upload) return;
        if (window.__yamlUploadInFlight || window.__yamlUploadedThisSession) return;
        window.__yamlUploadInFlight = true;
        try {
            const res = await fetch(`${CHATBOT_API}/upload_modified_yaml`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: '{}',
            });
            const data = await res.json().catch(() => ({}));
            if (data && data.success) {
                window.__yamlUploadedThisSession = true;
                console.info('[skill upload] uploaded:', data.uploaded_to || '');
            } else {
                console.warn('[skill upload] failed:', data && data.error);
            }
        } catch (e) {
            console.warn('[skill upload] network error:', e);
        } finally {
            window.__yamlUploadInFlight = false;
        }
    }

    // ── On page load, render the skill source badge + toggle ──────────
    // Active source is always reset to "cloud" on app restart, so the page
    // boots showing the cloud baseline. If a user-customised file exists
    // on disk, the toggle to switch back to it shows up here.
    document.addEventListener('DOMContentLoaded', function () {
        refreshSkillSourcePanel();
    });
