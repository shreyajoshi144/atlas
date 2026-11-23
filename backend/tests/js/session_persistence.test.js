// Regression: "Keep me signed in" unchecked must stay in sessionStorage, including after the
// workspace refreshes the session on load; checked must stay in localStorage.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '../../../frontend/js/script.js'), 'utf8');

const start = src.indexOf('Atlas.auth = {');
assert(start >= 0, 'Atlas.auth not found');
let depth = 0, end = -1;
for (let j = src.indexOf('{', start); j < src.length; j++) {
  if (src[j] === '{') depth++;
  if (src[j] === '}' && --depth === 0) { end = j + 1; break; }
}
const block = src.slice(start, end);

function makeStore() { const m = {}; return { getItem: (k) => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); }, removeItem: (k) => { delete m[k]; } }; }
function fresh() {
  const localStorage = makeStore(), sessionStorage = makeStore();
  const Atlas = new Function('localStorage', 'sessionStorage', `const Atlas = {}; ${block}; return Atlas;`)(localStorage, sessionStorage);
  return { Atlas, localStorage, sessionStorage };
}

let passed = 0;
const test = (name, fn) => { fn(); passed++; };

test('unchecked: sign-in uses sessionStorage only', () => {
  const { Atlas, localStorage, sessionStorage } = fresh();
  Atlas.auth.set('tok', { name: 'A' }, false);
  assert(sessionStorage.getItem('atlas_auth')); assert.strictEqual(localStorage.getItem('atlas_auth'), null);
  assert.strictEqual(Atlas.auth.remember, false); assert.strictEqual(Atlas.auth.token, 'tok');
});
test('unchecked: the workspace session refresh keeps it in sessionStorage', () => {
  const { Atlas, localStorage, sessionStorage } = fresh();
  Atlas.auth.set('tok', { name: 'A' }, false);
  Atlas.auth.set(Atlas.auth.token, { name: 'A2' }, Atlas.auth.remember);   // what bootstrapSession does
  assert(sessionStorage.getItem('atlas_auth')); assert.strictEqual(localStorage.getItem('atlas_auth'), null);
  assert.strictEqual(localStorage.getItem('atlas_remember'), '0'); assert.strictEqual(Atlas.auth.user.name, 'A2');
});
test('checked: refresh stays persistent, and a fresh browser defaults to persistent', () => {
  const { Atlas, localStorage, sessionStorage } = fresh();
  assert.strictEqual(Atlas.auth.remember, true);
  Atlas.auth.set('tok', { name: 'A' }, true);
  Atlas.auth.set(Atlas.auth.token, { name: 'A2' }, Atlas.auth.remember);
  assert(localStorage.getItem('atlas_auth')); assert.strictEqual(sessionStorage.getItem('atlas_auth'), null);
});
test('clear removes the session from both stores', () => {
  const { Atlas, localStorage, sessionStorage } = fresh();
  Atlas.auth.set('tok', {}, false); Atlas.auth.clear();
  assert.strictEqual(sessionStorage.getItem('atlas_auth'), null); assert.strictEqual(localStorage.getItem('atlas_auth'), null);
});
test('the refresh call site passes the stored preference', () => {
  assert(/Atlas\.auth\.set\(stored, json\.data, Atlas\.auth\.remember\)/.test(src));
  assert.strictEqual((src.match(/Atlas\.auth\.set\(/g) || []).length, 1, 'unexpected extra auth.set callers');
});
console.log(`session_persistence.test.js: ${passed} tests passed`);
