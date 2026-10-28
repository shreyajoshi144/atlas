"""Browser smoke test of the Compare view against compare_mock.py (canned API; not the real backend).
Run: python frontend/tests/e2e/test_compare_e2e.py   (set SHOT_DIR for screenshots, CHROME_PATH optional)"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
import compare_mock, mock_server
from playwright.sync_api import sync_playwright
BASE = 'http://127.0.0.1:5500/'; SHOTS = os.environ.get('SHOT_DIR', '/tmp')
fails, checks = [], 0
def ok(c, m):
    global checks; checks += 1; print(('  ok  : ' if c else '  FAIL: ') + m)
    if not c: fails.append(m)
SESSION = "localStorage.setItem('atlas_auth', JSON.stringify({token:'tok',user:{name:'Shreya Test',email:'s@example.com'}}))"
def run():
    a, s = mock_server.serve()
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=os.environ.get('CHROME_PATH') or None, args=['--no-sandbox'])
        for w, h in ((1280, 900), (360, 800)):
            ctx = b.new_context(viewport={'width': w, 'height': h}); ctx.add_init_script(SESSION)
            pg = ctx.new_page(); errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
            dialogs = []; pg.on('dialog', lambda d: (dialogs.append(d.message), d.dismiss()))
            pg.goto(BASE + 'app.html#/compare'); pg.wait_for_timeout(600)
            if pg.locator('#mainView .cmp-page').count() == 0:
                pg.evaluate("Atlas.nav('compare')"); pg.wait_for_timeout(500)
            print(f'# width {w}')
            ok(pg.locator('.cmp-page').count() == 1, 'compare view renders')
            ok(pg.locator('[data-cmp-model]').count() == 4, 'four models listed')
            ok(pg.locator('[data-cmp-model][value="xai:grok-4.7"]').is_disabled(), 'unconfigured provider model disabled')
            ok('XAI_API_KEY' in pg.inner_text('.cmp-page'), 'env var hint shown for unconfigured provider')
            ok(pg.locator('#cmpRun').is_disabled(), 'run disabled until consent and prompt')
            pg.fill('#cmpPrompt', 'How does RAG work?'); pg.check('#cmpConsent')
            ok(pg.locator('#cmpRun').is_enabled(), 'run enabled after prompt + consent')
            ok('Worst-case cost before running' in pg.inner_text('#cmpGate'), 'worst-case cost estimate shown before run')
            pg.fill('#cmpMaxOut', '8000'); pg.wait_for_timeout(100)
            ok(pg.locator('#cmpRun').is_disabled() and 'ceiling' in pg.inner_text('#cmpGate'), 'cost ceiling blocks run client-side')
            pg.fill('#cmpMaxOut', '')
            pg.click('#cmpRun'); pg.wait_for_selector('[data-cmp-result]', timeout=5000)
            ok(pg.locator('[data-cmp-result]').count() == 3, 'three result cards')
            txt = pg.inner_text('#cmpResults')
            ok('Provider rate limit' in txt and 'slow down' in txt, 'failure explained in plain words')
            ok('cut off' in txt, 'truncation warning shown')
            ok('Not reported' in txt, 'missing values say Not reported')
            ok('Measured differences' in txt and 'No measured evaluation evidence' in txt, 'summary + evaluation honesty')
            ok('best model' not in txt.lower() and 'winner' not in txt.lower(), 'no best-model wording')
            ok(pg.locator('#cmpResults script').count() == 0 and not dialogs and not errs, 'script payload inert; no dialogs/errors %s' % errs[:1])
            ok('instruction-like text' in txt, 'source risk flag shown')
            ok(pg.locator('#cmpResults a[target=_blank]').evaluate_all('els => els.every(e => /noopener/.test(e.rel) && /^https?:/.test(e.href))'), 'links safe')
            body = [c for c in compare_mock.CALLS if c[0] == 'POST' and c[1].endswith('/run')][-1][2]
            ok(body['consent_external'] is True and body['idempotency_key'].startswith('cmp-') and len(body['models']) == 3, 'request carries consent, idempotency key, models')
            ov = pg.evaluate('document.documentElement.scrollWidth - document.documentElement.clientWidth')
            ok(ov <= 0, f'no horizontal overflow ({ov})')
            pg.screenshot(path=f'{SHOTS}/compare_{w}.png', full_page=True)
            pg.click('input[name=cmpMode][value=ask]'); pg.click('#cmpRun'); pg.wait_for_selector('.cmp-timeline', timeout=5000)
            ok('Fallback was used' in pg.inner_text('.cmp-timeline'), 'ask mode shows fallback timeline')
            pg.screenshot(path=f'{SHOTS}/compare_ask_{w}.png', full_page=True)
            pg.click('[data-cmp-open]'); pg.wait_for_selector('[data-cmp-result]'); ok(True, 'history item opens')
            ctx.close()
        b.close()
    print(f'{checks} checks, {len(fails)} failed'); sys.exit(1 if fails else 0)
run()
