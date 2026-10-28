(function () {
    let _evtPath = '';
    let _evtData = [];
    let _evtTotal = 0;
    let _evtOffset = 0;
    const _EVT_PAGE = 200;
    let _evtLoading = false;
    let _evtHasMore = false;
    let _evtTimeHeader = 'Time';
    let _evtPanelOpen = false;
    let _evtLoaded = false;

    window.updateEvtButton = function (path) {
        _evtPath = path || '';
        const btn = document.getElementById('btn-evt-log');
        if (_evtPath) {
            btn.disabled = false;
            btn.title = 'Toggle System Event Log';
        } else {
            btn.disabled = true;
            btn.title = 'No event log available';
        }
        _evtData = []; _evtOffset = 0; _evtHasMore = false; _evtLoaded = false;
        const panel = document.getElementById('evt-inline-panel');
        if (panel) panel.style.display = 'none';
        const arrow = document.getElementById('evt-toggle-arrow');
        if (arrow) arrow.classList.remove('open');
        btn.setAttribute('aria-expanded', 'false');
        _evtPanelOpen = false;

        // BT captures are named after the transport they came from, so the
        // matching source group can be pre-selected when the page offers it.
        const logPath = (document.getElementById('log-path-input') || {}).value || '';
        const logName = logPath.split(/[\\\/]/).pop().toLowerCase();
        const srcSel = document.getElementById('evtPopupSourceFilter');
        const hasOption = (v) => !!srcSel && Array.from(srcSel.options).some(o => o.value === v);
        if (srcSel && logName) {
            if (logName.startsWith('ibtusb') && hasOption('usb_bt')) srcSel.value = 'usb_bt';
            else if (logName.startsWith('ibtpci') && hasOption('pci_bt')) srcSel.value = 'pci_bt';
        }
    };

    window.toggleEvtPanel = function () {
        if (!_evtPath) return;
        const panel = document.getElementById('evt-inline-panel');
        const arrow = document.getElementById('evt-toggle-arrow');
        _evtPanelOpen = !_evtPanelOpen;
        panel.style.display = _evtPanelOpen ? 'block' : 'none';
        arrow.classList.toggle('open', _evtPanelOpen);
        document.getElementById('btn-evt-log').setAttribute('aria-expanded', String(_evtPanelOpen));
        if (_evtPanelOpen && !_evtLoaded) {
            _evtLoaded = true;
            _evtResetAndFetch();
        }
    };

    window.evtFilterChanged = function () { _evtResetAndFetch(); };

    function _evtResetAndFetch() {
        _evtData = []; _evtOffset = 0; _evtHasMore = false;
        const body = document.getElementById('evtPopupBody');
        body.innerHTML = '<p style="padding:12px;color:#999;text-align:center;font-size:0.7rem;">Loading…</p>';
        _evtFetchPage();
    }

    async function _evtFetchPage() {
        if (_evtLoading || !_evtPath) return;
        _evtLoading = true;
        try {
            const res = await fetch('/parse_event_log', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    path: _evtPath, offset: _evtOffset, limit: _EVT_PAGE,
                    source_filter: document.getElementById('evtPopupSourceFilter').value,
                    level_filter: document.getElementById('evtPopupLevelFilter').value
                })
            });
            const json = await res.json();
            if (!json.events) { _evtRenderError(json.error || 'No data'); return; }
            _evtTotal = json.total || 0;
            _evtHasMore = json.has_more || false;
            _evtTimeHeader = json.time_header || 'Time';
            _evtOffset += json.events.length;
            _evtData = _evtData.concat(json.events);
            _evtRenderTable();
        } catch (e) { _evtRenderError('Network error: ' + e.message); }
        finally { _evtLoading = false; }
    }

    function _evtRenderError(msg) {
        const body = document.getElementById('evtPopupBody');
        body.replaceChildren();
        const p = document.createElement('p');
        p.style.cssText = 'padding:12px;color:#dc2626;text-align:center;font-size:0.7rem;';
        p.textContent = msg;
        body.appendChild(p);
        document.getElementById('evtPopupFooter').textContent = '';
        document.getElementById('evtPopupCount').textContent = '';
    }

    function _evtRenderTable() {
        const body = document.getElementById('evtPopupBody');
        let html = `<table><colgroup>
            <col style="width:120px;"><col style="width:18px;">
            <col style="width:80px;"><col style="width:40px;">
        </colgroup><thead><tr>
            <th>${_esc(_evtTimeHeader)}</th><th>Lv</th><th>Source</th><th>ID</th>
        </tr></thead><tbody>`;
        for (let i = 0; i < _evtData.length; i++) {
            const ev = _evtData[i];
            const dot = (ev.level === 'Error' || ev.level === 'Critical') ? '🔴'
                : ev.level === 'Warning' ? '🟡' : '🟢';
            html += `<tr data-evt-idx="${i}">
                <td>${_esc(ev.time)}</td>
                <td style="text-align:center;">${dot}</td>
                <td>${_esc(ev.source)}</td>
                <td>${_esc(ev.event_id)}</td>
            </tr>`;
        }
        html += '</tbody></table>';
        body.innerHTML = html;
        document.getElementById('evtPopupCount').textContent = `${_evtData.length}/${_evtTotal}`;
        document.getElementById('evtPopupFooter').textContent = _evtHasMore ? 'Scroll for more…' : '';

        body.onscroll = function () {
            if (!_evtHasMore || _evtLoading) return;
            if (body.scrollTop + body.clientHeight >= body.scrollHeight * 0.7) _evtFetchPage();
        };
        body.querySelectorAll('tr[data-evt-idx]').forEach(function (tr) {
            tr.addEventListener('mouseenter', _onRowEnter);
            tr.addEventListener('mouseleave', _onRowLeave);
        });
    }

    function _onRowEnter(e) {
        const idx = parseInt(e.currentTarget.dataset.evtIdx, 10);
        const ev = _evtData[idx];
        if (!ev) return;
        const card = document.getElementById('evt-hover-card');
        let h = '<table>';
        h += '<tr><th>Time</th><td>' + _esc(ev.time) + '</td></tr>';
        const lvColor = (ev.level === 'Error' || ev.level === 'Critical') ? '#dc2626'
            : ev.level === 'Warning' ? '#ca8a04' : '#16a34a';
        h += '<tr><th>Level</th><td style="color:' + lvColor + ';font-weight:600;">' + _esc(ev.level) + '</td></tr>';
        h += '<tr><th>Source</th><td>' + _esc(ev.source) + '</td></tr>';
        h += '<tr><th>ID</th><td>' + _esc(ev.event_id) + '</td></tr>';
        if (ev.details) h += '<tr><th>Details</th><td>' + _esc(ev.details) + '</td></tr>';
        h += '<tr><th>Message</th><td>' + _esc(ev.message) + '</td></tr>';
        h += '</table>';
        card.innerHTML = h;
        const rect = e.currentTarget.getBoundingClientRect();
        const sidebar = document.querySelector('.sidebar');
        const sRight = sidebar ? sidebar.getBoundingClientRect().right : 340;
        card.style.left = (sRight + 8) + 'px';
        let top = rect.top;
        if (top + 200 > window.innerHeight - 20) top = window.innerHeight - 220;
        if (top < 60) top = 60;
        card.style.top = top + 'px';
        card.style.display = 'block';
    }

    function _onRowLeave() {
        document.getElementById('evt-hover-card').style.display = 'none';
    }

    function _esc(s) {
        if (!s) return '';
        return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    // ── Expose: find the closest Error/Critical event to a given time ──
    // Used by the BT auto-refine logic to snap the issue time to the nearest
    // system event error without a separate backend round-trip.
    const _SOURCE_GROUPS = {
        'pci_bt':      ['ibtpci', 'bthmini'],
        'usb_bt':      ['ibtusb', 'bthusb'],
        'wifi':        ['netwaw', 'netwtw'],
        'pci_bt_wifi': ['ibtpci', 'bthmini', 'netwaw', 'netwtw'],
        'usb_bt_wifi': ['ibtusb', 'bthusb', 'netwaw', 'netwtw'],
    };

    window.findClosestEventError = async function (issueTimeStr, sourceFilter) {
        if (!_evtPath || !issueTimeStr) return null;

        const issueDt = _parseEvtTimeStr(issueTimeStr);
        if (!issueDt) return null;

        let errorEvents = [];
        if (_evtData && _evtData.length > 0) {
            errorEvents = _evtData.filter(ev => ev.level === 'Error' || ev.level === 'Critical');
        }
        if (errorEvents.length === 0) {
            try {
                const res = await fetch('/parse_event_log', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        path: _evtPath, offset: 0, limit: 0,
                        source_filter: 'all',
                        level_filter: 'warning_error'
                    })
                });
                const json = await res.json();
                if (json.events) {
                    errorEvents = json.events.filter(ev => ev.level === 'Error' || ev.level === 'Critical');
                }
            } catch (e) {
                console.warn('[findClosestEventError] fetch failed:', e);
                return null;
            }
        }

        if (sourceFilter && sourceFilter !== 'all') {
            const keywords = _SOURCE_GROUPS[sourceFilter] || [sourceFilter];
            errorEvents = errorEvents.filter(ev => {
                const src = (ev.source || '').toLowerCase();
                return keywords.some(kw => src.includes(kw));
            });
        }

        if (errorEvents.length === 0) return null;

        let best = null;
        let bestDiff = Infinity;
        for (const ev of errorEvents) {
            const evDt = _parseEvtTimeStr(ev.time);
            if (!evDt) continue;
            const diff = Math.abs(evDt.getTime() - issueDt.getTime()) / 1000;
            if (diff < bestDiff) {
                bestDiff = diff;
                best = ev;
            }
        }
        if (!best) return null;

        const evDt = _parseEvtTimeStr(best.time);
        let formattedTime = best.time;
        if (evDt) {
            const mm = String(evDt.getMonth() + 1).padStart(2, '0');
            const dd = String(evDt.getDate()).padStart(2, '0');
            const yyyy = evDt.getFullYear();
            const hh = String(evDt.getHours()).padStart(2, '0');
            const mi = String(evDt.getMinutes()).padStart(2, '0');
            const ss = String(evDt.getSeconds()).padStart(2, '0');
            formattedTime = `${mm}/${dd}/${yyyy}-${hh}:${mi}:${ss}`;
        }

        return {
            formatted_time: formattedTime,
            diff_seconds: bestDiff,
            source: best.source || '',
            event_id: best.event_id || '',
            level: best.level || '',
            message: best.message || '',
        };
    };

    /**
     * Parse various time string formats into a Date object.
     * Supports: "YYYY-MM-DD HH:MM:SS", "MM/DD/YYYY-HH:MM:SS(.mmm)",
     *           "MM/DD/YYYY HH:MM:SS"
     */
    function _parseEvtTimeStr(s) {
        if (!s) return null;
        let m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})/);
        if (m) return new Date(+m[1], +m[2]-1, +m[3], +m[4], +m[5], +m[6]);
        m = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})[\s-](\d{1,2}):(\d{2}):(\d{2})/);
        if (m) return new Date(+m[3], +m[1]-1, +m[2], +m[4], +m[5], +m[6]);
        return null;
    }
})();
