"""Browser end-to-end checks for the redesigned frontend (real Chromium via Playwright) against the mock API
in mock_server.py. Verifies wiring, navigation, auth flows, research run (success + failure) and responsive
layout. It does NOT exercise the real backend and does not judge aesthetics.
Run:  pip install playwright && playwright install chromium && python tests/e2e/test_frontend_e2e.py"""
import os, sys, re, json
sys.path.insert(0, os.path.dirname(__file__))
import mock_server
from playwright.sync_api import sync_playwright
CHROME = os.environ.get('CHROME_PATH')  # optional explicit browser path
BASE = 'http://127.0.0.1:5500/'
fails, checks = [], 0
def ok(c, m):
    global checks; checks += 1
    print(('  ok  : ' if c else '  FAIL: ') + m)
    if not c: fails.append(m)
SESSION = "localStorage.setItem('atlas_auth', JSON.stringify({token:'tok',user:{name:'Shreya Test',email:'shreya@example.com'}}))"
def page(b, w=1440, h=900, authed=False):
    ctx = b.new_context(viewport={'width': w, 'height': h})
    if authed: ctx.add_init_script(SESSION)
    pg = ctx.new_page(); pg.errs = []
    pg.on('pageerror', lambda e: pg.errs.append(str(e)))
    return pg
def overflow(pg): return pg.evaluate('document.documentElement.scrollWidth - document.documentElement.clientWidth')

