const DEFAULT_CHUNK_BYTES = 1024 * 1024;
const ADD_TO_QUESTION_MAX_LINES = 200;
const ADD_TO_QUESTION_MAX_CHARS = 8000;
const COPY_MAX_LINES = 50000;
const MAX_SOURCE_CACHE_BYTES = 32 * 1024 * 1024;
const MAX_RULE_INDEX_BYTES = 16 * 1024 * 1024;
let activeRunId = 0;
let displayedRunId = 0;
let matchedRows = [];
let ruleHitCounts = [];
let cachedChunks = null;
let cachedIndexes = new Map();

self.onmessage = event => {
    const message = event.data || {};
    if (message.type === 'start') {
        activeRunId = message.runId;
        if (!message.reuseCache) {
            cachedChunks = null;
            cachedIndexes = new Map();
        }
        scanSource(message, message.runId).catch(error => {
            if (message.runId !== activeRunId) return;
            self.postMessage({type: 'error', runId: message.runId,
                message: error && error.message ? error.message : String(error)});
        });
    } else if (message.type === 'range' && message.runId === displayedRunId) {
        const start = Math.max(0, Math.min(matchedRows.length, message.start | 0));
        const end = Math.max(start, Math.min(matchedRows.length, start + (message.count | 0)));
        self.postMessage({type: 'range', runId: displayedRunId, requestId: message.requestId,
            start, rows: matchedRows.slice(start, end)});
    } else if (message.type === 'selection' && message.runId === displayedRunId) {
        const start = Math.max(0, Math.min(message.start, message.end));
        const end = Math.min(matchedRows.length - 1, Math.max(message.start, message.end));
        const purpose = message.purpose === 'copy' ? 'copy' : 'add';
        const maxLines = purpose === 'copy' ? COPY_MAX_LINES : ADD_TO_QUESTION_MAX_LINES;
        const maxChars = purpose === 'copy' ? Infinity : ADD_TO_QUESTION_MAX_CHARS;
        const rows = [];
        let length = 0;
        for (let index = start; index <= end; index++) {
            length += matchedRows[index].text.length;
            if (rows.length >= maxLines || length > maxChars) {
                const error = purpose === 'copy'
                    ? `Select at most ${maxLines.toLocaleString()} lines to copy. Nothing was copied.`
                    : 'Select at most 200 lines and 8,000 characters. Nothing was added.';
                self.postMessage({type: 'selection', runId: displayedRunId, requestId: message.requestId, purpose, error});
                return;
            }
            rows.push(matchedRows[index]);
        }
        self.postMessage({type: 'selection', runId: displayedRunId, requestId: message.requestId, purpose, rows});
    } else if (message.type === 'locate' && message.runId === displayedRunId) {
        let low = 0;
        let high = matchedRows.length - 1;
        let index = matchedRows.length - 1;
        while (low <= high) {
            const mid = (low + high) >> 1;
            if (matchedRows[mid].lineNumber >= message.lineNumber) {
                index = mid;
                high = mid - 1;
            } else {
                low = mid + 1;
            }
        }
        index = Math.max(0, Math.min(matchedRows.length - 1, index));
        self.postMessage({type: 'locate', runId: displayedRunId, requestId: message.requestId, index});
    }
};

function compileRules(sourceRules) {
    return (sourceRules || []).map(rule => {
        const text = String(rule.text || '');
        const regex = !!rule.regex;
        const caseSensitive = !!rule.caseSensitive;
        return {
            text,
            searchText: caseSensitive ? text : text.toLowerCase(),
            caseSensitive,
            enabled: !!rule.enabled,
            excluding: !!rule.excluding,
            regex,
            foreground: rule.foreground || '',
            background: rule.background || '',
            matcher: regex ? new RegExp(text, caseSensitive ? '' : 'i') : null,
        };
    }).filter(rule => rule.text);
}

