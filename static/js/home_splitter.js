(() => {
    'use strict';

    const workspace = document.querySelector('.home-workspace');
    const splitter = document.getElementById('home-splitter');
    if (!workspace || !splitter) return;

    const minLeft = 320;
    const minRight = 360;
    const splitterWidth = 12;
    const gap = 12;
    let ratio = 0.5;
    let dragging = false;

    try {
        const savedRatio = Number(localStorage.getItem('wireless-ce-home-split'));
        if (Number.isFinite(savedRatio) && savedRatio > 0) ratio = savedRatio;
    } catch (error) {
        // Keep the default split if browser storage is unavailable.
    }

    function availableWidth() {
        return workspace.clientWidth - splitterWidth - gap * 2;
    }

    function ratioLimits(width) {
        return {
            min: Math.max(0.36, minLeft / width),
            max: Math.min(0.64, 1 - minRight / width),
        };
    }

    function applyRatio() {
        if (window.innerWidth <= 880) {
            workspace.style.removeProperty('grid-template-columns');
            return;
        }
        const width = availableWidth();
        if (width <= minLeft + minRight) return;
        const limits = ratioLimits(width);
        ratio = Math.min(limits.max, Math.max(limits.min, ratio));
        const leftWidth = Math.round(width * ratio);
        workspace.style.gridTemplateColumns = `${leftWidth}px ${splitterWidth}px ${width - leftWidth}px`;
        splitter.setAttribute('aria-valuemin', String(Math.round(limits.min * 100)));
        splitter.setAttribute('aria-valuemax', String(Math.round(limits.max * 100)));
        splitter.setAttribute('aria-valuenow', String(Math.round(ratio * 100)));
        splitter.setAttribute('aria-valuetext', `Review queue ${Math.round(ratio * 100)}%, Quick Analysis ${Math.round((1 - ratio) * 100)}%`);
    }

    function saveRatio() {
        try {
            localStorage.setItem('wireless-ce-home-split', String(ratio));
        } catch (error) {
            // Current-session resizing remains available without storage.
        }
    }

    function stopDragging() {
        if (!dragging) return;
        dragging = false;
        document.body.classList.remove('home-split-dragging');
        saveRatio();
    }

    splitter.addEventListener('pointerdown', event => {
        if (window.innerWidth <= 880 || splitter.getAttribute('aria-disabled') === 'true') return;
        event.preventDefault();
        dragging = true;
        document.body.classList.add('home-split-dragging');
        splitter.setPointerCapture?.(event.pointerId);
    });

    splitter.addEventListener('pointermove', event => {
        if (!dragging || splitter.getAttribute('aria-disabled') === 'true') return;
        const width = availableWidth();
        if (width <= minLeft + minRight) return;
        const limits = ratioLimits(width);
        const workspaceLeft = workspace.getBoundingClientRect().left;
        const nextRatio = (event.clientX - workspaceLeft - gap - splitterWidth / 2) / width;
        ratio = Math.min(limits.max, Math.max(limits.min, nextRatio));
        applyRatio();
    });

    splitter.addEventListener('pointerup', stopDragging);
    splitter.addEventListener('pointercancel', stopDragging);
    splitter.addEventListener('lostpointercapture', stopDragging);
    document.addEventListener('pointerup', stopDragging);

    splitter.addEventListener('keydown', event => {
        if (window.innerWidth <= 880 || splitter.getAttribute('aria-disabled') === 'true') return;
        const step = event.shiftKey ? 0.05 : 0.02;
        if (event.key === 'ArrowLeft') ratio -= step;
        else if (event.key === 'ArrowRight') ratio += step;
        else if (event.key === 'Home') ratio = 0.36;
        else if (event.key === 'End') ratio = 0.64;
        else return;
        event.preventDefault();
        applyRatio();
        saveRatio();
    });

    window.addEventListener('resize', applyRatio);
    applyRatio();
})();