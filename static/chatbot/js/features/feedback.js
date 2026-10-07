    const feedbackDomain =
        (window.CHATBOT && window.CHATBOT.feedback_domain) || '';
    // ── Feedback widget renderer (called by appendAssistantText / appendReport) ─
    function renderFeedbackWidget(turnId) {
        return `
            <div class="feedback-widget" data-turn-id="${turnId}">
                <button type="button" class="feedback-btn" title="Helpful"
                        onclick="submitFeedback('${turnId}', 1, this)">👍</button>
                <button type="button" class="feedback-btn" title="Not helpful"
                        onclick="submitFeedback('${turnId}', -1, this)">👎</button>
            </div>`;
    }

    async function submitFeedback(turnId, vote, btnEl) {
        const widget = btnEl ? btnEl.closest('.feedback-widget') : null;
        if (!widget) return;
        // Toggle / switch: clicking the active vote again clears it (vote=0);
        // clicking the opposite vote switches. Keep buttons clickable so the
        // user can change their mind without reloading the turn.
        const prev = parseInt(widget.dataset.castVote || '0', 10) || 0;
        const nextVote = (prev === vote) ? 0 : vote;
        const conversationId = window.__feedbackConversationId || '';
        widget.querySelectorAll('.feedback-btn').forEach(b => b.disabled = true);

        try {
            const res = await fetch('/feedback/vote', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    conversation_id: conversationId,
                    turn_id: turnId,
                    vote: nextVote,
                    ...(feedbackDomain ? {domain: feedbackDomain} : {})
                }),
            });
            const data = await res.json().catch(() => ({}));
            if (!data || !data.success) {
                console.warn('Feedback save failed:', data);
                return;
            }
            widget.dataset.castVote = String(nextVote);

            // Repaint the two thumb buttons to match the active vote.
            widget.querySelectorAll('.feedback-btn').forEach((b) => {
                b.classList.remove('voted-up', 'voted-down');
            });
            const upBtn   = widget.querySelector('.feedback-btn[title="Helpful"]');
            const downBtn = widget.querySelector('.feedback-btn[title="Not helpful"]');
            if (nextVote === 1 && upBtn)   upBtn.classList.add('voted-up');
            if (nextVote === -1 && downBtn) downBtn.classList.add('voted-down');

            // Reuse a single More-feedback link + thanks span so flipping the
            // vote doesn't stack duplicates onto the widget.
            let more   = widget.querySelector('.feedback-more-link');
            let thanks = widget.querySelector('.feedback-thanks');
            if (nextVote === 0) {
                if (more)   more.remove();
                if (thanks) thanks.remove();
            } else {
                if (!more) {
                    more = document.createElement('a');
                    more.className = 'feedback-more-link';
                    more.textContent = '✏️ More feedback';
                    widget.appendChild(more);
                }
                more.onclick = () => openFeedbackModal(turnId, nextVote, more);
                if (!thanks) {
                    thanks = document.createElement('span');
                    thanks.className = 'feedback-thanks';
                    thanks.textContent = 'Thanks!';
                    widget.appendChild(thanks);
                }
            }
        } catch (e) {
            console.warn('Feedback network error:', e);
        } finally {
            widget.querySelectorAll('.feedback-btn').forEach(b => b.disabled = false);
        }
    }

    // ── Feedback Layer 2: detailed structured feedback modal ─────────
    window.__turnContext = window.__turnContext || {};
    window.__currentFeedbackCtx = null;

    function captureTurnContext(turnId, steps) {
        if (!turnId || !Array.isArray(steps)) return;
        const skillSeen = new Set();
        const skillList = [];
        // Backend wraps the verb in markdown bold (`**Invoking** \`name\``),
        // so allow any non-backtick filler between the verb and the name.
        const invokeRe = /Invoking[^`]*`([^`]+)`/i;
        const fetchRe  = /Fetching\s+filtered\s+logs[^`]*for[^`]*`([^`]+)`/i;
        // Names like `get_final_state_snapshot` / `get_phase_summaries` are
        // raw tool calls, not domain skills — surface them in the step row
        // but keep them out of the skill picker (no playbook to feed back).
        const toolRe   = /^(get|fetch)_/i;

        // Bucket raw actions by their reasoning-step number so multiple
        // actions (e.g. Invoke X + Fetch logs (X)) inside the same step
        // collapse into a single row in the feedback modal.
        const buckets = new Map();           // stepNo -> { stepNo, firstIdx, actions[] }
        let reasoningStep = 0;
        let synthetic = 0;                   // fallback when no "Reasoning Step" marker yet
        const bucketFor = (idx) => {
            const key = reasoningStep || (++synthetic + 1000);  // 1000+ marks pre-marker
            let b = buckets.get(key);
            if (!b) {
                b = { stepNo: reasoningStep || 0, firstIdx: idx, actions: [] };
                buckets.set(key, b);
            }
            return b;
        };

        steps.forEach((s, idx) => {
            const c = (s && s.content ? String(s.content) : '');
            const rm = c.match(/Reasoning Step\s+(\d+)\s*\/\s*\d+/);
            if (rm) { reasoningStep = parseInt(rm[1], 10) || reasoningStep + 1; return; }
            const im = c.match(invokeRe);
            if (im) {
                const sid = im[1].trim();
                const isTool = toolRe.test(sid);
                if (sid && !isTool && !skillSeen.has(sid)) {
                    skillSeen.add(sid); skillList.push(sid);
                }
                bucketFor(idx).actions.push({
                    kind: isTool ? 'tool' : 'invoke',
                    skill_id: isTool ? null : sid,
                    text: isTool ? `Tool ${sid}` : `Invoke ${sid}`,
                });
                return;
            }
            const fm = c.match(fetchRe);
            if (fm) {
                const sid = fm[1].trim();
                if (sid && !skillSeen.has(sid)) { skillSeen.add(sid); skillList.push(sid); }
                bucketFor(idx).actions.push({ kind: 'fetch_logs', skill_id: sid, text: `Fetch logs (${sid})` });
                return;
            }
            if (/Conclusion\s+reached/i.test(c)) {
                bucketFor(idx).actions.push({ kind: 'final_report', skill_id: null, text: 'Final report' });
            }
        });

        // Stable order by first occurrence; one stepList entry per bucket.
        const sorted = Array.from(buckets.values()).sort((a, b) => a.firstIdx - b.firstIdx);
        const stepList = sorted.map((b, i) => {
            const stepNo = b.stepNo || (i + 1);
            const primarySkill = (b.actions.find((a) => a.skill_id) || {}).skill_id || null;
            const actionText = b.actions.map((a) => a.text).join(' · ');
            const actions = b.actions.map((a) => ({ kind: a.kind, skill_id: a.skill_id }));
            return {
                step_index: b.firstIdx,
                step_no:    stepNo,
                skill_id:   primarySkill,
                actions:    actions,
                label:      `Step ${stepNo} — ${actionText || '(no action)'}`,
            };
        });
        window.__turnContext[turnId] = { skills: skillList, steps: stepList };
    }

    // ── Two-page wizard: route + per-skill menus ──────────────────────
    // Page 1 routes by problem type and reveals the matching menu(s);
    // Page 2 always collects the correct answer. The route maps directly
    // to ACE's dispatch hint:
    //   workflow → agent (workflow) playbook
    //   skill    → domain (per-skill) playbook
    //   both     → both
    const FBD_ROUTE_TO_LAYER = { workflow: 'agent', skill: 'skill', both: 'both' };

    function currentTurnSkills() {
        const turnId = (window.__currentFeedbackCtx || {}).turnId;
        const ctx = (turnId && window.__turnContext[turnId]) || { skills: [] };
        return Array.isArray(ctx.skills) ? ctx.skills : [];
    }
    function currentTurnSkillsKey() {
        const turnId = (window.__currentFeedbackCtx || {}).turnId || '';
        return turnId + '::' + currentTurnSkills().join('|');
    }
    function currentTurnSteps() {
        const turnId = (window.__currentFeedbackCtx || {}).turnId;
        const ctx = (turnId && window.__turnContext[turnId]) || { steps: [] };
        return Array.isArray(ctx.steps) ? ctx.steps : [];
    }
    function currentTurnStepsKey() {
        const turnId = (window.__currentFeedbackCtx || {}).turnId || '';
        return turnId + '::' + currentTurnSteps()
            .map((s) => `${s.step_index}:${s.skill_id || ''}`).join('|');
    }

    // Picking a route on Page 1 reveals only that route's menu(s) and
    // advances straight to Page 2 (the route's full interface). Pass
    // advance=false to restore state without leaving Page 1 (used by
    // prefill before the user re-confirms).
    function selectFeedbackRoute(route, advance = true) {
        window.__feedbackRoute = route;
        document.querySelectorAll('#fbd-route-cards .fbd-route-card').forEach((c) => {
            c.classList.toggle('selected', c.dataset.route === route);
        });
        const wfOn = (route === 'workflow' || route === 'both');
        const skOn = (route === 'skill' || route === 'both');
        const wf = document.getElementById('fbd-workflow-menu');
        const sk = document.getElementById('fbd-skill-menu');
        if (wf) wf.classList.toggle('hidden-row', !wfOn);
        if (sk) sk.classList.toggle('hidden-row', !skOn);
        if (wfOn) buildStepRows();
        if (skOn) buildSkillRows();
        if (advance && route) _setFeedbackPage(2);
    }

    // Skill lane — one verdict row per skill that ran. 👎 Wrong reveals a
    // reason + per-skill evidence box.
    function buildSkillRows() {
        const list = document.getElementById('fbd-skill-rows');
        if (!list || list.dataset.built === currentTurnSkillsKey()) return;
        const skills = currentTurnSkills();
        list.innerHTML = '';
        if (!skills.length) {
            list.innerHTML = '<div class="fbd-field-hint">No skills were invoked in this turn.</div>';
            list.dataset.built = currentTurnSkillsKey();
            updateSelectedCount('fbd-skill-rows', 'fbd-skill-count');
            return;
        }
        skills.forEach((sid) => {
            const safe = escapeHtml(String(sid));
            const row = document.createElement('div');
            row.className = 'fbd-skill-row';
            row.dataset.skillId = sid;
            row.innerHTML = `
                <div class="fbd-skill-row-head">
                    <span class="fbd-skill-row-name">${safe}</span>
                    <div class="fbd-skill-verdict">
                        <button type="button" class="fbd-verdict-btn" data-v="helpful" title="Helpful" aria-label="Mark this skill helpful" onclick="setSkillVerdict(this,'helpful')">👍</button>
                        <button type="button" class="fbd-verdict-btn" data-v="wrong" title="Not helpful" aria-label="Mark this skill not helpful" onclick="setSkillVerdict(this,'wrong')">👎</button>
                    </div>
                </div>
                <div class="fbd-skill-row-detail hidden-row">
                    <label class="fbd-sub-label fbd-reason-label">Notes on this skill</label>
                    <textarea class="fbd-skill-reason" rows="2"
                              placeholder="Anything it could have done better? (optional)"></textarea>
                    <label class="fbd-sub-label">What it should have concluded</label>
                    <textarea class="fbd-skill-should" rows="2"
                              placeholder="What it should have concluded, if different (optional)"></textarea>
                </div>`;
            list.appendChild(row);
        });
        list.dataset.built = currentTurnSkillsKey();
        updateSelectedCount('fbd-skill-rows', 'fbd-skill-count');
    }

    // Step lane — one verdict row per reasoning step that ran, mirroring
    // the Skill lane so the user can pin good/bad to a specific step.
    function buildStepRows() {
        const list = document.getElementById('fbd-step-rows');
        if (!list || list.dataset.built === currentTurnStepsKey()) return;
        const steps = currentTurnSteps();
        list.innerHTML = '';
        if (!steps.length) {
            list.innerHTML = '<div class="fbd-field-hint">No steps recorded for this turn.</div>';
            list.dataset.built = currentTurnStepsKey();
            updateSelectedCount('fbd-step-rows', 'fbd-step-count');
            return;
        }
        steps.forEach((st) => {
            const safe = escapeHtml(String(st.label || ''));
            const row = document.createElement('div');
            row.className = 'fbd-skill-row';
            row.dataset.stepIndex = String(st.step_index);
            if (st.skill_id) row.dataset.skillId = st.skill_id;
            row.innerHTML = `
                <div class="fbd-skill-row-head">
                    <span class="fbd-skill-row-name">${safe}</span>
                    <div class="fbd-skill-verdict">
                        <button type="button" class="fbd-verdict-btn" data-v="helpful" data-label="Good step" title="Good step" aria-label="Mark this step good" onmouseenter="showStepHint(this)" onmouseleave="resetStepHint(this)" onclick="setStepVerdict(this,'helpful')">👍</button>
                        <button type="button" class="fbd-verdict-btn" data-v="redundant" data-label="Redundant / unnecessary step" title="Redundant / unnecessary step" aria-label="Mark this step redundant or unnecessary" onmouseenter="showStepHint(this)" onmouseleave="resetStepHint(this)" onclick="setStepVerdict(this,'redundant')">♻️</button>
                        <button type="button" class="fbd-verdict-btn" data-v="wrong" data-label="Wrong step" title="Wrong step" aria-label="Mark this step wrong" onmouseenter="showStepHint(this)" onmouseleave="resetStepHint(this)" onclick="setStepVerdict(this,'wrong')">👎</button>
                    </div>
                </div>
                <div class="fbd-verdict-caption" aria-live="polite"></div>
                <div class="fbd-skill-row-detail hidden-row">
                    <label class="fbd-sub-label fbd-reason-label">Notes on this step</label>
                    <textarea class="fbd-skill-reason" rows="2"
                              placeholder="Anything it could have done better? Paste any relevant log line(s) (optional)"></textarea>
                    <label class="fbd-sub-label">What the agent should have done</label>
                    <textarea class="fbd-skill-should" rows="2"
                              placeholder="What the agent should have done, if different (optional)"></textarea>
                </div>`;
            list.appendChild(row);
        });
        list.dataset.built = currentTurnStepsKey();
        updateSelectedCount('fbd-step-rows', 'fbd-step-count');
    }

    // Update the "selected N/M" badge in a row group's header.
    function updateSelectedCount(listId, badgeId) {
        const list = document.getElementById(listId);
        const badge = document.getElementById(badgeId);
        if (!list || !badge) return;
        const rows = list.querySelectorAll('.fbd-skill-row');
        const total = rows.length;
        let picked = 0;
        rows.forEach((r) => { if (r.dataset.verdict) picked += 1; });
        badge.textContent = `selected ${picked}/${total}`;
        badge.classList.toggle('has-pick', picked > 0);
    }

    function setStepVerdict(btn, verdict) {
        const row = btn.closest('.fbd-skill-row');
        if (!row) return;
        const already = btn.classList.contains('active');
        const next = already ? '' : verdict;
        row.querySelectorAll('.fbd-verdict-btn').forEach((b) => b.classList.remove('active'));
        if (next) btn.classList.add('active');
        row.dataset.verdict = next;
        const detail = row.querySelector('.fbd-skill-row-detail');
        // Reveal the notes panel for ANY verdict (including 👍 helpful) so a
        // reviewer can leave an optional note; hide it only when the verdict
        // is cleared. 👍 uses neutral copy; other verdicts use direct copy.
        if (detail) {
            detail.classList.toggle('hidden-row', !next);
            const lbl = detail.querySelector('.fbd-reason-label');
            const ta  = detail.querySelector('.fbd-skill-reason');
            const pos = (next === 'helpful');
            if (lbl) lbl.textContent = pos ? 'Notes on this step' : 'What went wrong';
            if (ta)  ta.placeholder  = pos
                ? 'Anything it could have done better? Paste any relevant log line(s) (optional)'
                : 'Describe what went wrong — paste the relevant log line(s) if you have them (optional)';
        }
        _refreshStepCaption(row);
        updateSelectedCount('fbd-step-rows', 'fbd-step-count');
    }

    // Show a verdict's meaning below the buttons while hovering.
    function showStepHint(btn) {
        const row = btn.closest('.fbd-skill-row');
        if (!row) return;
        const cap = row.querySelector('.fbd-verdict-caption');
        if (cap) cap.textContent = btn.dataset.label || '';
    }

    // On mouse-out, fall back to the selected verdict's meaning (or clear).
    function resetStepHint(btn) {
        const row = btn.closest('.fbd-skill-row');
        if (row) _refreshStepCaption(row);
    }

    function _refreshStepCaption(row) {
        const cap = row.querySelector('.fbd-verdict-caption');
        if (!cap) return;
        const active = row.querySelector('.fbd-verdict-btn.active');
        cap.textContent = active ? (active.dataset.label || '') : '';
    }

    // Toggle a skill's verdict. Any verdict (including 👍 helpful) reveals the
    // notes box so a reviewer can leave an optional note; clicking the active
    // verdict again clears the verdict and hides the box.
    function setSkillVerdict(btn, verdict) {
        const row = btn.closest('.fbd-skill-row');
        if (!row) return;
        const already = btn.classList.contains('active');
        const next = already ? '' : verdict;
        row.querySelectorAll('.fbd-verdict-btn').forEach((b) => b.classList.remove('active'));
        if (next) btn.classList.add('active');
        row.dataset.verdict = next;   // '', 'helpful', or 'wrong'
        const detail = row.querySelector('.fbd-skill-row-detail');
        // Reveal the notes panel for any verdict so a 👍 helpful skill can
        // still carry an optional note; hide it only when the verdict is
        // cleared. 👍 uses neutral copy; other verdicts use direct copy.
        if (detail) {
            detail.classList.toggle('hidden-row', !next);
            const lbl = detail.querySelector('.fbd-reason-label');
            const ta  = detail.querySelector('.fbd-skill-reason');
            const pos = (next === 'helpful');
            if (lbl) lbl.textContent = pos ? 'Notes on this skill' : 'What went wrong';
            if (ta)  ta.placeholder  = pos
                ? 'Anything it could have done better? (optional)'
                : 'Describe what went wrong (optional)';
        }
        updateSelectedCount('fbd-skill-rows', 'fbd-skill-count');
    }

    // ── Page navigation ──────────────────────────────────────────────
    function _setFeedbackPage(page) {
        window.__feedbackPage = page;
        const p1 = document.getElementById('fbd-page-1');
        const p2 = document.getElementById('fbd-page-2');
        if (p1) p1.classList.toggle('hidden-row', page !== 1);
        if (p2) p2.classList.toggle('hidden-row', page !== 2);
        const show = (id, on) => {
            const el = document.getElementById(id);
            if (el) el.style.display = on ? '' : 'none';
        };
        show('fbd-back-btn',   page === 2);
        show('fbd-cancel-btn', page === 1);
        show('fbd-submit-btn', page === 2);
    }
    // Back returns to the route picker; the previously chosen card stays
    // highlighted so the user can re-enter or switch routes.
    function feedbackWizardBack() { _setFeedbackPage(1); }

    // Collect per-skill verdicts from the Skill lane into the backend's
    // skill_feedback shape:
    //   👎 Wrong → assessment: wrong  (+ reason + evidence)
    //   👍 Good  → assessment: helpful
    // (Workflow-level "ran more than needed" is captured by the overall
    // Investigation-flow dropdown's `over_investigated`, not per skill.)
    function collectSkillFeedback() {
        const bySkill = {};
        document.querySelectorAll('#fbd-skill-rows .fbd-skill-row').forEach((row) => {
            const sid = row.dataset.skillId || '';
            const v = row.dataset.verdict || '';
            if (!sid || !v || v === 'none') return;
            const reason = (row.querySelector('.fbd-skill-reason')?.value || '').trim();
            const should = (row.querySelector('.fbd-skill-should')?.value || '').trim();
            if (v === 'wrong' || v === 'helpful') {
                bySkill[sid] = { skill_id: sid, assessment: v,
                                 what_wrong: reason, should_be: should,
                                 evidence_lines: [] };
            }
        });
        return Object.values(bySkill);
    }

    // Per-step verdicts from the Agent-workflow lane. Same shape as
    // collectSkillFeedback but keyed by step_index so reviewers can pin
    // good/bad to a specific reasoning step.
    function collectStepFeedback() {
        const out = [];
        document.querySelectorAll('#fbd-step-rows .fbd-skill-row').forEach((row) => {
            const idxRaw = row.dataset.stepIndex;
            const v = row.dataset.verdict || '';
            if (!idxRaw || !v || v === 'none') return;
            const idx = parseInt(idxRaw, 10);
            if (!Number.isFinite(idx)) return;
            const sid = row.dataset.skillId || null;
            const label = (row.querySelector('.fbd-skill-row-name')?.textContent || '').trim();
            const reason = (row.querySelector('.fbd-skill-reason')?.value || '').trim();
            const should = (row.querySelector('.fbd-skill-should')?.value || '').trim();
            // Step lane sends an explicit verdict the user chose:
            //   👍 helpful | 🔁 redundant | 👎 wrong
            // (`negative` still accepted for back-compat with legacy drafts.)
            if (v === 'helpful' || v === 'redundant' || v === 'wrong' || v === 'negative') {
                out.push({ step_index: idx, skill_id: sid, step_label: label,
                           assessment: v, what_wrong: reason, should_be: should,
                           evidence_lines: [] });
            }
        });
        return out;
    }

    // ── Tone presets: 👍 vs 👎 wording ────────────────────────────────
    // Same form, same backend payload — only the display text shifts so
    // a thumbs-up reviewer isn't asked to label things as "wrong" or
    // "problems". Option *values* stay identical so ACE downstream
    // analytics keep working without a schema change.
    const FBD_TONE = {
        down: {
            title:           'More feedback',
            badge:           '👎',
            routeLabel:      'What kind of problem?',
            routes: {
                workflow: { title: 'Workflow',
                            desc:  'The investigation flow was off — e.g. order of steps, stopping too early, looping, or going down the wrong path.' },
                skill:    { title: 'Skill knowledge',
                            desc:  'The agent\'s domain knowledge was off — e.g. misread a log, applied a wrong rule, or drew a wrong conclusion from the evidence.' },
                both:     { title: 'Both',
                            desc:  "Both the workflow and the skill knowledge need fixing." },
            },
            grpFlowTitle:    'Agent workflow',
            flowFirstOpt:    '— Pick the main issue —',
            flowOpts: {
                missed_evidence:    'Missed key log evidence',
                wrong_conclusion:   'Wrong root cause / conclusion',
                hallucinated:       'Cited a log line that doesn\'t exist',
                stopped_too_early:  'Stopped too early / incomplete',
                over_investigated:  'Over-investigated unnecessarily',
                loop_or_stuck:      'Loop / stuck / repeated the same fetch',
                wrong_direction:    'Wrong direction — followed the wrong path',
                bad_output:         'Output unclear or misleading',
                other:              'Other',
            },
            grpSkillTitle:   'Which skill was wrong?',
            stepLaneHint:    'Per step: mark ♻️ Redundant (unnecessary) or 👎 Wrong (off-track), then add the correction. 👍 marks a good step.',
            skillLaneHint:   'Per skill: mark 👎 Wrong on any skill whose output was off and add the correction.',
            concLabel:       'Conclusion category',
            concFirstOpt:    '— Select if you know —',
            attachLogHint:   'Lets reviewers replay the exact log you analysed. Recommended when reporting a wrong answer.',
            submitText:      'Submit',
        },
        up: {
            title:           'What worked — and what could still sharpen up?',
            badge:           '👍',
            routeLabel:      'What to keep doing — and where to polish a little?',
            routes: {
                workflow: { title: 'Workflow',
                            desc:  'Investigation flow was mostly right — note any small step that could be tightened.' },
                skill:    { title: 'Skill knowledge',
                            desc:  'Domain knowledge held up — flag any small misread or shaky claim worth correcting.' },
                both:     { title: 'Both',
                            desc:  'Right answer, good path — call out small slips so the next run is even cleaner.' },
            },
            grpFlowTitle:    'Agent workflow',
            flowFirstOpt:    '— Pick the main issue —',
            flowOpts: {
                missed_evidence:    'Missed key log evidence',
                wrong_conclusion:   'Wrong root cause / conclusion',
                hallucinated:       'Cited a log line that doesn\'t exist',
                stopped_too_early:  'Stopped too early / incomplete',
                over_investigated:  'Over-investigated unnecessarily',
                loop_or_stuck:      'Loop / stuck / repeated the same fetch',
                wrong_direction:    'Wrong direction — followed the wrong path',
                bad_output:         'Output unclear or misleading',
                other:              'Other',
            },
            grpSkillTitle:   'Which skill did well?',
            stepLaneHint:    '',
            skillLaneHint:   'Mark 👍 knowledge is complete, 👎 knowledge is wrong or has gaps',
            concLabel:       'Conclusion category the agent nailed (optional)',
            concFirstOpt:    '— Select if it matched —',
            attachLogHint:   'Lets reviewers replay the same log the agent analysed — only attach if you want to share it.',
            submitText:      'Submit',
        },
    };

    function applyFeedbackTone(vote) {
        const key = (vote === 1) ? 'up' : 'down';
        window.__feedbackTone = key;
        const T = FBD_TONE[key];

        const setText = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.textContent = val;
        };
        const setPh = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.placeholder = val;
        };

        const titleEl = document.querySelector('#feedback-detail-modal .fbd-header h3');
        if (titleEl) titleEl.textContent = T.title;

        // Page-1 route cards: prompt + per-card title/description.
        setText('fbd-route-label', T.routeLabel);
        document.querySelectorAll('#fbd-route-cards .fbd-route-card').forEach((card) => {
            const meta = T.routes && T.routes[card.dataset.route];
            if (!meta) return;
            const tEl = card.querySelector('[data-route-title]');
            const dEl = card.querySelector('[data-route-desc]');
            if (tEl) tEl.textContent = meta.title;
            if (dEl) dEl.textContent = meta.desc;
        });

        // Workflow menu.
        const flowTitleEl = document.querySelector('#fbd-grp-flow-title > span:first-child');
        if (flowTitleEl) flowTitleEl.textContent = T.grpFlowTitle;

        // Skill menu.
        const skillTitleEl = document.querySelector('#fbd-grp-skill-title > span:first-child');
        if (skillTitleEl) skillTitleEl.textContent = T.grpSkillTitle;

        // Per-lane instruction hints — tone-aware so a 1 reviewer is
        // explicitly invited to flag small slips per step / per skill.
        setText('fbd-step-lane-hint',  T.stepLaneHint  || '');
        setText('fbd-skill-lane-hint', T.skillLaneHint || '');

        // Category dropdown label.
        setText('fbd-conc-label',        T.concLabel);

        // Conclusion-category first option text.
        const concSel = document.getElementById('fbd-correct-conclusion-tag');
        if (concSel && concSel.options.length > 0) {
            concSel.options[0].textContent = T.concFirstOpt;
        }

        // Investigation-flow dropdown: rewrite labels for the existing
        // values so the ACE schema stays unchanged.
        const flowSel = document.getElementById('fbd-agent-workflow');
        if (flowSel) {
            Array.from(flowSel.options).forEach((opt) => {
                if (!opt.value) { opt.textContent = T.flowFirstOpt; return; }
                if (T.flowOpts[opt.value]) opt.textContent = T.flowOpts[opt.value];
            });
        }

        // No attach-log row to describe any more; the footer note states that
        // the log is uploaded with every submission.

        // Submit button label.
        const submitBtn = document.getElementById('fbd-submit-btn');
        if (submitBtn) submitBtn.textContent = T.submitText;
    }

    // Issue-time feedback is collapsed by default — ticking the checkbox
    // expands the problem/correct-time fields; unticking clears them so a
    // stale value can't be submitted from a hidden field.
    function onIssueTimeFlagToggle() {
        const cb = document.getElementById('fbd-issue-time-flag');
        const detail = document.getElementById('fbd-issue-time-detail');
        const on = !!(cb && cb.checked);
        if (detail) detail.classList.toggle('hidden-row', !on);
        if (!on) {
            const sel = document.getElementById('fbd-issue-time-problem');
            const inp = document.getElementById('fbd-correct-issue-time');
            if (sel) sel.value = '';
            if (inp) inp.value = '';
        }
    }

    function openFeedbackModal(turnId, vote, linkEl) {
        window.__currentFeedbackCtx = {
            turnId, vote,
            conversationId: window.__feedbackConversationId || '',
            linkEl,
        };
        const badge = document.getElementById('fbd-vote-badge');
        badge.classList.remove('up', 'down');
        if (vote === 1) { badge.classList.add('up'); badge.textContent = '👍'; }
        else if (vote === -1) { badge.classList.add('down'); badge.textContent = '👎'; }
        else { badge.textContent = ''; }

        // Reset structured ACE feedback fields.
        ['fbd-correct-conclusion-tag',
         'fbd-agent-workflow',
         'fbd-correct-root-cause',
         'fbd-issue-time-problem',
         'fbd-correct-issue-time'].forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.value = '';
        });
        // Reset the wizard: clear the route, hide both menus, rebuild the
        // per-skill rows for THIS turn, and start on Page 1.
        window.__feedbackRoute = '';
        document.querySelectorAll('#fbd-route-cards .fbd-route-card')
                .forEach((c) => c.classList.remove('selected'));
        document.getElementById('fbd-workflow-menu')?.classList.add('hidden-row');
        document.getElementById('fbd-skill-menu')?.classList.add('hidden-row');
        // Force a rebuild of the per-skill and per-step rows for the current turn.
        const _sr = document.getElementById('fbd-skill-rows');
        if (_sr) { _sr.dataset.built = ''; _sr.innerHTML = ''; }
        const _st = document.getElementById('fbd-step-rows');
        if (_st) { _st.dataset.built = ''; _st.innerHTML = ''; }
        // Collapse the Issue Time detail (checkbox-gated, unticked by default).
        const _itFlag = document.getElementById('fbd-issue-time-flag');
        if (_itFlag) _itFlag.checked = false;
        const _itDetail = document.getElementById('fbd-issue-time-detail');
        if (_itDetail) _itDetail.classList.add('hidden-row');
        _setFeedbackPage(1);

        // Apply tone-specific labels / hints / option text before the
        // user sees anything. Must run after the reset block (which
        // recreates the empty issue row) so the new row is re-toned too.
        applyFeedbackTone(vote);

        // The attach-log checkbox no longer exists — the log always goes with
        // the feedback (main PR #139), so there is no default to reset here.

        // If the user already submitted feedback for this turn, pre-fill
        // every field with what they sent last time — so they can revise
        // and re-submit instead of starting from scratch.
        const cached = (window.__lastFeedback || {})[turnId];
        if (cached) prefillFeedbackModal(cached);

        // Draft auto-save: if the user closed the page or got interrupted
        // mid-edit on THIS turn, restore what they had typed. Draft wins
        // over cached submission (it's the more recent state).
        const draft = _loadFeedbackDraft(turnId);
        if (draft) {
            prefillFeedbackModal(draft);
            _showDraftHint('draft restored');
        } else {
            _hideDraftHint();
        }

        const modal = document.getElementById('feedback-detail-modal');
        modal.classList.add('open');
        modal.setAttribute('aria-hidden', 'false');
        document.getElementById('fbd-submit-btn').disabled = false;
        // Mark session active + expanded → drawer slides in on the RIGHT,
        // chat-area's right margin grows to leave room for it.
        document.body.classList.add('fbd-drawer-active', 'fbd-drawer-expanded', 'fbd-modal-open');
        // Auto-collapse the LEFT sidebar so the chat-area has breathing
        // room while feedback is open. Remember the user's previous
        // collapsed/expanded choice so we can restore it on close. The
        // hamburger button in the top-bar lets the user manually re-show
        // the sidebar even while the feedback drawer is open.
        _autoCollapseSidebarForFeedback();

        // One-time listener wiring: every field change inside the modal
        // dumps the current snapshot to localStorage. Debounced 300 ms
        // so we don't burn cycles on every keystroke.
        if (!modal.dataset.draftWired) {
            modal.dataset.draftWired = '1';
            ['input', 'change'].forEach((evtName) => {
                modal.addEventListener(evtName, () => {
                    const cur = window.__currentFeedbackCtx;
                    if (cur && cur.turnId) _saveFeedbackDraftDebounced(cur.turnId);
                });
            });
        }
    }

    // ── Draft auto-save (localStorage, per turn_id) ───────────────────
    const _FEEDBACK_DRAFT_PREFIX = 'feedback_draft_v1__';
    let _draftSaveTimer = null;

    function _saveFeedbackDraftDebounced(turnId) {
        if (!turnId) return;
        clearTimeout(_draftSaveTimer);
        _draftSaveTimer = setTimeout(() => _saveFeedbackDraftNow(turnId), 300);
    }
    function _saveFeedbackDraftNow(turnId) {
        try {
            const snap = _currentFeedbackFormSnapshot();
            if (!snap) return;
            localStorage.setItem(_FEEDBACK_DRAFT_PREFIX + turnId, JSON.stringify(snap));
        } catch (e) {}
    }
    function _loadFeedbackDraft(turnId) {
        try {
            const raw = localStorage.getItem(_FEEDBACK_DRAFT_PREFIX + turnId);
            return raw ? JSON.parse(raw) : null;
        } catch (e) { return null; }
    }
    function _clearFeedbackDraft(turnId) {
        try { localStorage.removeItem(_FEEDBACK_DRAFT_PREFIX + turnId); } catch (e) {}
    }
    function _showDraftHint(text) {
        const el = document.getElementById('fbd-draft-hint');
        if (!el) return;
        el.textContent = text;
        el.classList.add('show');
    }
    function _hideDraftHint() {
        const el = document.getElementById('fbd-draft-hint');
        if (el) el.classList.remove('show');
    }
    // Build the same snapshot shape that submitFeedbackDetail caches, but
    // straight from the live DOM — used by both the draft saver and the
    // no-change guard.
    function _currentFeedbackFormSnapshot() {
        const _val = (id) => {
            const el = document.getElementById(id);
            return el ? String(el.value || '').trim() : '';
        };
        return {
            route:                window.__feedbackRoute || '',
            correctConclusionTag: _val('fbd-correct-conclusion-tag'),
            agentWorkflow:        _val('fbd-agent-workflow'),
            correctRootCause:     _val('fbd-correct-root-cause'),
            issueTimeProblem:     _val('fbd-issue-time-problem'),
            correctIssueTime:     _val('fbd-correct-issue-time'),
            skillFeedback:        collectSkillFeedback(),
            stepFeedback:         collectStepFeedback(),
            attachLog:       true,
        };
    }

    // Restore every visible field from a previous submission cached in
    // window.__lastFeedback. Runs AFTER the modal's reset block so the
    // baseline defaults (route cleared, Page 1, etc.) are applied first
    // and then selectively overwritten.
    function prefillFeedbackModal(d) {
        const setVal = (id, v) => {
            const el = document.getElementById(id);
            if (el && v !== undefined && v !== null) el.value = v;
        };
        setVal('fbd-correct-conclusion-tag', d.correctConclusionTag || '');
        setVal('fbd-agent-workflow',         d.agentWorkflow || '');
        setVal('fbd-correct-root-cause',     d.correctRootCause || '');
        setVal('fbd-issue-time-problem',     d.issueTimeProblem || '');
        setVal('fbd-correct-issue-time',     d.correctIssueTime || '');
        // Auto-expand the Issue Time detail if a prior submission filled it.
        const _itHas = !!(d.issueTimeProblem || d.correctIssueTime);
        const _itFlag = document.getElementById('fbd-issue-time-flag');
        if (_itFlag) _itFlag.checked = _itHas;
        const _itDetail = document.getElementById('fbd-issue-time-detail');
        if (_itDetail) _itDetail.classList.toggle('hidden-row', !_itHas);

        // Restore the Page-1 route (which builds the per-skill + per-step
        // rows), then re-apply each verdict and its reason/evidence.
        if (d.route) selectFeedbackRoute(d.route);

        const sf = Array.isArray(d.skillFeedback) ? d.skillFeedback : [];
        sf.forEach((item) => {
            const sid = item && item.skill_id;
            if (!sid) return;
            if (item.assessment !== 'wrong' && item.assessment !== 'helpful') return;
            const row = Array.from(
                document.querySelectorAll('#fbd-skill-rows .fbd-skill-row'))
                .find((r) => r.dataset.skillId === sid) || null;
            if (!row) return;
            const btn = row.querySelector(`.fbd-verdict-btn[data-v="${item.assessment}"]`);
            if (btn) setSkillVerdict(btn, item.assessment);
            const r = row.querySelector('.fbd-skill-reason');
            const e = row.querySelector('.fbd-skill-should');
            if (r && item.what_wrong) r.value = item.what_wrong;
            if (e && item.should_be) e.value = item.should_be;
        });

        const stf = Array.isArray(d.stepFeedback) ? d.stepFeedback : [];
        stf.forEach((item) => {
            if (!item || item.step_index == null) return;
            if (!['wrong', 'redundant', 'helpful'].includes(item.assessment)) return;
            const row = document.querySelector(
                `#fbd-step-rows .fbd-skill-row[data-step-index="${item.step_index}"]`);
            if (!row) return;
            const btn = row.querySelector(`.fbd-verdict-btn[data-v="${item.assessment}"]`);
            if (btn) setStepVerdict(btn, item.assessment);
            const r = row.querySelector('.fbd-skill-reason');
            const e = row.querySelector('.fbd-skill-should');
            if (r && item.what_wrong) r.value = item.what_wrong;
            if (e && item.should_be) e.value = item.should_be;
        });

        // Nothing to restore for the log attachment: it is unconditional now,
        // so a saved draft's attachLog value no longer drives any control.
    }

    function closeFeedbackModal() {
        // ✕ / Cancel = "close for now". DOES NOT clear the draft —
        // the autosaved localStorage entry stays so the user can
        // re-open the same turn's More-feedback link later and pick
        // up exactly where they left off. The only paths that
        // actively erase a draft are:
        //   * Submit success (handled in submitFeedbackDetail) —
        //     fields are persisted on the server, draft no longer needed
        //   * Manual "Discard draft" affordance (not implemented yet)
        // This matters especially in multi-incident analyses: turn A's
        // draft must survive while the user briefly inspects turn B.
        _hideDraftHint();

        const modal = document.getElementById('feedback-detail-modal');
        modal.classList.remove('open');
        modal.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('fbd-drawer-active', 'fbd-drawer-expanded', 'fbd-modal-open');
        _restoreSidebarAfterFeedback();
        window.__currentFeedbackCtx = null;
    }

    // ── Top-bar height sync ──────────────────────────────────────────
    // Measure the real rendered top-bar height (which depends on font
    // metrics, padding and the box-shadow) and pin --fbd-topbar-height
    // to it. Guarantees the feedback right-sidebar slots exactly under
    // the blue bar with no gap and no overlap. Runs on load + resize.
    function _syncTopbarHeightVar() {
        const tb = document.querySelector('.top-bar');
        if (!tb) return;
        const h = Math.ceil(tb.getBoundingClientRect().height);
        document.documentElement.style.setProperty(
            '--fbd-topbar-height', h + 'px');
    }
    window.addEventListener('load', _syncTopbarHeightVar);
    window.addEventListener('resize', _syncTopbarHeightVar);

    // ── Left sidebar (LOG FILE / ISSUE TIME) collapse helpers ─────────
    // Manual toggle wired to the hamburger button in the top-bar.
    function toggleSidebar() {
        document.body.classList.toggle('sidebar-collapsed');
    }

    // Auto-collapse on feedback open. Remembers the user's previous
    // sidebar state so we can put it back when feedback closes. Null
    // means "no feedback session currently in flight".
    let _sidebarStateBeforeFeedback = null;
    function _autoCollapseSidebarForFeedback() {
        _sidebarStateBeforeFeedback =
            document.body.classList.contains('sidebar-collapsed');
        document.body.classList.add('sidebar-collapsed');
    }
    function _restoreSidebarAfterFeedback() {
        // If the user had the sidebar EXPANDED before opening feedback,
        // bring it back. If they had it collapsed already, leave it.
        // Either way, if they manually re-expanded the sidebar WHILE
        // feedback was open, their explicit choice wins — so we only
        // touch state when restoring the EXPANDED case.
        if (_sidebarStateBeforeFeedback === false) {
            document.body.classList.remove('sidebar-collapsed');
        }
        _sidebarStateBeforeFeedback = null;
    }

    // ── Top-of-page toast (also used by log-switch auto-reset) ────────
    function showToast({ message, actionLabel, onAction, ttlMs = 30000 }) {
        const stack = document.getElementById('toast-stack');
        if (!stack) return null;
        const el = document.createElement('div');
        el.className = 'toast';
        const msg = document.createElement('span');
        msg.className = 'toast-msg';
        msg.textContent = message;
        el.appendChild(msg);
        if (actionLabel && typeof onAction === 'function') {
            const btn = document.createElement('button');
            btn.className = 'toast-btn';
            btn.textContent = actionLabel;
            btn.onclick = () => { try { onAction(); } catch (e) {} el.remove(); };
            el.appendChild(btn);
        }
        const close = document.createElement('button');
        close.className = 'toast-close';
        close.textContent = '×';
        close.onclick = () => el.remove();
        el.appendChild(close);
        stack.appendChild(el);
        if (ttlMs > 0) setTimeout(() => { if (el.parentNode) el.remove(); }, ttlMs);
        return el;
    }

    // Canonical string form of a feedback snapshot — used to detect
    // "no-change" resubmits from the Edit-feedback flow so we don't
    // append duplicate JSONL rows for the same payload.
    function _normalizeFeedbackForCompare(d) {
        const d_ = d || {};
        return JSON.stringify({
            route:                d_.route || '',
            correctConclusionTag: d_.correctConclusionTag || '',
            agentWorkflow:        d_.agentWorkflow || '',
            correctRootCause:     d_.correctRootCause || '',
            issueTimeProblem:     d_.issueTimeProblem || '',
            correctIssueTime:     d_.correctIssueTime || '',
            skillFeedback: (Array.isArray(d_.skillFeedback) ? d_.skillFeedback : []).map((s) => ({
                skill_id:       s && s.skill_id   != null ? s.skill_id   : null,
                assessment:     s && s.assessment != null ? s.assessment : null,
                what_wrong:     s && s.what_wrong != null ? s.what_wrong : null,
                evidence_lines: s && Array.isArray(s.evidence_lines) ? s.evidence_lines.slice() : [],
            })),
            stepFeedback: (Array.isArray(d_.stepFeedback) ? d_.stepFeedback : []).map((s) => ({
                step_index:     s && s.step_index != null ? Number(s.step_index) : null,
                skill_id:       s && s.skill_id   != null ? s.skill_id   : null,
                step_label:     s && s.step_label != null ? s.step_label : '',
                assessment:     s && s.assessment != null ? s.assessment : null,
                what_wrong:     s && s.what_wrong != null ? s.what_wrong : null,
                evidence_lines: s && Array.isArray(s.evidence_lines) ? s.evidence_lines.slice() : [],
            })),
            attachLog:       !!d_.attachLog,
        });
    }

    async function submitFeedbackDetail() {
        const ctx = window.__currentFeedbackCtx;
        if (!ctx) return;

        const _val = (id) => {
            const el = document.getElementById(id);
            return el ? String(el.value || '').trim() : '';
        };
        const route                = window.__feedbackRoute || '';
        const correctConclusionTag = _val('fbd-correct-conclusion-tag');
        const agentWorkflow        = _val('fbd-agent-workflow');
        const correctRootCause     = _val('fbd-correct-root-cause');
        const issueTimeProblem     = _val('fbd-issue-time-problem');
        const correctIssueTime     = _val('fbd-correct-issue-time');
        // Issue time the turn actually used (current sidebar value) — captured
        // automatically so reviewers can compare it with the user's correction.
        const usedIssueTime        = (typeof getIssueTimeString === 'function') ? getIssueTimeString() : '';

        // Per-skill verdicts from the Skill lane and per-step verdicts
        // from the Agent-workflow lane. Both share the same wrong / helpful
        // shape; the backend treats them as parallel feedback streams.
        const skillFeedback = collectSkillFeedback();
        const stepFeedback  = collectStepFeedback();

        // Page-1 route → ACE dispatch hint (workflow→agent, skill→skill, both).
        const feedbackLayer = FBD_ROUTE_TO_LAYER[route] || '';

        const hasAny =
            correctConclusionTag
            || agentWorkflow
            || correctRootCause
            || issueTimeProblem
            || correctIssueTime
            || skillFeedback.length > 0
            || stepFeedback.length > 0;
        if (!hasAny) {
            closeFeedbackModal();
            return;
        }

        // The log is always attached now — the opt-in checkbox was removed and
        // the footer tells the user the upload happens (main PR #139). Kept as
        // a named constant so the no-change comparison below and the payloads
        // further down keep reading one value.
        const attachLog       = true;

        // No-change guard: when the user re-opens the modal via
        // "✏️ Edit feedback" and clicks Submit without touching
        // anything, the current payload is byte-identical to what we
        // already sent. Submitting again would append a duplicate row
        // to feedback_details.jsonl, so short-circuit here.
        const currentSnapshot = {
            route, correctConclusionTag, agentWorkflow, correctRootCause,
            issueTimeProblem, correctIssueTime, skillFeedback, stepFeedback,
            attachLog,
        };
        const cachedSnapshot = (window.__lastFeedback || {})[ctx.turnId];
        if (cachedSnapshot
            && _normalizeFeedbackForCompare(currentSnapshot)
                === _normalizeFeedbackForCompare(cachedSnapshot)) {
            closeFeedbackModal();
            return;
        }

        const submitBtn = document.getElementById('fbd-submit-btn');
        submitBtn.disabled = true;
        try {
            const res = await fetch('/feedback/detail', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    conversation_id:          ctx.conversationId,
                    turn_id:                  ctx.turnId,
                    vote:                     ctx.vote,
                    // Keep BT feedback in its separate bt_-prefixed storage.
                    ...(feedbackDomain ? {domain: feedbackDomain} : {}),
                    // High-ACE-value structured signals.
                    correct_conclusion_tag:   correctConclusionTag,
                    correct_root_cause:       correctRootCause,
                    // Agent-prompt-layer ACE signal: overall flow.
                    agent_workflow:           agentWorkflow,
                    // Explicit dispatch lane chosen on Page 1.
                    feedback_layer:           feedbackLayer,
                    // Per-skill verdicts + attributed reason / evidence.
                    skill_feedback:           skillFeedback,
                    // Per-step verdicts (Agent-workflow lane).
                    step_feedback:            stepFeedback,
                    // Issue-time (analysis anchor) feedback.
                    issue_time_problem:       issueTimeProblem,
                    correct_issue_time:       correctIssueTime,
                    used_issue_time:          usedIssueTime,
                    // Tells ACE how to read the issue-time fields above: when
                    // false the log has no dates, so used/correct issue times
                    // are time-only "HH:MM:SS" (e.g. DDD logs) — not malformed.
                    log_has_date:             (window.__logHasDate !== false),
                    attach_log:               attachLog,
                }),
            });
            const data = await res.json().catch(() => ({}));
            if (data && data.success) {
                // Cache every field the user filled in so reopening the
                // modal pre-fills exactly what they last submitted —
                // letting them tweak and re-submit without retyping.
                window.__lastFeedback = window.__lastFeedback || {};
                window.__lastFeedback[ctx.turnId] = {
                    route:                  route,
                    correctConclusionTag:   correctConclusionTag,
                    agentWorkflow:          agentWorkflow,
                    correctRootCause:       correctRootCause,
                    issueTimeProblem:       issueTimeProblem,
                    correctIssueTime:       correctIssueTime,
                    skillFeedback:          skillFeedback,
                    stepFeedback:           stepFeedback,
                    attachLog:              attachLog,
                };
                // Persisted to the server — draft no longer needed.
                _clearFeedbackDraft(ctx.turnId);
                if (ctx.linkEl) {
                    ctx.linkEl.classList.add('submitted');
                    ctx.linkEl.textContent = '✓ Edit feedback';
                    // Link stays clickable on purpose — clicking it
                    // reopens the modal pre-filled with the last
                    // submission so the user can revise & re-submit.
                    ctx.linkEl.onclick = () => openFeedbackModal(ctx.turnId, ctx.vote, ctx.linkEl);
                }
                closeFeedbackModal();
            } else {
                submitBtn.disabled = false;
                alert('Submit failed: ' + (data && data.error ? data.error : 'Unknown error'));
            }
        } catch (e) {
            submitBtn.disabled = false;
            alert('Network error: ' + e.message);
        }
    }
