/* Atlas authentication page. Same endpoints, payloads and session storage as the workspace
   (POST /auth/login, /auth/register, /auth/forgot-password, /auth/reset-password; GET /auth/me). */
(function () {
  'use strict';
  var $ = function (s) { return document.querySelector(s); }, $$ = function (s) { return Array.prototype.slice.call(document.querySelectorAll(s)); };

  function apiBase() {
    var stored = localStorage.getItem('atlas_api_base'); if (stored) return stored;
    if (window.ATLAS_API_BASE) return String(window.ATLAS_API_BASE).replace(/\/$/, '') + (String(window.ATLAS_API_BASE).endsWith('/api') ? '' : '/api');
    var local = ['localhost', '127.0.0.1', '::1'].indexOf(location.hostname) !== -1 || location.hostname === '';
    return local ? 'http://' + (location.hostname || '127.0.0.1') + ':8000/api' : location.origin + '/api';
  }
  function saveSession(token, user, remember) {
    localStorage.setItem('atlas_remember', remember ? '1' : '0');
    var active = remember ? localStorage : sessionStorage, other = remember ? sessionStorage : localStorage;
    other.removeItem('atlas_auth'); active.setItem('atlas_auth', JSON.stringify({ token: token, user: user }));
  }
  function storedToken() {
    try { var b = localStorage.getItem('atlas_remember') === '0' ? sessionStorage : localStorage; var s = JSON.parse(b.getItem('atlas_auth') || 'null'); return s && s.token || null; } catch (e) { return null; }
  }
  // Only same-app hash routes are honoured after sign-in (no open redirects).
  var params = new URLSearchParams(location.search);
  var next = /^#\/[A-Za-z]+$/.test(params.get('next') || '') ? params.get('next') : '';
  function goToApp() { location.replace('app.html' + next); }

  function errText(json, status) {
    if (status === 429) return 'Too many attempts. Please wait a minute and try again.';
    var d = json && json.detail;
    if (typeof d === 'string') return d;
    if (Array.isArray(d) && d.length && d[0].msg) return String(d[0].msg).replace(/^Value error, /, '');
    return null;
  }
  async function post(path, body) {
    var res = await fetch(apiBase() + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    var json = null; try { json = await res.json(); } catch (e) { /* non-JSON */ }
    return { ok: res.ok && !(json && json.success === false), status: res.status, json: json };
  }

  /* ── view switching ───────────────────────────────────────── */
  var VIEWS = ['signin', 'signup', 'forgot', 'reset'];
  function show(view, focus) {
    VIEWS.forEach(function (v) { $('#view-' + v).hidden = v !== view; });
    ['signin', 'signup'].forEach(function (v) { $('#tab-' + v).setAttribute('aria-selected', v === view ? 'true' : 'false'); });
    $('.au-tabs').style.display = (view === 'forgot' || view === 'reset') ? 'none' : '';
    document.title = { signin: 'Sign in to Atlas', signup: 'Create your Atlas account', forgot: 'Reset your Atlas password', reset: 'Set a new Atlas password' }[view];
    banner('');
    if (focus !== false) { var f = $('#view-' + view + ' input'); if (f) f.focus(); }
  }
  function banner(msg, kind) { var b = $('#banner'); b.textContent = msg || ''; b.className = 'au-banner' + (kind ? ' ' + kind : ''); }
  $$('[data-switch]').forEach(function (b) { b.addEventListener('click', function () { show(b.dataset.switch); }); });
  $('#tab-signin').addEventListener('click', function () { show('signin'); });
  $('#tab-signup').addEventListener('click', function () { show('signup'); });
  $$('.au-tab').forEach(function (t) { t.addEventListener('keydown', function (e) { if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') { e.preventDefault(); var to = t.id === 'tab-signin' ? 'signup' : 'signin'; show(to, false); $('#tab-' + to).focus(); } }); });
  $('#toForgot').addEventListener('click', function () { var e = $('#siEmail').value.trim(); show('forgot'); if (e) $('#fpEmail').value = e; });

  /* ── field helpers ────────────────────────────────────────── */
  function setErr(id, msg) { var i = $('#' + id), e = $('#' + id + 'Err'); e.textContent = msg || ''; if (msg) i.setAttribute('aria-invalid', 'true'); else i.removeAttribute('aria-invalid'); return !msg; }
  var EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  var rules = {
    email: function (id) { var v = $('#' + id).value.trim(); return setErr(id, !v ? 'Enter your email address.' : (!EMAIL.test(v) || v.length > 254) ? 'Enter a valid email address.' : ''); },
    name: function (id) { var v = $('#' + id).value.trim(); return setErr(id, !v ? 'Enter your name.' : v.length > 120 ? 'Use 120 characters or fewer.' : ''); },
    password: function (id) { var v = $('#' + id).value; return setErr(id, v.length < 8 ? 'Use at least 8 characters.' : v.length > 128 ? 'Use 128 characters or fewer.' : ''); },
    existing: function (id) { return setErr(id, $('#' + id).value ? '' : 'Enter your password.'); },
    confirm: function (id, other) { var v = $('#' + id).value; return setErr(id, !v ? 'Re-enter your password.' : v !== $('#' + other).value ? 'Passwords do not match.' : ''); },
  };
  [['siEmail', 'email'], ['suEmail', 'email'], ['fpEmail', 'email'], ['suName', 'name'], ['suPassword', 'password'], ['rpPassword', 'password']].forEach(function (p) {
    $('#' + p[0]).addEventListener('blur', function () { if ($('#' + p[0]).value) rules[p[1]](p[0]); });
    $('#' + p[0]).addEventListener('input', function () { if ($('#' + p[0]).getAttribute('aria-invalid')) rules[p[1]](p[0]); });
  });
  $('#suConfirm').addEventListener('blur', function () { if ($('#suConfirm').value) rules.confirm('suConfirm', 'suPassword'); });
  $('#rpConfirm').addEventListener('blur', function () { if ($('#rpConfirm').value) rules.confirm('rpConfirm', 'rpPassword'); });
  $('#suPassword').addEventListener('input', function () {
    var n = $('#suPassword').value.length, m = $('#suMeter');
    m.firstElementChild.style.width = Math.min(100, Math.round(n / 12 * 100)) + '%'; m.classList.toggle('ok', n >= 8);
  });
  $$('[data-pw]').forEach(function (b) { b.addEventListener('click', function () {
    var i = $('#' + b.dataset.pw), vis = i.type === 'password'; i.type = vis ? 'text' : 'password';
    b.textContent = vis ? 'Hide' : 'Show'; b.setAttribute('aria-label', vis ? 'Hide password' : 'Show password');
  }); });
  function busy(btn, on, label) { if (on) { btn.dataset.l = btn.textContent; btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> ' + label; } else { btn.disabled = false; btn.textContent = btn.dataset.l || btn.textContent; } }
  var NET = 'Could not reach the server. Check your connection and try again.';

  /* ── sign in ──────────────────────────────────────────────── */
  $('#signinForm').addEventListener('submit', async function (e) {
    e.preventDefault(); $('#siFormErr').textContent = '';
    var ok = rules.email('siEmail') & rules.existing('siPassword'); if (!ok) { var bad = $('#signinForm [aria-invalid="true"]'); if (bad) bad.focus(); return; }
    var btn = $('#siBtn'); busy(btn, true, 'Signing in…');
    try {
      var r = await post('/auth/login', { email: $('#siEmail').value.trim(), password: $('#siPassword').value });
      if (!r.ok) { $('#siFormErr').textContent = errText(r.json, r.status) || 'Invalid email or password.'; return; }
      saveSession(r.json.data.access_token, r.json.data.user, $('#siRemember').checked); goToApp();
    } catch (err) { $('#siFormErr').textContent = NET; } finally { busy(btn, false); }
  });

  /* ── sign up ──────────────────────────────────────────────── */
  $('#signupForm').addEventListener('submit', async function (e) {
    e.preventDefault(); $('#suFormErr').textContent = '';
    var ok = rules.name('suName') & rules.email('suEmail') & rules.password('suPassword') & rules.confirm('suConfirm', 'suPassword');
    if (!ok) { var bad = $('#signupForm [aria-invalid="true"]'); if (bad) bad.focus(); return; }
    var btn = $('#suBtn'); busy(btn, true, 'Creating account…');
    try {
      var r = await post('/auth/register', { name: $('#suName').value.trim(), email: $('#suEmail').value.trim(), password: $('#suPassword').value });
      if (!r.ok) { $('#suFormErr').textContent = errText(r.json, r.status) || 'Could not create the account.'; return; }
      saveSession(r.json.data.access_token, r.json.data.user, true); goToApp();
    } catch (err) { $('#suFormErr').textContent = NET; } finally { busy(btn, false); }
  });

  /* ── forgot / reset ───────────────────────────────────────── */
  var devToken = null, resetToken = params.get('reset_token');
  $('#forgotForm').addEventListener('submit', async function (e) {
    e.preventDefault(); $('#fpFormErr').textContent = '';
    if (!rules.email('fpEmail')) { $('#fpEmail').focus(); return; }
    var btn = $('#fpBtn'); busy(btn, true, 'Requesting…');
    try {
      var r = await post('/auth/forgot-password', { email: $('#fpEmail').value.trim() });
      if (r.status === 404) { $('#fpFormErr').textContent = 'Password reset is not available on this deployment.'; return; }
      if (!r.ok) { $('#fpFormErr').textContent = errText(r.json, r.status) || 'We could not complete the request. Please try again.'; return; }
      $('#forgotForm').hidden = true; $('#fpDone').hidden = false;
      $('#fpDoneMsg').textContent = (r.json && r.json.message) || 'If an account exists for that email, a reset link is on its way.';
      devToken = r.json && r.json.data && r.json.data.dev_reset_token || null;   // development deployments only
      $('#fpDev').hidden = !devToken;
    } catch (err) { $('#fpFormErr').textContent = NET; } finally { busy(btn, false); }
  });
  $('#fpDev').addEventListener('click', function () { resetToken = devToken; show('reset'); });
  $('#resetForm').addEventListener('submit', async function (e) {
    e.preventDefault(); $('#rpFormErr').textContent = '';
    var ok = rules.password('rpPassword') & rules.confirm('rpConfirm', 'rpPassword'); if (!ok) return;
    var btn = $('#rpBtn'); busy(btn, true, 'Saving…');
    try {
      var r = await post('/auth/reset-password', { token: resetToken, new_password: $('#rpPassword').value });
      if (!r.ok) { $('#rpFormErr').textContent = errText(r.json, r.status) || 'This reset link is invalid or has expired. Request a new one.'; return; }
      history.replaceState(null, '', 'auth.html?mode=signin'); resetToken = null; show('signin', false);
      banner('Password updated. Sign in with your new password.', 'good'); $('#siPassword').focus();
    } catch (err) { $('#rpFormErr').textContent = NET; } finally { busy(btn, false); }
  });

  /* ── entry ────────────────────────────────────────────────── */
  var mode = params.get('mode');
  if (resetToken) show('reset', true); else if (location.hash === '#forgot') show('forgot', true); else show(mode === 'signup' ? 'signup' : 'signin', false);
  var notice = params.get('notice'); if (notice) banner(notice.slice(0, 160), /expired|invalid/i.test(notice) ? 'bad' : '');
  // Already signed in? Verify the stored session, then go straight to the workspace.
  var t = storedToken();
  if (t && !resetToken) fetch(apiBase() + '/auth/me', { headers: { Authorization: 'Bearer ' + t } }).then(function (r) { if (r.ok) goToApp(); }).catch(function () {});
  if (!$('#view-signin').hidden) { var f = $('#siEmail'); if (f && !notice) f.focus(); }
})();
