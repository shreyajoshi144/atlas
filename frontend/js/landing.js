/* Atlas landing page: header state, mobile menu and the claim → source trace. No app or auth logic. */
(function () {
  'use strict';
  var header = document.getElementById('siteHeader');
  var toggle = document.getElementById('navToggle'), drawer = document.getElementById('navDrawer');

  function onScroll() { if (header) header.classList.toggle('is-solid', (window.scrollY || 0) > 28); }
  window.addEventListener('scroll', onScroll, { passive: true }); onScroll();

  function setDrawer(open) {
    if (!toggle || !drawer) return;
    drawer.classList.toggle('show', open);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    toggle.textContent = open ? 'Close' : 'Menu';
    if (header && open) header.classList.add('is-solid'); else onScroll();
  }
  if (toggle) toggle.addEventListener('click', function () { setDrawer(!drawer.classList.contains('show')); });
  if (drawer) Array.prototype.forEach.call(drawer.querySelectorAll('a'), function (a) { a.addEventListener('click', function () { setDrawer(false); }); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') setDrawer(false); });

  /* ── Claim → source trace ───────────────────────────────────────── */
  var trace = document.getElementById('trace'), svg = document.getElementById('traceLines');
  var claims = Array.prototype.slice.call(document.querySelectorAll('.spec-claim'));
  var srcs = Array.prototype.slice.call(document.querySelectorAll('.spec-src'));
  var foot = document.getElementById('specFoot');
  var NOTE = {
    strong: 'Supported: two independent sources carry this claim.',
    mid: 'Partly supported: the sources overlap only in part. Atlas labels it as partial and keeps both links.',
    weak: 'Contested: the only source is an opinion piece. Atlas flags it instead of presenting it as fact.'
  };
  var current = claims[0] || null;

  function drawLines() {
    if (!trace || !svg || !current) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    if (window.getComputedStyle(svg).display === 'none') return;
    var box = trace.getBoundingClientRect();
    var ids = current.dataset.src.split(',');
    var a = current.getBoundingClientRect();
    var x1 = a.right - box.left, y1 = a.top + a.height / 2 - box.top;
    srcs.forEach(function (s) {
      if (ids.indexOf(s.dataset.id) === -1) return;
      var b = s.getBoundingClientRect();
      var x2 = b.left - box.left, y2 = b.top + b.height / 2 - box.top, mx = (x1 + x2) / 2;
      var p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      p.setAttribute('d', 'M' + x1 + ' ' + y1 + ' C' + mx + ' ' + y1 + ' ' + mx + ' ' + y2 + ' ' + x2 + ' ' + y2);
      svg.appendChild(p);
    });
  }
  function pick(c) {
    current = c;
    var ids = c.dataset.src.split(',');
    claims.forEach(function (x) { x.setAttribute('aria-pressed', x === c ? 'true' : 'false'); });
    srcs.forEach(function (s) { s.classList.toggle('on', ids.indexOf(s.dataset.id) !== -1); });
    if (foot) foot.textContent = NOTE[c.dataset.state] + ' (Illustration with sample text.)';
    drawLines();
  }
  claims.forEach(function (c) {
    c.addEventListener('click', function () { pick(c); });
    c.addEventListener('mouseenter', function () { pick(c); });
    c.addEventListener('focus', function () { pick(c); });
  });
  var raf = 0;
  window.addEventListener('resize', function () { cancelAnimationFrame(raf); raf = requestAnimationFrame(drawLines); });
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(drawLines);
  window.addEventListener('load', drawLines);
  drawLines();
})();