def run():
    a, s = mock_server.serve()
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, args=['--no-sandbox']) if CHROME else p.chromium.launch(args=['--no-sandbox'])
        print('# landing')
        pg = page(b); pg.goto(BASE + 'index.html'); pg.wait_for_timeout(500)
        ok('inspect' in pg.inner_text('h1').lower() or 'trace' in pg.inner_text('h1').lower(), 'hero states the value')
        ok(pg.locator('h1').count() == 1, 'exactly one h1')
        txt = pg.inner_text('main')
        ok(all(t in txt for t in ['AI Playground', 'AtlasBench', 'Workflow Studio', 'Runtime & Control']), 'product areas explained')
        ok(all(pg.locator('#' + i).count() == 1 for i in ['product', 'how', 'features', 'use-cases', 'security', 'faq', 'playground', 'atlasbench', 'workflow', 'runtime', 'join']), 'all section anchors exist')
        hrefs = pg.eval_on_selector_all('a[href^="#"]', 'els => els.map(e => e.getAttribute("href")).filter(h => h.length > 1)')
        ok(all(pg.locator(h).count() >= 1 for h in hrefs), 'every in-page link has a target')
        ok(pg.locator('a[href^="auth.html?mode=signup"]').count() >= 4 and pg.locator('a[href^="auth.html?mode=signin"]').count() >= 3, 'sign up / sign in actions throughout')
        ok(all(pg.locator(f'footer a[href="{h}"]').count() for h in ['terms.html', 'privacy.html', 'contact.html']), 'footer links terms, privacy, contact')
        ok(pg.locator('#mainView').count() == 0 and pg.locator('script[src*="script.js"]').count() == 0, 'no workspace code on the landing page')
        pg.locator('.spec-claim').nth(2).click()
        ok(pg.locator('.spec-src.on').count() == 1 and 'Contested' in pg.inner_text('#specFoot'), 'trace: selecting a claim highlights only its sources')
        ok(pg.locator('#traceLines path').count() == 1, 'trace: a connector thread is drawn per supporting source')
        ok(pg.locator('details.lp-faq').count() >= 6, 'FAQ present')
        ok(overflow(pg) <= 0, 'no horizontal overflow at 1440')
        ok(not pg.errs, 'no script errors: %s' % pg.errs[:1]); pg.context.close()
        for w in (768, 390):
            pg = page(b, w, 800); pg.goto(BASE + 'index.html'); pg.wait_for_timeout(400)
            ok(overflow(pg) <= 0, f'landing: no horizontal overflow at {w}px')
            if w == 390:
                pg.click('#navToggle'); ok(pg.locator('#navDrawer.show').count() == 1, 'mobile menu opens')
                ok(pg.locator('.sh-nav').is_hidden(), 'desktop nav hidden on mobile')
            pg.context.close()

        print('# auth')
        pg = page(b); pg.goto(BASE + 'auth.html?mode=signin&next=%23%2Fresearch'); pg.wait_for_timeout(300)
        ok(pg.locator('#view-signin').is_visible() and pg.locator('#view-signup').is_hidden(), 'sign-in shown first')
        pg.click('#siBtn'); ok('valid' in pg.inner_text('#siEmailErr') or 'Enter your email' in pg.inner_text('#siEmailErr'), 'empty submit shows inline errors')
        pg.fill('#siEmail', 'shreya@example.com'); pg.fill('#siPassword', 'wrong'); pg.click('#siBtn'); pg.wait_for_timeout(500)
        ok('Invalid email or password' in pg.inner_text('#siFormErr'), 'API rejection shown'); ok(pg.locator('#siBtn').is_enabled(), 'button re-enabled')
        pg.click('[data-pw="siPassword"]'); ok(pg.get_attribute('#siPassword', 'type') == 'text', 'show password works')
        pg.fill('#siPassword', 'correct-horse'); pg.uncheck('#siRemember'); pg.click('#siBtn')
        pg.wait_for_url(re.compile(r'app\.html#/research'), timeout=5000)
        ok(pg.evaluate("sessionStorage.getItem('atlas_auth')") is not None and pg.evaluate("localStorage.getItem('atlas_remember')") == '0', 'unchecked keep-signed-in uses session storage')
        ok('app.html#/research' in pg.url, 'redirected into the workspace on the requested route'); pg.context.close()
        pg = page(b); pg.goto(BASE + 'auth.html?mode=signup'); pg.wait_for_timeout(300)
        ok(pg.locator('#view-signup').is_visible(), 'mode=signup opens sign-up')
        pg.fill('#suName', 'New'); pg.fill('#suEmail', 'new@example.com'); pg.fill('#suPassword', 'short'); pg.fill('#suConfirm', 'short'); pg.click('#suBtn')
        ok('at least 8' in pg.inner_text('#suPasswordErr'), 'password length rule')
        pg.fill('#suPassword', 'long-enough-1'); pg.fill('#suConfirm', 'different-1'); pg.click('#suBtn'); ok('do not match' in pg.inner_text('#suConfirmErr'), 'mismatch caught')
        pg.fill('#suEmail', 'taken@example.com'); pg.fill('#suConfirm', 'long-enough-1'); pg.click('#suBtn'); pg.wait_for_timeout(500)
        ok('already exists' in pg.inner_text('#suFormErr'), 'duplicate email error shown')
        pg.fill('#suEmail', 'fresh@example.com'); pg.click('#suBtn'); pg.wait_for_url(re.compile(r'app\.html'), timeout=5000); ok(True, 'registration enters the workspace'); pg.context.close()
        pg = page(b); pg.goto(BASE + 'auth.html'); pg.click('#toForgot'); ok(pg.locator('#view-forgot').is_visible(), 'forgot view opens')
        pg.fill('#fpEmail', 'shreya@example.com'); pg.click('#fpBtn'); pg.wait_for_selector('#fpDone:not([hidden])')
        ok('If an account exists' in pg.inner_text('#fpDoneMsg'), 'generic reset confirmation')
        pg.click('#fpDev'); pg.fill('#rpPassword', 'brand-new-pass'); pg.fill('#rpConfirm', 'brand-new-pass'); pg.click('#rpBtn'); pg.wait_for_timeout(500)
        ok('Password updated' in pg.inner_text('#banner'), 'reset succeeds back to sign-in'); pg.context.close()
        pg = page(b); pg.goto(BASE + 'auth.html?reset_token=bad'); pg.fill('#rpPassword', 'brand-new-pass'); pg.fill('#rpConfirm', 'brand-new-pass'); pg.click('#rpBtn'); pg.wait_for_timeout(500)
        ok(re.search('invalid|expired', pg.inner_text('#rpFormErr'), re.I) is not None, 'invalid reset token error'); pg.context.close()
        for w in (390, 768, 1440):
            pg = page(b, w, 800); pg.goto(BASE + 'auth.html?mode=signup'); pg.wait_for_timeout(300); ok(overflow(pg) <= 0, f'auth: no horizontal overflow at {w}px'); pg.context.close()

        print('# workspace guard')
        pg = page(b); pg.goto(BASE + 'app.html'); pg.wait_for_url(re.compile('auth.html'), timeout=5000); ok(True, 'no session redirects to sign-in'); pg.context.close()

        print('# workspace navigation')
        pg = page(b, authed=True); pg.goto(BASE + 'app.html'); pg.wait_for_selector('.ov-title'); pg.wait_for_selector('.act-row')
        ok(pg.locator('#appRoot.auth-locked').count() == 0, 'workspace unlocks with a valid session')
        ok(pg.locator('.act-row').count() == 3, 'recent activity merges cases and conversations')
        ok(pg.locator('.topnav-item').evaluate_all('els => els.map(e => e.dataset.nav).join()') == 'home,research,playground,compare,atlasbench,workflowStudio,runtime', 'seven areas in the rail (Compare added)')
        ok(pg.locator('#accountName').inner_text() == 'Shreya Test' and pg.inner_text('#accountAvatar') == 'S', 'account identity shown in the rail')
        pg.click('.topnav-item[data-nav="research"]'); pg.wait_for_selector('#rTopic')
        ok(pg.locator('#subnav.show').count() == 1 and pg.locator('.subtab').count() == 6, 'Research shows its six-part sub-bar')
        ok(pg.get_attribute('.topnav-item[data-nav="research"]', 'aria-current') == 'page', 'active area exposed via aria-current')
        ok(pg.locator('input[name="rMode"]').count() == 5, 'five research modes')
        pg.click('#rRunBtn'); ok('at least 3' in pg.inner_text('#toastHost'), 'too-short question rejected with a message')
        for sub in ['history', 'kb', 'search', 'knowledge', 'analytics']:
            pg.click(f'.subtab[data-sub="{sub}"]'); pg.wait_for_timeout(350)
            ok(len(pg.inner_html('#mainView')) > 60, f'sub-view {sub} renders')
        pg.click('.subtab[data-sub="history"]'); pg.wait_for_selector('.case-row'); ok(pg.locator('.case-row').count() == 2, 'Cases lists reports')
        pg.click('.case-row >> nth=0'); pg.wait_for_selector('.case-figures'); ok('solid-state' in pg.inner_text('.page-title').lower(), 'opening a case shows its title')
        ok(pg.locator('.tab').count() == 6, 'case has six tabs'); pg.click('.tab[data-tab="sources"]'); ok(pg.locator('#tab-sources.active').count() == 1, 'case tabs switch')
        for key in ['playground', 'atlasbench', 'workflowStudio', 'runtime']:
            pg.click(f'.topnav-item[data-nav="{key}"]'); pg.wait_for_timeout(450)
            ok(len(pg.inner_html('#mainView')) > 80 and pg.locator(f'.topnav-item[data-nav="{key}"].active').count() == 1, f'{key} renders and is active')
            ok(pg.locator('#subnav.show').count() == 0, f'sub-bar hidden outside Research ({key})')
        ok('outing' in pg.inner_html('#mainView'), 'Runtime & Control shows routing interface')
        pg.click('.topnav-item[data-nav="workflowStudio"]'); pg.wait_for_selector('#wfTemplate'); pg.click('[data-wf-select="w1"]'); pg.wait_for_selector('#wfInput'); ok(True, 'Workflow Studio: select workflow reveals run form')
        pg.click('#accountTrigger'); pg.click('#settingsMenuItem'); pg.wait_for_selector('#setModel'); ok(pg.locator('.settings-group').count() == 6, 'Settings renders six groups')
        pg.keyboard.press('Control+k'); ok(pg.locator('#searchBar').is_visible(), 'Ctrl+K opens search'); pg.keyboard.press('Escape')
        pg.click('#chatFabToggle'); ok(pg.locator('#chatFabWrap.open').count() == 1, 'Ask Atlas widget opens')
        ok(not pg.errs, 'no script errors across the workspace: %s' % pg.errs[:1])
        pg.click('#chatFabToggle'); pg.click('#accountTrigger'); pg.click('#logoutMenuItem'); pg.click('#logoutModal .btn-danger'); pg.wait_for_url(re.compile('auth.html'), timeout=5000); ok(True, 'sign out returns to sign-in'); pg.context.close()

        print('# research run')
        pg = page(b, authed=True)
        pg.route('**/research/run', lambda r: r.fulfill(status=500, content_type='application/json', body='{"detail":"boom"}'))
        pg.goto(BASE + 'app.html#/research'); pg.wait_for_selector('#rTopic'); pg.fill('#rTopic', 'How does retrieval-augmented generation work?'); pg.click('#rRunBtn')
        pg.wait_for_selector('.rs-err'); ok('could not be opened' in pg.inner_text('#rResultBox'), 'failed run shows an error panel')
        ok(pg.locator('#rsForm').is_visible() and 'retrieval' in pg.input_value('#rTopic'), 'form restored with question intact'); pg.context.close()
        pg = page(b, authed=True); pg.goto(BASE + 'app.html#/research'); pg.wait_for_selector('#rTopic')
        pg.fill('#rTopic', 'How does retrieval-augmented generation work?'); pg.click('input[name="rMode"][value="brief"]', force=True)
        with pg.expect_request('**/research/run') as req: pg.click('#rRunBtn')
        sent = json.loads(req.value.post_data)
        ok(sent['research_mode'] == 'brief' and sent['top_k_urls'] == 8 and sent['include_verification'] is True, 'request payload matches the API contract')
        pg.wait_for_selector('.case-figures'); ok('retrieval' in pg.inner_text('.page-title').lower(), 'successful run lands on the case view'); pg.context.close()

        print('# responsive workspace')
        for w, h in ((1440, 900), (1024, 800), (768, 900), (390, 800)):
            pg = page(b, w, h, authed=True)
            for route in ['home', 'research', 'playground', 'atlasbench', 'workflowStudio', 'runtime', 'history', 'kb', 'settings']:
                pg.goto(BASE + 'app.html#/' + route); pg.reload(); pg.wait_for_timeout(500)
                if overflow(pg) > 0: ok(False, f'{route} @ {w}px overflows by {overflow(pg)}px')
            ok(True, f'workspace routes checked at {w}px')
            if w <= 1000:
                pg.goto(BASE + 'app.html#/home'); pg.reload(); pg.wait_for_selector('.ov-title')
                ok(pg.locator('.rail').is_hidden() and pg.locator('#mobileNavToggle').is_visible(), f'{w}px: rail collapses to a menu button')
                pg.click('#mobileNavToggle'); ok(pg.locator('#mobileNavDrawer.show').count() == 1, f'{w}px: drawer opens')
                pg.click('#mobileNavDrawer [data-nav="playground"]'); pg.wait_for_timeout(300); ok(pg.locator('#mobileNavDrawer.show').count() == 0 and pg.locator('.pg-page').count() == 1, f'{w}px: drawer navigates and closes')
            pg.context.close()

        print('# accessibility basics')
        for f, authed in (('index.html', 0), ('auth.html', 0), ('app.html', 1), ('terms.html', 0), ('privacy.html', 0), ('contact.html', 0)):
            pg = page(b, authed=bool(authed)); pg.goto(BASE + f); pg.wait_for_timeout(700)
            ok(pg.evaluate('document.documentElement.lang') == 'en' and bool(pg.title()), f'{f}: lang and title')
            ok(pg.locator('h1').count() == 1, f'{f}: exactly one h1 (found {pg.locator("h1").count()})')
            un = pg.evaluate('''() => [...document.querySelectorAll('input:not([type=hidden]):not([type=radio]):not([type=checkbox]), textarea, select')].filter(i => !(i.id && document.querySelector('label[for="'+i.id+'"]')) && !i.closest('label') && !i.getAttribute('aria-label')).map(i => i.id || i.className)''')
            ok(not un, f'{f}: every field labelled {un[:2]}')
            nm = pg.evaluate('''() => [...document.querySelectorAll('button, a[href]')].filter(b => !(b.textContent||'').trim() && !b.getAttribute('aria-label') && !b.getAttribute('title')).length''')
            ok(nm == 0, f'{f}: every button and link has a name'); ok(pg.locator('main').count() >= 1, f'{f}: main landmark'); pg.context.close()
        b.close()
    print(f'\nfrontend e2e: {checks - len(fails)}/{checks} checks passed')
    return 1 if fails else 0
if __name__ == '__main__': sys.exit(run())
