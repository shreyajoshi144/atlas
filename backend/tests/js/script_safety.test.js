// Regression tests: report/source links must never carry a non-http(s) scheme.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '../../../frontend/js/script.js'), 'utf8');

function extract(name) {
  const start = src.indexOf(`function ${name}(`);
  assert(start >= 0, `${name} not found`);
  let depth = 0, i = src.indexOf('{', start);
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') depth++;
    if (src[j] === '}' && --depth === 0) return src.slice(start, j + 1);
  }
  throw new Error(`unterminated ${name}`);
}
const code = ['escapeHtml', 'escapeAttr', 'safeUrl', 'mdToHtml'].map(extract).join('\n');
const { safeUrl, mdToHtml } = new Function(`${code}; return { safeUrl, mdToHtml };`)();

let passed = 0;
const test = (name, fn) => { fn(); passed++; };

test('safeUrl allows only http(s)', () => {
  assert.strictEqual(safeUrl('https://example.com/a?b=1'), 'https://example.com/a?b=1');
  assert.strictEqual(safeUrl('HTTP://example.com'), 'HTTP://example.com');
  for (const bad of ['javascript:alert(1)', ' javascript:alert(1)', 'data:text/html,<b>', 'vbscript:x', '//evil.com', '', null, undefined]) {
    assert.strictEqual(safeUrl(bad), '', String(bad));
  }
});
test('markdown links with a javascript: scheme are rendered as plain text', () => {
  const html = mdToHtml('[click](javascript:alert(1))');
  assert(!/href=/i.test(html), html);
  assert(html.includes('click'));
});
test('markdown http links keep a safe, noopener anchor', () => {
  const html = mdToHtml('[ok](https://example.com/x)');
  assert(html.includes('href="https://example.com/x"'));
  assert(html.includes('noopener noreferrer'));
});
test('raw HTML in markdown is escaped', () => {
  const html = mdToHtml('<img src=x onerror=alert(1)>');
  assert(!html.includes('<img'));
});
console.log(`script_safety.test.js: ${passed} passed`);
