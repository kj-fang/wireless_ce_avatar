const DEFAULT_CHUNK_BYTES = 1024 * 1024;
let activeRunId = 0;
let matchedRows = [];

self.onmessage = event => {
    const message = event.data || {};
    if (message.type === 'start') {
        activeRunId = message.runId;
        matchedRows = [];
        scanSource(message, message.runId).catch(error => {
            if (message.runId !== activeRunId) return;
            self.postMessage({type: 'error', runId: message.runId,
                message: error && error.message ? error.message : String(error)});
        });
    } else if (message.type === 'range' && message.runId === activeRunId) {
        const start = Math.max(0, Math.min(matchedRows.length, message.start | 0));
        const end = Math.max(start, Math.min(matchedRows.length, start + (message.count | 0)));
        self.postMessage({type: 'range', runId: activeRunId, requestId: message.requestId,
            start, rows: matchedRows.slice(start, end)});
    }
};

function compileRules(sourceRules) {
    return (sourceRules || []).map(rule => {
        const text = String(rule.text || '');
        const regex = !!rule.regex;
        return {
            text,
            searchText: text.toLowerCase(),
            enabled: !!rule.enabled,
            excluding: !!rule.excluding,
            regex,
            foreground: rule.foreground || '',
            background: rule.background || '',
            matcher: regex ? new RegExp(text, 'i') : null,
        };
    }).filter(rule => rule.text);
}

async function scanSource(message, runId) {
    const rules = compileRules(message.rules);
    const includeRules = rules.filter(rule => rule.enabled && !rule.excluding);
    const excludeRules = rules.filter(rule => rule.enabled && rule.excluding);
    const decoder = new TextDecoder('utf-8');
    const chunkSize = Math.max(32 * 1024, Math.min(message.chunkSize || DEFAULT_CHUNK_BYTES, 1024 * 1024));
    let pending = '';
    let physicalLine = 0;
    let candidateCount = 0;
    let lastProgressAt = 0;
    let bytesRead = 0;
    const lineBreakPattern = /[\r\n]/g;

    const matches = (line, lowerLine, rule) => {
        if (rule.matcher) {
            rule.matcher.lastIndex = 0;
            return rule.matcher.test(line);
        }
        return lowerLine.includes(rule.searchText);
    };

    function processLine(originalLine) {
        physicalLine++;
        if (physicalLine === 1 && originalLine.startsWith('# WCE_LOG_TZ_SHIFTED=')) return;
        const line = originalLine.replace(/[\r\n]+$/, '');
        if (!line.trim()) return;
        candidateCount++;
        if (!rules.length) {
            matchedRows.push({text: originalLine, foreground: '', background: ''});
            return;
        }
        if (!includeRules.length) return;
        const lowerLine = line.toLowerCase();
        const include = includeRules.find(rule => matches(line, lowerLine, rule));
        if (!include || excludeRules.some(rule => matches(line, lowerLine, rule))) return;
        matchedRows.push({text: originalLine, foreground: include.foreground, background: include.background});
    }

    function consumeDecoded(text, finalChunk) {
        pending += text;
        let start = 0;
        lineBreakPattern.lastIndex = 0;
        let match;
        while ((match = lineBreakPattern.exec(pending))) {
            const index = match.index;
            const code = pending.charCodeAt(index);
            if (code === 13 && index + 1 >= pending.length && !finalChunk) break;
            const end = code === 13 && pending.charCodeAt(index + 1) === 10 ? index + 2 : index + 1;
            processLine(pending.slice(start, end));
            start = end;
            lineBreakPattern.lastIndex = end;
        }
        pending = pending.slice(start);
        if (finalChunk && pending) {
            processLine(pending);
            pending = '';
        }
    }

    const source = message.source || {};
    if (source.kind === 'file' && source.file) {
        const file = source.file;
        for (let offset = 0; offset < file.size; offset += chunkSize) {
            if (runId !== activeRunId) return;
            const buffer = await file.slice(offset, Math.min(file.size, offset + chunkSize)).arrayBuffer();
            bytesRead += buffer.byteLength;
            const finalChunk = offset + buffer.byteLength >= file.size;
            consumeDecoded(decoder.decode(buffer, {stream: !finalChunk}), finalChunk);
            emitProgress();
        }
        if (file.size === 0) consumeDecoded(decoder.decode(), true);
    } else if (source.kind === 'server' && source.url) {
        let offset = 0;
        let eof = false;
        while (!eof) {
            if (runId !== activeRunId) return;
            const url = new URL(source.url, self.location.href);
            url.searchParams.set('offset', String(offset));
            url.searchParams.set('limit', String(chunkSize));
            const response = await fetch(url.toString(), {cache: 'no-store', credentials: 'same-origin'});
            if (!response.ok) throw new Error(await response.text() || `Log read failed (${response.status}).`);
            const buffer = await response.arrayBuffer();
            const nextOffset = Number(response.headers.get('X-Next-Offset'));
            eof = response.headers.get('X-Log-Eof') === 'true';
            if (!buffer.byteLength && !eof) throw new Error('Log preview returned an empty chunk before EOF.');
            bytesRead += buffer.byteLength;
            consumeDecoded(decoder.decode(buffer, {stream: !eof}), eof);
            offset = Number.isFinite(nextOffset) && nextOffset > offset ? nextOffset : offset + buffer.byteLength;
            emitProgress();
        }
        if (!bytesRead) consumeDecoded(decoder.decode(), true);
    } else {
        throw new Error('No log file was selected for preview.');
    }

    if (runId !== activeRunId) return;
    self.postMessage({type: 'ready', runId, rowCount: matchedRows.length,
        candidateCount, bytesRead, sourceName: source.name || ''});

    function emitProgress() {
        const now = Date.now();
        if (now - lastProgressAt < 200) return;
        lastProgressAt = now;
        self.postMessage({type: 'progress', runId, candidateCount, matchedCount: matchedRows.length, bytesRead});
    }
}
