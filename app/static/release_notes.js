(function attachReleaseNotes(global, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (global) global.SolverrReleaseNotes = api;
})(typeof globalThis === 'undefined' ? this : globalThis, function releaseNotesModule() {
    const DISMISSAL_KEY = 'solverr.dismissed-release-revision';

    function browserStorage() {
        try { return window.localStorage; } catch { return undefined; }
    }

    function parseInlineMarkdown(text) {
        const result = [];
        const token = /\[([^\]]+)\]\(([^)]+)\)|\*\*([^*]+)\*\*|`([^`]+)`/g;
        let start = 0;
        let match;
        while ((match = token.exec(text))) {
            if (match.index > start) result.push({ type: 'text', text: text.slice(start, match.index) });
            if (match[1] !== undefined) {
                let url;
                try { url = new URL(match[2]); } catch { url = null; }
                if (url && (url.protocol === 'https:' || url.protocol === 'http:')) {
                    result.push({ type: 'link', text: match[1], url: url.href });
                } else {
                    result.push({ type: 'text', text: match[0] });
                }
            } else if (match[3] !== undefined) result.push({ type: 'strong', text: match[3] });
            else result.push({ type: 'code', text: match[4] });
            start = token.lastIndex;
        }
        if (start < text.length) result.push({ type: 'text', text: text.slice(start) });
        return result;
    }

    function parseMarkdown(markdown) {
        const blocks = [];
        let paragraph = [];
        let list = [];
        const flushParagraph = () => {
            if (paragraph.length) blocks.push({ type: 'paragraph', text: paragraph.join(' ') });
            paragraph = [];
        };
        const flushList = () => {
            if (list.length) blocks.push({ type: 'list', items: list });
            list = [];
        };
        for (const rawLine of String(markdown || '').split(/\r?\n/)) {
            const line = rawLine.trim();
            const heading = /^(#{1,3})\s+(.+)$/.exec(line);
            const item = /^[-*]\s+(.+)$/.exec(line);
            if (!line) { flushParagraph(); flushList(); }
            else if (heading) { flushParagraph(); flushList(); blocks.push({ type: 'heading', level: heading[1].length, text: heading[2] }); }
            else if (item) { flushParagraph(); list.push(item[1]); }
            else { flushList(); paragraph.push(line); }
        }
        flushParagraph();
        flushList();
        return blocks;
    }

    function appendInline(parent, text) {
        for (const part of parseInlineMarkdown(text)) {
            if (part.type === 'text') parent.appendChild(document.createTextNode(part.text));
            else if (part.type === 'link') {
                const link = document.createElement('a');
                link.href = part.url;
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
                link.textContent = part.text;
                parent.appendChild(link);
            } else {
                const element = document.createElement(part.type === 'strong' ? 'strong' : 'code');
                element.textContent = part.text;
                parent.appendChild(element);
            }
        }
    }

    function renderMarkdown(container, markdown) {
        for (const block of parseMarkdown(markdown)) {
            let element;
            if (block.type === 'heading') element = document.createElement(`h${block.level + 1}`);
            else if (block.type === 'list') {
                element = document.createElement('ul');
                for (const item of block.items) {
                    const listItem = document.createElement('li');
                    appendInline(listItem, item);
                    element.appendChild(listItem);
                }
            } else {
                element = document.createElement('p');
                appendInline(element, block.text);
            }
            if (block.type === 'heading') appendInline(element, block.text);
            container.appendChild(element);
        }
    }

    function shouldShowReleaseNotes(metadata, store) {
        if (!metadata || typeof metadata.revision !== 'string' || !metadata.revision.trim()) return false;
        if (typeof metadata.notes !== 'string' || !metadata.notes.trim()) return false;
        try { return store.getItem(DISMISSAL_KEY) !== metadata.revision; } catch { return true; }
    }

    function dismissReleaseNotes(revision, store) {
        try { store.setItem(DISMISSAL_KEY, revision); } catch { /* storage may be disabled */ }
    }

    function openDialog(metadata) {
        const dialog = document.getElementById('release-notes-dialog');
        const notes = document.getElementById('release-notes-content');
        const title = document.getElementById('release-notes-title');
        const revision = document.getElementById('release-notes-revision');
        const close = document.getElementById('release-notes-close');
        if (!dialog || !notes || !title || !revision || !close) return;
        title.textContent = `What’s new${metadata.version ? ` in ${metadata.version}` : ''}`;
        revision.textContent = `Revision ${metadata.revision}`;
        renderMarkdown(notes, metadata.notes);
        const store = browserStorage();
        const dismiss = () => { dismissReleaseNotes(metadata.revision, store); if (dialog.open) dialog.close(); };
        close.onclick = dismiss;
        dialog.oncancel = (event) => { event.preventDefault(); dismiss(); };
        dialog.showModal();
        close.focus();
    }

    function loadReleaseNotes() {
        fetch('/api/release-notes', { credentials: 'same-origin' })
            .then((response) => response.ok ? response.json() : null)
            .then((metadata) => {
                if (shouldShowReleaseNotes(metadata, browserStorage())) openDialog(metadata);
            })
            .catch(() => {});
    }

    return { DISMISSAL_KEY, parseInlineMarkdown, parseMarkdown, renderMarkdown, shouldShowReleaseNotes, dismissReleaseNotes, loadReleaseNotes };
});
