    class BtIssueTimeStrategy extends IssueTimeStrategyBase {}

    // The AI-suggest flow can hand back the nearest system Error/Critical it
    // used, which drives the refine picker without a second event-log fetch.
    BtIssueTimeStrategy.prototype._beforeOpenCaptureModal = function () {
        // Clear any AI refine linkage by default; the AI-suggest flow re-sets it
        // AFTER this call. This keeps typed-token captures (which also open this
        // modal) from inheriting a stale nearest-error and showing the picker.
        window.__aiNearestError = null;
        window.__aiBestTimeStr = '';
    };

    BtIssueTimeStrategy.prototype._afterApplyTimeCapture = function (times, send) {
        if (window.__aiNearestError) {
            const origTime = getIssueTimeString() || window.__aiBestTimeStr || '';
            if (origTime) showRefinePickerFromData(origTime, window.__aiNearestError);
        }
        window.__aiNearestError = null;
        window.__aiBestTimeStr = '';
    };

    BtIssueTimeStrategy.prototype._suggestRequestBody = function (text) {
        // Forward the System Event Log dropdowns so the AI weighs the
        // SAME Warn+Err selection the user sees on this page as a
        // high-priority anchor for the issue time.
        const srcSel = document.getElementById('evtPopupSourceFilter');
        const lvlSel = document.getElementById('evtPopupLevelFilter');
        return {
            text,
            source_filter: srcSel ? srcSel.value : 'all',
            level_filter: lvlSel ? lvlSel.value : 'warning_error',
        };
    };

    BtIssueTimeStrategy.prototype._selectSuggestions = function (data, sugg) {
        // For AI suggestions, pre-select ONLY the single best candidate so
        // the user isn't forced to prune a long auto-filled list. The user's
        // own explicit times (user_explicit) are NOT narrowed — all of them
        // are intentional, so we keep the original behaviour there.
        const bestIdx = _pickBestSuggestionIndex(sugg);
        const chosen = (data.user_explicit || sugg.length <= 1)
            ? sugg
            : (bestIdx >= 0 ? [sugg[bestIdx]] : []);
        return { chosen, bestIdx };
    };

    BtIssueTimeStrategy.prototype._afterSuggestionsOpened = function (data, sugg, bestIdx) {
        // Stash the best suggestion's nearest system Error/Critical (computed
        // server-side) so applyTimeCapture() can show "Found a nearby system
        // error" WITHOUT a second /parse_event_log fetch.
        const best = (!data.user_explicit && bestIdx >= 0) ? sugg[bestIdx]
            : (sugg.length === 1 ? sugg[0] : null);
        if (best && best.nearest_error) {
            window.__aiNearestError = best.nearest_error;
            window.__aiBestTimeStr = best.issue_time || '';
        }
    };

    BtIssueTimeStrategy.prototype._aiSuggestionTag = function (idx, bestIdx, sugg, data) {
        const onlyBest = !data.user_explicit && sugg.length > 1;
        if (!onlyBest) return '';
        // Mark the auto-selected best candidate so the user can see
        // which one was pre-filled below (the rest are alternatives).
        return idx === bestIdx
            ? ' <strong style="color:#15803d;">✓ selected</strong>'
            : ' <span style="color:#94a3b8;">(alternative)</span>';
    };

    BtIssueTimeStrategy.prototype._aiSummaryFooter = function (data, sugg) {
        const onlyBest = !data.user_explicit && sugg.length > 1;
        if (onlyBest) {
            return ['<div style="margin-top:6px;">Only the <strong>best</strong> time is pre-filled below. '
                + 'Add an alternative with “＋ Add another time” if needed, then choose '
                + '<strong>Replace</strong> or <strong>Append</strong>.</div>'];
        }
        return ['<div style="margin-top:6px;">Edit if needed, then choose <strong>Replace</strong> or <strong>Append</strong>.</div>'];
    };

    BtIssueTimeStrategy.prototype._applyDateFieldState = function (noDate) {
        ['it-month', 'it-day', 'it-year'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.style.opacity = noDate ? '0.55' : '';
        });
    };

    window.createIssueTimeStrategy = (profile) => new BtIssueTimeStrategy(profile);
