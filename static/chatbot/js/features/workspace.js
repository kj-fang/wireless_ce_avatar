(function () {
    const layout = document.querySelector('.main-layout');
    const toggle = document.getElementById('advanced-mode-toggle');
    const chat = layout.querySelector('.chat-area');
    const pane = document.createElement('section');
    pane.className = 'workspace-pane';
    pane.hidden = true;
    pane.setAttribute('aria-label', 'Log workspace');
    pane.innerHTML = '<div class="workspace-tabs" role="tablist" aria-label="Log source"><button type="button" role="tab" aria-selected="true" data-view="raw">Original log</button><button type="button" role="tab" aria-selected="false" data-view="evidence">Analysis evidence</button></div><div class="workspace-view-host" data-host="raw"></div><div class="workspace-view-host" data-host="evidence" hidden></div>';
    const hosts = {raw: pane.querySelector('[data-host="raw"]'), evidence: pane.querySelector('[data-host="evidence"]')};
    let active = false;
    let rawPath = '';
    let currentView = 'raw';

    function selectView(name) {
        currentView = name;
        for (const [key, host] of Object.entries(hosts)) host.hidden = key !== name;
        pane.querySelectorAll('[data-view]').forEach(button => button.setAttribute('aria-selected', String(button.dataset.view === name)));
        const viewer = hosts[name].firstElementChild;
        if (viewer) {
            viewer.__lastVirtualRange = '';
            requestVisibleLogRows(viewer);
        }
    }
    pane.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => selectView(button.dataset.view)));

    async function showRaw() {
        selectView('raw');
        const path = document.getElementById('log-path-input').value.trim();
        if (!logLoaded) {
            if (!hosts.raw.firstElementChild) {
                const viewer = openKeyLogModal('', toggle, 'preview', {id: 'workspace-raw-viewer', host: hosts.raw});
                viewer.querySelector('.key-log-content').textContent = 'No log loaded.';
            }
            return;
        }
        if (rawPath === path && hosts.raw.firstElementChild?.__source) return;
        rawPath = path;
        await openTatPreviewFromSidebar(toggle, {id: 'workspace-raw-viewer', host: hosts.raw});
    }

    function divider(label, leftName, rightName) {
        const element = document.createElement('div');
        element.className = 'workspace-divider';
        element.hidden = true;
        element.tabIndex = 0;
        element.setAttribute('role', 'separator');
        element.setAttribute('aria-orientation', 'vertical');
        element.setAttribute('aria-label', label);
        function measure() {
            return {
                sidebar: layout.querySelector('.sidebar').getBoundingClientRect().width,
                viewer: pane.getBoundingClientRect().width,
                chat: chat.getBoundingClientRect().width,
            };
        }
        function resize(delta, start = measure()) {
            const widths = {...start};
            const minimum = {sidebar: 200, viewer: 260, chat: 320};
            const change = Math.max(minimum[leftName] - widths[leftName], Math.min(delta, widths[rightName] - minimum[rightName]));
            widths[leftName] += change;
            widths[rightName] -= change;
            const collapsed = document.body.classList.contains('sidebar-collapsed');
            Object.entries(widths).forEach(([name, width]) => {
                if (!(collapsed && name === 'sidebar')) layout.style.setProperty(`--workspace-${name}`, `${width}fr`);
            });
        }
        element.addEventListener('pointerdown', event => {
            element.setPointerCapture(event.pointerId);
            layout.classList.add('workspace-resizing');
            const start = measure();
            const originX = event.clientX;
            let frame = 0;
            let latestX = originX;
            element.onpointermove = move => {
                latestX = move.clientX;
                if (!frame) frame = requestAnimationFrame(() => { frame = 0; resize(latestX - originX, start); });
            };
            element.onpointerup = element.onpointercancel = () => {
                element.onpointermove = null;
                layout.classList.remove('workspace-resizing');
            };
            event.preventDefault();
        });
        element.addEventListener('keydown', event => {
            if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
            resize(event.key === 'ArrowLeft' ? -20 : 20);
            event.preventDefault();
        });
        return element;
    }
    const leftDivider = divider('Resize sidebar and log viewer', 'sidebar', 'viewer');
    const rightDivider = divider('Resize log viewer and chatbot', 'viewer', 'chat');
    layout.insertBefore(leftDivider, chat);
    layout.insertBefore(pane, chat);
    layout.insertBefore(rightDivider, chat);
    toggle.addEventListener('click', () => {
        active = !active;
        layout.classList.toggle('advanced-workspace', active);
        pane.hidden = leftDivider.hidden = rightDivider.hidden = !active;
        toggle.setAttribute('aria-pressed', String(active));
        toggle.textContent = active ? 'Standard mode' : 'Advanced mode';
        if (active) {
            if (currentView === 'raw') showRaw();
            else selectView(currentView);
        }
    });
    window.ChatbotWorkspace = {
        get active() { return active; },
        showRaw,
        showEvidence(raw, trigger, status) {
            const viewer = openKeyLogModal(raw, trigger, status, {id: 'workspace-evidence-viewer', host: hosts.evidence});
            selectView('evidence');
            return viewer;
        },
    };
    document.addEventListener('chatbot:context-changed', () => {
        rawPath = '';
        for (const host of Object.values(hosts)) if (host.firstElementChild) cleanupKeyLogModal(host.firstElementChild);
        if (active) showRaw();
    });
})();