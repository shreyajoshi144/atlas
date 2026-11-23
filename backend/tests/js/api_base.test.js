// Regression tests: the API base URL is same-origin by default and browser storage cannot override it.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.join(__dirname, '../../../frontend/js');
const read = (f) => fs.readFileSync(path.join(JS, f), 'utf8');

// Run config.js in a sandbox with a fake localStorage, as a browser page would.
function loadConfig(store) {
  const removed = [];
  const localStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    removeItem: (k) => { removed.push(k); delete store[k]; },
  };
  const win = {};
  vm.runInNewContext(read('config.js'), { window: win, localStorage });
  return { win, removed };
}

let passed = 0;
const test = (name, fn) => { fn(); passed++; };

test('production default: empty config resolves to same-origin /api', () => {
  const { win } = loadConfig({});
  assert.strictEqual(win.ATLAS_API_BASE, '');
  assert.strictEqual(win.AtlasApi.resolveBase(win.ATLAS_API_BASE, 'http://localhost:7860'), 'http://localhost:7860/api');
  assert.strictEqual(win.AtlasApi.resolveBase('', 'https://x.hf.space'), 'https://x.hf.space/api');
});

test('a stale localhost:8000 value in localStorage is purged and never used', () => {
  const store = { atlas_api_base: 'http://localhost:8000/api' };
  const { win, removed } = loadConfig(store);
  assert.deepStrictEqual(removed, ['atlas_api_base']);
  assert(!('atlas_api_base' in store));
  assert.strictEqual(win.AtlasApi.resolveBase(win.ATLAS_API_BASE, 'http://localhost:7860'), 'http://localhost:7860/api');
});

test('a separate backend is configured only through ATLAS_API_BASE', () => {
  const { win } = loadConfig({});
  const r = win.AtlasApi.resolveBase;
  assert.strictEqual(r('https://api.example.com', 'http://o'), 'https://api.example.com/api');
  assert.strictEqual(r('https://api.example.com/', 'http://o'), 'https://api.example.com/api');
  assert.strictEqual(r('https://api.example.com/api', 'http://o'), 'https://api.example.com/api');
  assert.strictEqual(r('http://localhost:8000/api/', 'http://o'), 'http://localhost:8000/api');
});

test('malformed config falls back to same origin', () => {
  const r = loadConfig({}).win.AtlasApi.resolveBase;
  for (const bad of ['javascript:alert(1)', '//evil.com', '/api', 'localhost:8000', '  ', null, undefined]) {
    assert.strictEqual(r(bad, 'http://o'), 'http://o/api', String(bad));
  }
});

test('page scripts resolve the base only through AtlasApi and never read atlas_api_base', () => {
  for (const f of fs.readdirSync(JS).filter((n) => n.endsWith('.js') && n !== 'config.js')) {
    const s = read(f);
    assert(!/atlas_api_base/.test(s), `${f} reads atlas_api_base`);
    assert(!/:8000/.test(s), `${f} hard-codes :8000`);
  }
  assert(/AtlasApi\.resolveBase/.test(read('auth-page.js')));
  assert(/AtlasApi\.resolveBase/.test(read('script.js')));
});

test('config.js loads before the scripts that use it', () => {
  const dir = path.join(JS, '..');
  for (const [page, script] of [['auth.html', 'auth-page.js'], ['app.html', 'script.js']]) {
    const html = fs.readFileSync(path.join(dir, page), 'utf8');
    const a = html.indexOf('js/config.js'), b = html.indexOf('js/' + script);
    assert(a >= 0 && b > a, `${page}: config.js must precede ${script}`);
  }
});

console.log(`api_base.test.js: ${passed} tests passed`);
