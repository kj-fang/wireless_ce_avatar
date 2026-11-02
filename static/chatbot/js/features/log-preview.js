    const LOG_PREVIEW_API = (window.CHATBOT && window.CHATBOT.api) || '/log_chatbot';
    document.addEventListener('chatbot:result-rendered', event => attachKeyLogsToLastCard(event.detail));
    document.addEventListener('chatbot:context-changed', () => {
        const dialog = document.getElementById('key-log-modal');
        if (!dialog) return;
        if (dialog.open) dialog.close();
        else cleanupKeyLogModal(dialog);
    });

    function attachKeyLogsToLastCard(result) {
        const row = document.getElementById('chat-window').lastElementChild;
        const card = row && row.querySelector('.report-card, .msg-bubble');
        if (!card || card.querySelector('.key-log-footer')) return;
        const status = result.key_logs_basis === 'skill_filtered_compact'
            ? result.key_logs_status : 'legacy';
        const raw = status === 'selected' && typeof result.key_logs === 'string'
            ? result.key_logs : '';
        const footer = document.createElement('div');
        footer.className = 'key-log-footer';
        const button = makeKeyLogButton('file-text', 'View filtered skill logs', '\u2637');
        button.setAttribute('aria-haspopup', 'dialog');
        button.addEventListener('click', () => {
            if (window.ChatbotWorkspace?.active) window.ChatbotWorkspace.showEvidence(raw, button, status);
            else openKeyLogModal(raw, button, status);
        });
        footer.appendChild(button);
        card.appendChild(footer);
        refreshKeyLogIcons();
    }

    async function openTatPreviewFromSidebar(trigger, options = {}) {
        if (!options.host && window.ChatbotWorkspace?.active) return window.ChatbotWorkspace.showRaw();
        if (!logLoaded) {
            showStatus(document.getElementById('log-status'), 'Load a log file first to preview it.', 'err');
            return;
        }
        const dialog = openKeyLogModal('', trigger, 'preview', options);
        const sourceVersion = (dialog.__sourceVersion || 0) + 1;
        dialog.__sourceVersion = sourceVersion;
        const state = dialog.querySelector('.key-log-state');
        state.classList.remove('success');
        state.hidden = false;
        state.textContent = 'Starting background scan…';
        try {
            const response = await fetch(`${LOG_PREVIEW_API}/preview_log`, {cache: 'no-store'});
            const metadata = await response.json();
            if (dialog.__sourceVersion !== sourceVersion) return;
            if (!response.ok || !metadata.success) {
                throw new Error(metadata.error || 'Could not load the selected log.');
            }
            dialog.__status = 'preview';
            dialog.__source = {kind: 'server', url: metadata.chunk_url,
                size: metadata.size, name: metadata.name};
            dialog.__logName = metadata.name;
            dialog.querySelector('.key-log-log-name').textContent = dialog.__logName;
            startTatLogScan(dialog);
        } catch (error) {
            if (dialog.__sourceVersion !== sourceVersion) return;
            state.classList.remove('success');
            state.hidden = false;
            dialog.__previewError = true;
            state.textContent = error.message || 'Could not load the selected log.';
        }
        return dialog;
    }

    const TAT_VIRTUAL_ROW_HEIGHT = 19;
    const TAT_VIRTUAL_OVERSCAN = 40;
    const TAT_WORKER_CHUNK_BYTES = 1024 * 1024;

    function ensureVirtualLogView(dialog) {
        const content = dialog.querySelector('.key-log-content');
        content.classList.add('virtualized');
        if (dialog.__virtualSpacer) return;
        const spacer = document.createElement('div');
        spacer.className = 'key-log-virtual-spacer';
        content.replaceChildren(spacer);
        dialog.__virtualSpacer = spacer;
        if (!dialog.__virtualScrollBound) {
            content.addEventListener('scroll', () => requestVisibleLogRows(dialog), {passive: true});
            dialog.__virtualScrollBound = true;
            dialog.__resizeObserver = new ResizeObserver(() => requestVisibleLogRows(dialog));
            dialog.__resizeObserver.observe(content);
        }
    }

    function startTatLogScan(dialog) {
        if (!dialog.__source) return;
        if (dialog.__worker) dialog.__worker.terminate();
        dialog.__workerRunId = (dialog.__workerRunId || 0) + 1;
        dialog.__workerReady = false;
        dialog.__rowCount = 0;
        dialog.__candidateCount = 0;
        dialog.__lastVirtualRange = '';
        dialog.__virtualMode = true;
        dialog.__rawLogs = '';
        dialog.__previewError = false;
        dialog.__selectionStart = null;
        dialog.__selectionEnd = null;
        if (dialog.__addSelectionButton) dialog.__addSelectionButton.disabled = true;
        ensureVirtualLogView(dialog);
        const state = dialog.querySelector('.key-log-state');
        state.hidden = false;
        state.classList.remove('success');
        state.textContent = 'Starting background scan…';
        renderKeyLogContent(dialog);

        const worker = new Worker('/static/js/tat_log_preview_worker.js');
        dialog.__worker = worker;
        const runId = dialog.__workerRunId;
        worker.onmessage = event => {
            const message = event.data || {};
            if (message.runId !== dialog.__workerRunId) return;
            if (message.type === 'progress') {
                const state = dialog.querySelector('.key-log-state');
                state.hidden = false;
                state.classList.remove('success');
                state.textContent = `Scanning ${Number(message.candidateCount || 0).toLocaleString()} lines…`;
            } else if (message.type === 'ready') {
                dialog.__workerReady = true;
                dialog.__rowCount = message.rowCount;
                dialog.__candidateCount = message.candidateCount;
                dialog.__virtualSpacer.style.height = `${message.rowCount * TAT_VIRTUAL_ROW_HEIGHT}px`;
                dialog.querySelector('.key-log-content').scrollTop = 0;
                renderKeyLogContent(dialog);
                requestVisibleLogRows(dialog);
            } else if (message.type === 'range') {
                if (message.requestId !== dialog.__lastRangeRequestId) return;
                renderVirtualLogRows(dialog, message.start, message.rows || []);
            } else if (message.type === 'selection') {
                if (message.requestId !== dialog.__selectionRequestId) return;
                if (message.error) {
                    state.hidden = false;
                    state.classList.remove('success');
                    state.textContent = message.error;
                } else if (message.rows.length && window.ChatbotSnippets) {
                    window.ChatbotSnippets.select({name: dialog.__logName || 'Analysis evidence', rows: message.rows});
                }
            } else if (message.type === 'error') {
                dialog.__workerReady = false;
                const state = dialog.querySelector('.key-log-state');
                state.hidden = false;
                state.classList.remove('success');
                state.textContent = message.message || 'Could not scan this log.';
            }
        };

        const rules = (dialog.__tatRules || []).map(rule => ({
            text: rule.text, enabled: rule.enabled, excluding: rule.excluding,
            regex: rule.regex, foreground: rule.foreground, background: rule.background,
        }));
        worker.postMessage({type: 'start', runId, source: dialog.__source,
            rules, chunkSize: TAT_WORKER_CHUNK_BYTES});
    }

    function requestVisibleLogRows(dialog) {
        if (!dialog.__worker || !dialog.__workerReady || !dialog.__rowCount) return;
        const content = dialog.querySelector('.key-log-content');
        if (!content.clientHeight) return;
        const start = Math.max(0, Math.floor(content.scrollTop / TAT_VIRTUAL_ROW_HEIGHT) - TAT_VIRTUAL_OVERSCAN);
        const end = Math.min(dialog.__rowCount,
            Math.ceil((content.scrollTop + content.clientHeight) / TAT_VIRTUAL_ROW_HEIGHT) + TAT_VIRTUAL_OVERSCAN);
        const rangeKey = `${start}:${end}`;
        if (rangeKey === dialog.__lastVirtualRange) return;
        dialog.__lastVirtualRange = rangeKey;
        dialog.__lastRangeRequestId = (dialog.__lastRangeRequestId || 0) + 1;
        dialog.__worker.postMessage({type: 'range', runId: dialog.__workerRunId,
            requestId: dialog.__lastRangeRequestId, start, count: end - start});
    }

    function renderVirtualLogRows(dialog, start, rows) {
        const spacer = dialog.__virtualSpacer;
        spacer.replaceChildren();
        rows.forEach((row, offset) => {
            const element = document.createElement('div');
            element.className = 'key-log-virtual-row';
            element.dataset.rowIndex = String(start + offset);
            element.dataset.lineNumber = String(row.lineNumber);
            element.style.top = `${(start + offset) * TAT_VIRTUAL_ROW_HEIGHT}px`;
            const number = document.createElement('span');
            number.className = 'key-log-line-number';
            number.textContent = String(row.lineNumber);
            const text = document.createElement('span');
            text.className = 'key-log-line-text';
            text.textContent = row.text.replace(/[\r\n]+$/, '');
            element.append(number, text);
            if (row.foreground) element.style.color = row.foreground;
            if (row.background) element.style.backgroundColor = row.background;
            spacer.appendChild(element);
        });
        updateKeyLogSelection(dialog);
    }

    function updateKeyLogSelection(dialog) {
        const start = dialog.__selectionStart;
        const end = dialog.__selectionEnd;
        dialog.querySelectorAll('.key-log-virtual-row').forEach(element => {
            const index = Number(element.dataset.rowIndex);
            element.classList.toggle('is-selected', start !== null && start !== undefined && index >= Math.min(start, end) && index <= Math.max(start, end));
        });
        if (dialog.__addSelectionButton) dialog.__addSelectionButton.disabled = start === null || start === undefined;
    }

    function enableKeyLogSelection(dialog) {
        if (dialog.__addSelectionButton) return;
        const add = makeKeyLogButton('message-square-plus', 'Add selected log to question', '+');
        add.classList.add('key-log-filter-action');
        const label = document.createElement('span');
        label.textContent = 'Add to question';
        add.appendChild(label);
        add.disabled = true;
        dialog.__addSelectionButton = add;
        dialog.querySelector('.key-log-pane-heading').appendChild(add);
        const addSelection = () => {
            if (!dialog.__workerReady || dialog.__selectionStart === null || dialog.__selectionStart === undefined) return;
            dialog.__selectionRequestId = (dialog.__selectionRequestId || 0) + 1;
            dialog.__worker.postMessage({type: 'selection', runId: dialog.__workerRunId,
                requestId: dialog.__selectionRequestId, start: dialog.__selectionStart, end: dialog.__selectionEnd});
        };
        add.addEventListener('click', addSelection);
        const content = dialog.querySelector('.key-log-content');
        const isSelected = index => dialog.__selectionStart !== null && dialog.__selectionStart !== undefined
            && index >= Math.min(dialog.__selectionStart, dialog.__selectionEnd)
            && index <= Math.max(dialog.__selectionStart, dialog.__selectionEnd);
        // Range collapsed by the first click of a possible double-click, restored if the second click follows.
        let restorable = null;
        let lastDown = {time: 0, index: -1};
        content.addEventListener('dblclick', event => {
            const row = event.target.closest('.key-log-virtual-row');
            if (row && isSelected(Number(row.dataset.rowIndex))) addSelection();
        });
        content.addEventListener('pointerdown', event => {
            if (event.button !== 0 || !dialog.__workerReady) return;
            const row = event.target.closest('.key-log-virtual-row');
            if (!row) return;
            const index = Number(row.dataset.rowIndex);
            event.preventDefault();
            // PointerEvent.detail is always 0 in Chromium, so track repeat clicks manually.
            const repeat = !event.shiftKey && lastDown.index === index && event.timeStamp - lastDown.time < 500;
            lastDown = repeat ? {time: 0, index: -1} : {time: event.timeStamp, index};
            if (repeat && restorable?.index === index) {
                dialog.__selectionStart = restorable.start;
                dialog.__selectionEnd = restorable.end;
                restorable = null;
                updateKeyLogSelection(dialog);
                return;
            }
            if (repeat && isSelected(index)) return;
            restorable = !event.shiftKey && isSelected(index) && dialog.__selectionStart !== dialog.__selectionEnd
                ? {index, start: dialog.__selectionStart, end: dialog.__selectionEnd} : null;
            if (!event.shiftKey || dialog.__selectionStart === null) dialog.__selectionStart = index;
            dialog.__selectionEnd = index;
            dialog.__selecting = true;
            updateKeyLogSelection(dialog);
        });
        content.addEventListener('pointermove', event => {
            if (!dialog.__selecting || !(event.buttons & 1)) return;
            const row = event.target.closest('.key-log-virtual-row');
            if (!row) return;
            const index = Number(row.dataset.rowIndex);
            if (index !== dialog.__selectionEnd) restorable = null;
            dialog.__selectionEnd = index;
            updateKeyLogSelection(dialog);
        });
        document.addEventListener('pointerup', () => { dialog.__selecting = false; });
    }

    function makeKeyLogButton(icon, label, fallback) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'key-log-button';
        button.title = label;
        button.setAttribute('aria-label', label);
        if (window.lucide) {
            const glyph = document.createElement('i');
            glyph.dataset.lucide = icon;
            glyph.setAttribute('aria-hidden', 'true');
            button.appendChild(glyph);
        } else {
            button.textContent = fallback;
        }
        return button;
    }

    function refreshKeyLogIcons() {
        if (window.lucide) window.lucide.createIcons();
    }
    refreshKeyLogIcons();

    function parseTatFilterRules(xmlText) {
        const documentXml = new DOMParser().parseFromString(xmlText, 'application/xml');
        if (documentXml.querySelector('parsererror')) throw new Error('The selected .tat file is not valid XML.');
        const elements = Array.from(documentXml.getElementsByTagName('*'))
            .filter(element => (element.localName || element.tagName).toLowerCase() === 'filter');
        const rules = elements
            .map(element => ({
                text: (element.getAttribute('text') || '').trim(),
                enabled: (element.getAttribute('enabled') || 'n').toLowerCase() === 'y',
                excluding: (element.getAttribute('excluding') || 'n').toLowerCase() === 'y',
                regex: (element.getAttribute('regex') || 'n').toLowerCase() === 'y',
                foreground: getTatColor(element, [
                    'foreground', 'foregroundcolor', 'forecolor', 'textcolor', 'color',
                ]),
                background: getTatColor(element, [
                    'background', 'backgroundcolor', 'backcolor', 'bgcolor',
                ]),
            }))
            .filter(rule => rule.text);
        if (!rules.length) throw new Error('The .tat file has no filter rules.');
        rules.forEach(rule => {
            if (rule.regex) {
                try { rule.matcher = new RegExp(rule.text, 'i'); }
                catch { throw new Error(`Invalid .tat regular expression: ${rule.text}`); }
            }
        });
        return rules;
    }

    function getTatColor(element, attributeNames) {
        const attributes = Array.from(element.attributes);
        for (const name of attributeNames) {
            const attribute = attributes.find(item => item.name.toLowerCase() === name.toLowerCase());
            const value = (attribute ? attribute.value : '').trim();
            if (!value) continue;
            const cssColor = normalizeTatColor(value);
            if (cssColor) return cssColor;
        }
        return '';
    }

    function normalizeTatColor(value) {
        if (window.CSS && CSS.supports('color', value)) return value;
        const hex = value.replace(/^#/, '');
        if (/^[0-9a-f]{6}$/i.test(hex)) return `#${hex}`;
        if (/^[0-9a-f]{8}$/i.test(hex)) {
            const alpha = parseInt(hex.slice(0, 2), 16) / 255;
            return `rgba(${parseInt(hex.slice(2, 4), 16)}, ${parseInt(hex.slice(4, 6), 16)}, ${parseInt(hex.slice(6, 8), 16)}, ${alpha.toFixed(3)})`;
        }
        if (/^-?\d+$/.test(value)) {
            const argb = Number(value) >>> 0;
            const alpha = (argb >>> 24) & 255;
            const red = (argb >>> 16) & 255;
            const green = (argb >>> 8) & 255;
            const blue = argb & 255;
            return alpha === 255
                ? `rgb(${red}, ${green}, ${blue})`
                : `rgba(${red}, ${green}, ${blue}, ${(alpha / 255).toFixed(3)})`;
        }
        return '';
    }

    function splitLogLinesPreservingEndings(text) {
        const lines = [];
        let start = 0;
        for (let index = 0; index < text.length; index++) {
            if (text[index] !== '\r' && text[index] !== '\n') continue;
            if (text[index] === '\r' && text[index + 1] === '\n') index++;
            lines.push(text.slice(start, index + 1));
            start = index + 1;
        }
        if (start < text.length) lines.push(text.slice(start));
        return lines;
    }

    function applyTatRulesToLog(text, rules) {
        const sections = [];
        let heading = '';
        let matchedLines = [];
        let candidateCount = 0;
        let matchedCount = 0;
        const activeRules = rules.filter(rule => rule.enabled);
        const includeRules = activeRules.filter(rule => !rule.excluding);
        const excludeRules = activeRules.filter(rule => rule.excluding);
        const matches = (line, rule) => rule.matcher
            ? rule.matcher.test(line)
            : line.toLowerCase().includes(rule.text.toLowerCase());

        function flushSection() {
            if (matchedLines.length) {
                sections.push({heading, lines: matchedLines});
            }
            heading = '';
            matchedLines = [];
        }

        for (const originalLine of splitLogLinesPreservingEndings(text)) {
            const line = originalLine.replace(/[\r\n]+$/, '');
            if (/^=== .* ===$/.test(line.trim())) {
                flushSection();
                heading = originalLine;
                continue;
            }
            if (!line.trim()) continue;
            candidateCount++;
            const matchingInclude = includeRules.find(rule => matches(line, rule));
            if (!matchingInclude || excludeRules.some(rule => matches(line, rule))) continue;
            matchedLines.push({text: originalLine, foreground: matchingInclude.foreground,
                background: matchingInclude.background});
            matchedCount++;
        }
        flushSection();
        const filteredText = sections.map(section => section.heading
            + section.lines.map(line => line.text).join('')).join('\n');
        return {sections, text: filteredText, candidateCount, matchedCount};
    }

    function renderTatFilterOptions(dialog) {
        const list = dialog.querySelector('.key-log-filter-list');
        const count = dialog.querySelector('.key-log-filter-count');
        list.replaceChildren();
        const rules = dialog.__tatRules || [];
        const enabledCount = rules.filter(rule => rule.enabled).length;
        count.textContent = rules.length ? `(${enabledCount} enabled)` : '';
        if (!rules.length) {
            const empty = document.createElement('div');
            empty.className = 'key-log-filter-empty';
            empty.textContent = 'Load a .tat file to view its filters.';
            list.appendChild(empty);
            return;
        }
        rules.forEach((rule, index) => {
            const label = document.createElement('label');
            label.className = 'key-log-filter-rule';
            const mod = document.createElement('span');
            mod.className = 'key-log-filter-mod';
            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.checked = rule.enabled;
            checkbox.setAttribute('aria-label', `Enable ${rule.text}`);
            checkbox.addEventListener('change', () => {
                rule.enabled = checkbox.checked;
                renderTatFilterOptions(dialog);
                if (dialog.__source) startTatLogScan(dialog);
                else renderKeyLogContent(dialog);
            });
            const letter = document.createElement('span');
            letter.className = 'key-log-filter-letter';
            let letterIndex = index;
            do {
                letter.textContent = String.fromCharCode(97 + (letterIndex % 26)) + letter.textContent;
                letterIndex = Math.floor(letterIndex / 26) - 1;
            } while (letterIndex >= 0);
            mod.append(checkbox, letter);
            label.appendChild(mod);
            const pattern = document.createElement('span');
            pattern.className = 'key-log-filter-pattern';
            if (rule.excluding) {
                const badge = document.createElement('span');
                badge.className = 'key-log-filter-kind';
                badge.textContent = 'EXCL';
                pattern.appendChild(badge);
            }
            const text = document.createElement('span');
            text.className = 'key-log-filter-rule-text';
            text.textContent = rule.text;
            text.title = rule.text;
            if (rule.foreground) text.style.color = rule.foreground;
            if (rule.background) text.style.backgroundColor = rule.background;
            pattern.appendChild(text);
            if (rule.regex) {
                const kind = document.createElement('span');
                kind.className = 'key-log-filter-kind';
                kind.textContent = 'REGEX';
                pattern.appendChild(kind);
            }
            label.appendChild(pattern);
            list.appendChild(label);
        });
    }

    function renderTatFilteredSections(content, sections) {
        content.replaceChildren();
        sections.forEach((section, sectionIndex) => {
            if (sectionIndex) content.appendChild(document.createTextNode('\n'));
            if (section.heading) content.appendChild(document.createTextNode(section.heading));
            section.lines.forEach(line => {
                const span = document.createElement('span');
                span.textContent = line.text;
                if (line.foreground) span.style.color = line.foreground;
                if (line.background) span.style.backgroundColor = line.background;
                content.appendChild(span);
            });
        });
    }

    function renderKeyLogContent(dialog) {
        const content = dialog.querySelector('.key-log-content');
        const state = dialog.querySelector('.key-log-state');
        if (dialog.__virtualMode) {
            ensureVirtualLogView(dialog);
            if (!dialog.__workerReady) {
                dialog.__virtualSpacer.replaceChildren();
                dialog.__virtualSpacer.style.height = '0px';
                return;
            }
            dialog.__virtualSpacer.style.height = `${dialog.__rowCount * TAT_VIRTUAL_ROW_HEIGHT}px`;
            if (!dialog.__rowCount) {
                const empty = document.createElement('div');
                empty.className = 'key-log-virtual-empty';
                empty.textContent = dialog.__tatRules && dialog.__tatRules.some(rule => rule.enabled)
                    ? 'No log lines match the enabled TAT filters.' : 'No log lines to display.';
                dialog.__virtualSpacer.style.height = '100%';
                dialog.__virtualSpacer.replaceChildren(empty);
                state.hidden = false;
                state.classList.add('success');
                state.textContent = `${Number(dialog.__candidateCount || 0).toLocaleString()} candidate lines · 0 shown`;
                return;
            }
            state.hidden = false;
            state.classList.add('success');
            state.textContent = dialog.__tatRules && dialog.__tatRules.length
                ? `${dialog.__tatName}: ${Number(dialog.__rowCount).toLocaleString()} / ${Number(dialog.__candidateCount).toLocaleString()} log lines`
                : `${Number(dialog.__rowCount).toLocaleString()} log lines`;
            return;
        }
        const emptyMessages = {
            no_evidence: 'No skill-filtered log results were used in this response.',
            cancelled: 'Analysis was cancelled before filtered log results were returned.',
            legacy: 'No compact skill-filtered results were saved for this older response.',
            preview: 'Loading the selected log…',
        };
        if (!dialog.__rawLogs) {
            content.textContent = emptyMessages[dialog.__status] || emptyMessages.legacy;
            state.hidden = !dialog.__previewError;
            state.classList.remove('success');
            state.textContent = '';
            return;
        }

        let visibleText = dialog.__rawLogs;
        let filteredSections = null;
        if (dialog.__tatRules && dialog.__tatRules.length) {
            const filtered = applyTatRulesToLog(dialog.__rawLogs, dialog.__tatRules);
            visibleText = filtered.text;
            filteredSections = filtered.sections;
            state.hidden = false;
            state.classList.add('success');
            state.textContent = `${dialog.__tatName}: ${filtered.matchedCount} / ${filtered.candidateCount} log lines`;
            if (!visibleText) visibleText = 'No log lines match this .tat filter.';
        } else {
            state.hidden = true;
            state.classList.remove('success');
            state.textContent = '';
        }
        const hasVisibleLogs = !visibleText.startsWith('No log lines match this .tat filter.');
        if (filteredSections && hasVisibleLogs) {
            renderTatFilteredSections(content, filteredSections);
        } else {
            content.textContent = visibleText;
        }
        content.scrollTop = 0;
        content.scrollLeft = 0;
    }

    function removeKeyLogRestoreLauncher(dialog) {
        if (!dialog.__restoreLauncher) return;
        dialog.__restoreLauncher.remove();
        dialog.__restoreLauncher = null;
    }

    function cleanupKeyLogModal(dialog) {
        dialog.__sourceVersion = (dialog.__sourceVersion || 0) + 1;
        dialog.__workerRunId = (dialog.__workerRunId || 0) + 1;
        removeKeyLogRestoreLauncher(dialog);
        if (dialog.__worker) dialog.__worker.terminate();
        dialog.__worker = null;
        dialog.__source = null;
        dialog.__virtualMode = false;
        dialog.__workerReady = false;
        dialog.__virtualSpacer = null;
        dialog.__selectionStart = null;
        dialog.__selectionEnd = null;
        if (dialog.__addSelectionButton) dialog.__addSelectionButton.disabled = true;
        dialog.querySelector('.key-log-content').classList.remove('virtualized');
        dialog.querySelector('pre').textContent = '';
        dialog.__rawLogs = '';
        dialog.__previewError = false;
        dialog.__logName = '';
        dialog.querySelector('.key-log-log-name').textContent = '';
        if (!dialog.closest('.workspace-pane') && dialog.__trigger && dialog.__trigger.isConnected) dialog.__trigger.focus();
    }

    function showKeyLogRestoreLauncher(dialog) {
        if (dialog.__restoreLauncher) return;
        const launcher = document.createElement('div');
        launcher.className = 'key-log-restore-launcher';
        const restore = makeKeyLogButton('maximize-2', 'Restore log viewer', '\u2197');
        restore.classList.add('key-log-restore-action');
        const label = document.createElement('span');
        label.textContent = dialog.__logName || 'Log viewer';
        restore.appendChild(label);
        restore.addEventListener('click', () => {
            removeKeyLogRestoreLauncher(dialog);
            dialog.returnValue = '';
            dialog.showModal();
            dialog.querySelector('.key-log-content').focus({preventScroll: true});
        });
        const close = makeKeyLogButton('x', 'Close log viewer', '\u00d7');
        close.addEventListener('click', () => cleanupKeyLogModal(dialog));
        launcher.append(restore, close);
        document.body.appendChild(launcher);
        dialog.__restoreLauncher = launcher;
        refreshKeyLogIcons();
    }

    function openKeyLogModal(raw, trigger, status = 'legacy', options = {}) {
        let dialog = document.getElementById(options.id || 'key-log-modal');
        if (!dialog) {
            dialog = document.createElement('dialog');
            dialog.id = options.id || 'key-log-modal';
            dialog.className = 'key-log-modal';
            dialog.setAttribute('aria-labelledby', 'key-log-title');
            dialog.innerHTML = '<div class="key-log-heading"><h2 id="key-log-title">Filtered Skill Logs</h2></div><div class="key-log-layout"><section class="key-log-view"><div class="key-log-pane-heading"><span>Log Viewer</span><span class="key-log-log-name"></span><div class="key-log-state" role="status" hidden></div></div><pre class="key-log-content" tabindex="0"></pre></section><aside class="key-log-filter-panel"><div class="key-log-filter-header"><span class="key-log-filter-title">TAT Filter</span></div><div class="key-log-filter-table"><div class="key-log-filter-head"><span>Mod</span><span>Pattern <span class="key-log-filter-count"></span></span></div><div class="key-log-filter-list"></div></div></aside></div>';
            const title = dialog.querySelector('h2');
            title.id = `${dialog.id}-title`;
            dialog.setAttribute('aria-labelledby', title.id);
            const heading = dialog.querySelector('.key-log-heading');
            const filterPanel = dialog.querySelector('.key-log-filter-panel');
            const filterHeader = dialog.querySelector('.key-log-filter-header');
            const filterName = document.createElement('span');
            filterName.className = 'key-log-filter-name';
            filterName.textContent = 'No .tat file loaded';
            const loadTat = makeKeyLogButton('folder-open', 'Load a .tat filter', '\u25a4');
            loadTat.classList.add('key-log-filter-action');
            const loadLabel = document.createElement('span');
            loadLabel.textContent = 'Load .tat';
            loadTat.appendChild(loadLabel);
            const fileInput = document.createElement('input');
            fileInput.type = 'file';
            fileInput.accept = '.tat,application/xml,text/xml';
            fileInput.hidden = true;
            loadTat.addEventListener('click', () => fileInput.click());
            fileInput.addEventListener('change', async () => {
                const file = fileInput.files && fileInput.files[0];
                if (!file) return;
                try {
                    dialog.__tatRules = parseTatFilterRules(await file.text());
                    dialog.__tatName = file.name;
                    filterName.textContent = file.name;
                    renderTatFilterOptions(dialog);
                    if (dialog.__source) startTatLogScan(dialog);
                    else renderKeyLogContent(dialog);
                } catch (error) {
                    const state = dialog.querySelector('.key-log-state');
                    state.classList.remove('success');
                    state.hidden = false;
                    state.textContent = error.message || 'Could not load the .tat filter.';
                } finally {
                    fileInput.value = '';
                }
            });
            filterHeader.append(loadTat, filterName);
            filterPanel.appendChild(fileInput);
            renderTatFilterOptions(dialog);
            const close = makeKeyLogButton('x', 'Close filtered skill logs', '\u00d7');
            close.addEventListener('click', () => dialog.close());
            const minimize = makeKeyLogButton('minus', 'Minimize log viewer', '\u2212');
            minimize.addEventListener('click', () => dialog.close('minimize'));
            heading.append(minimize, close);
            dialog.addEventListener('keydown', event => {
                if (event.key === 'Escape') {
                    event.preventDefault();
                    event.stopPropagation();
                    dialog.close();
                }
            });
            dialog.addEventListener('click', event => {
                if (event.target !== dialog) return;
                const bounds = dialog.getBoundingClientRect();
                if (event.clientX < bounds.left || event.clientX > bounds.right ||
                    event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
            });
            dialog.addEventListener('close', () => {
                if (dialog.returnValue === 'minimize') {
                    dialog.returnValue = '';
                    showKeyLogRestoreLauncher(dialog);
                    return;
                }
                cleanupKeyLogModal(dialog);
            });
            (options.host || document.body).appendChild(dialog);
        }
        removeKeyLogRestoreLauncher(dialog);
        if (dialog.__worker) dialog.__worker.terminate();
        dialog.__worker = null;
        dialog.__source = null;
        dialog.__virtualMode = false;
        dialog.__workerReady = false;
        dialog.__virtualSpacer = null;
        dialog.querySelector('.key-log-content').classList.remove('virtualized');
        dialog.__rawLogs = raw;
        dialog.__previewError = false;
        dialog.__status = status;
        dialog.__trigger = trigger;
        dialog.__logName = '';
        dialog.querySelector('.key-log-log-name').textContent = '';
        dialog.querySelector('.key-log-heading h2').textContent = status === 'preview'
            ? 'TAT Log Preview' : 'Filtered Skill Logs';
        renderKeyLogContent(dialog);
        refreshKeyLogIcons();
        if (!options.host && !dialog.open) dialog.showModal();
        if (options.host) {
            enableKeyLogSelection(dialog);
            if (raw) {
                dialog.__logName = 'Analysis evidence';
                dialog.querySelector('.key-log-log-name').textContent = dialog.__logName;
                dialog.__source = {kind: 'file', file: new File([raw], 'analysis-evidence.log'), name: dialog.__logName};
                startTatLogScan(dialog);
            }
        }
        return dialog;
    }

