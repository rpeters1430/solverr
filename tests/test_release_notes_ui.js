const test = require('node:test');
const assert = require('node:assert/strict');
const { parseInlineMarkdown, parseMarkdown, shouldShowReleaseNotes, dismissReleaseNotes, DISMISSAL_KEY } = require('../app/static/release_notes.js');

function storage(initial = null) {
  let value = initial;
  return {
    getItem: () => value,
    setItem: (_key, next) => { value = next; },
  };
}

test('release notes Markdown becomes safe text-oriented blocks', () => {
  const blocks = parseMarkdown('## Fixes\n- Faster scans\n\n<script>alert("no")</script>');
  assert.deepEqual(blocks, [
    { type: 'heading', level: 2, text: 'Fixes' },
    { type: 'list', items: ['Faster scans'] },
    { type: 'paragraph', text: '<script>alert("no")</script>' },
  ]);
  assert.deepEqual(parseInlineMarkdown('[Docs](https://example.com) **bold** `code`'), [
    { type: 'link', text: 'Docs', url: 'https://example.com/' },
    { type: 'text', text: ' ' },
    { type: 'strong', text: 'bold' },
    { type: 'text', text: ' ' },
    { type: 'code', text: 'code' },
  ]);
  assert.deepEqual(parseInlineMarkdown('[bad](javascript:alert)')[0], { type: 'text', text: '[bad](javascript:alert)' });
});

test('release dialog is hidden for empty notes and repeats only for a new revision', () => {
  const saved = storage();
  const release = { version: '1.0', revision: 'abc123', notes: '## Changes' };
  assert.equal(shouldShowReleaseNotes(release, saved), true);
  assert.equal(shouldShowReleaseNotes({ ...release, notes: ' ' }, saved), false);
  dismissReleaseNotes(release.revision, saved);
  assert.equal(saved.getItem(DISMISSAL_KEY), release.revision);
  assert.equal(shouldShowReleaseNotes(release, saved), false);
  assert.equal(shouldShowReleaseNotes({ ...release, revision: 'def456' }, saved), true);
});