async function scanSource(message, runId) {
    const rules = compileRules(message.rules);
    const source = message.source || {};
    const sourceSize = source.kind === 'file' ? source.file?.size : source.size;
    const canIndex = Number.isSafeInteger(sourceSize) && sourceSize >= 0 && sourceSize <= MAX_SOURCE_CACHE_BYTES;
    const nextIndexes = new Map();
    const indexWords = canIndex ? Math.ceil(sourceSize / 32) : 0;
    let indexBytes = 0;
    const ruleIndexes = rules.map(rule => {
        if (!canIndex) return null;
        const key = JSON.stringify([rule.text, rule.regex, rule.caseSensitive]);
        if (nextIndexes.has(key)) return nextIndexes.get(key);
        const existing = message.reuseCache && cachedChunks ? cachedIndexes.get(key) : null;
        const bits = existing || (indexBytes + indexWords * 4 <= MAX_RULE_INDEX_BYTES ? new Uint32Array(indexWords) : null);
        if (!bits || indexBytes + bits.byteLength > MAX_RULE_INDEX_BYTES) return null;
        const entry = {bits, ready: !!existing};
        nextIndexes.set(key, entry);
        indexBytes += bits.byteLength;
        return entry;
    });
    const showOnlyFilteredLines = message.showOnlyFilteredLines !== false;
    const nextRows = [];
    const nextHitCounts = new Array(rules.length).fill(0);
    const sourceLineNumbers = Array.isArray(message.sourceLineNumbers) ? message.sourceLineNumbers : null;
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
        return rule.caseSensitive ? line.includes(rule.searchText) : lowerLine.includes(rule.searchText);
    };

    function processLine(originalLine) {
        physicalLine++;
        if (physicalLine === 1 && originalLine.startsWith('# WCE_LOG_TZ_SHIFTED=')) return;
        const lineNumber = sourceLineNumbers ? sourceLineNumbers[physicalLine - 1] : physicalLine;
        if (sourceLineNumbers && (!Number.isSafeInteger(lineNumber) || lineNumber <= 0)) return;
        const line = originalLine.replace(/[\r\n]+$/, '');
        if (!line.trim()) return;
        candidateCount++;
        const lowerLine = line.toLowerCase();
        if (!rules.length) {
            nextRows.push({text: originalLine, lineNumber, foreground: '', background: ''});
            return;
        }
        const wordIndex = (candidateCount - 1) >>> 5;
        const mask = 1 << ((candidateCount - 1) & 31);
        let include = null;
        let excluded = false;
        rules.forEach((rule, index) => {
            const entry = ruleIndexes[index];
            const matched = entry?.ready ? !!(entry.bits[wordIndex] & mask) : matches(line, lowerLine, rule);
            if (!matched) return;
            if (entry && !entry.ready) entry.bits[wordIndex] |= mask;
            nextHitCounts[index]++;
            if (rule.enabled && rule.excluding) excluded = true;
            else if (rule.enabled && !include) include = rule;
        });
        if (excluded) return;
        if (include) {
            nextRows.push({text: originalLine, lineNumber, foreground: include.foreground, background: include.background});
        } else if (!showOnlyFilteredLines) {
            nextRows.push({text: originalLine, lineNumber, foreground: '', background: '', dimmed: true});
        }
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

    let nextCache = Number.isSafeInteger(sourceSize) && sourceSize > MAX_SOURCE_CACHE_BYTES ? null : [];
    function rememberChunk(buffer) {
        if (!nextCache) return;
        if (bytesRead > MAX_SOURCE_CACHE_BYTES) nextCache = null;
        else nextCache.push(buffer);
    }
    if (cachedChunks && message.reuseCache) {
        nextCache = cachedChunks;
        for (let index = 0; index < cachedChunks.length; index++) {
            if (runId !== activeRunId) return;
            const buffer = cachedChunks[index];
            bytesRead += buffer.byteLength;
            consumeDecoded(decoder.decode(buffer, {stream: index < cachedChunks.length - 1}), index === cachedChunks.length - 1);
            emitProgress();
            await new Promise(resolve => setTimeout(resolve, 0));
        }
        if (!cachedChunks.length) consumeDecoded(decoder.decode(), true);
    } else if (source.kind === 'file' && source.file) {
        const file = source.file;
        for (let offset = 0; offset < file.size; offset += chunkSize) {
            if (runId !== activeRunId) return;
            const buffer = await file.slice(offset, Math.min(file.size, offset + chunkSize)).arrayBuffer();
            bytesRead += buffer.byteLength;
            rememberChunk(buffer);
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
            rememberChunk(buffer);
            consumeDecoded(decoder.decode(buffer, {stream: !eof}), eof);
            offset = Number.isFinite(nextOffset) && nextOffset > offset ? nextOffset : offset + buffer.byteLength;
            emitProgress();
        }
        if (!bytesRead) consumeDecoded(decoder.decode(), true);
    } else {
        throw new Error('No log file was selected for preview.');
    }

    if (runId !== activeRunId) return;
    if (canIndex && bytesRead > sourceSize) nextCache = null;
    cachedChunks = nextCache;
    cachedIndexes = nextCache ? new Map(Array.from(nextIndexes, ([key, entry]) => [key, entry.bits])) : new Map();
    matchedRows = nextRows;
    ruleHitCounts = nextHitCounts;
    displayedRunId = runId;
    self.postMessage({type: 'ready', runId, rowCount: matchedRows.length,
        candidateCount, bytesRead, sourceName: source.name || '', ruleHitCounts});

    function emitProgress() {
        const now = Date.now();
        if (now - lastProgressAt < 200) return;
        lastProgressAt = now;
        self.postMessage({type: 'progress', runId, candidateCount, matchedCount: nextRows.length, bytesRead});
    }
}
