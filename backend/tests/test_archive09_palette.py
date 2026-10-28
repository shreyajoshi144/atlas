"""Meadow palette guard: the shipped frontend may only use the eight meadow inks (see css/tokens.css)."""
import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
ALLOWED = {"#FBF7F0", "#F2EADF", "#2E292B", "#7A6270", "#C8999C", "#CDBF9B", "#9A9070", "#6E6266"}   # ivory, paper, charcoal, mauve, rose, straw, dry grass, stem
FILES = ["css/tokens.css", "css/atlas.css", "css/site.css", "css/landing.css", "index.html", "app.html", "auth.html", "terms.html", "privacy.html", "contact.html"] + \
        [f"js/{p.name}" for p in (FRONTEND / "js").glob("*.js")]


def _hexes(text):
    return {m.upper()[:7] for m in re.findall(r"#[0-9A-Fa-f]{6}(?![0-9A-Za-z])|#[0-9A-Fa-f]{3}(?![0-9A-Za-z-])", text)}


def test_only_archive_09_hex_colors_are_used():
    stray = {}
    for rel in FILES:
        text = (FRONTEND / rel).read_text(encoding="utf8")
        text = re.sub(r"&#\d+;|&#x[0-9a-fA-F]+;", "", text)          # HTML entities are not colors
        bad = {h for h in _hexes(text) if h not in ALLOWED and len(h) == 7}
        # 3-digit hex shorthands are ambiguous with anchors/IDs; only flag when used as a CSS color value
        bad |= {h for h in re.findall(r"(?:color|fill|stroke|background)\s*[:=]\s*\"?(#[0-9A-Fa-f]{3})\b", text)}
        if bad:
            stray[rel] = sorted(bad)
    assert not stray, stray


def test_no_glass_or_forbidden_hue_functions():
    for rel in FILES:
        text = (FRONTEND / rel).read_text(encoding="utf8")
        assert "backdrop-filter" not in text.split("/* ")[0] or rel.endswith(".html"), rel
        assert not re.search(r"backdrop-filter\s*:", text), rel
        assert not re.search(r"\b(hsl|hwb|lab|lch|oklch)\(", text), rel
        assert not re.search(r"\b(blue|green|orange|purple|violet|teal|cyan|indigo|emerald|red|yellow|pink)\b\s*[;,)]", text, re.I), rel


def test_old_stylesheet_is_gone_and_pages_link_the_new_ones():
    assert not (FRONTEND / "css/style.css").exists()
    assert (FRONTEND / "assets/meadow-hero.jpg").exists() and (FRONTEND / "assets/meadow-soft.jpg").exists()
    page = lambda n: (FRONTEND / n).read_text(encoding="utf8")
    assert 'href="css/tokens.css"' in page("app.html") and 'href="css/atlas.css"' in page("app.html")
    for n in ("index.html", "auth.html", "terms.html", "privacy.html", "contact.html"):
        assert 'href="css/tokens.css"' in page(n) and 'href="css/site.css"' in page(n), n
    assert 'href="css/landing.css"' in page("index.html")


def test_public_pages_are_separate_from_the_workspace():
    app, landing, auth = [(FRONTEND / n).read_text(encoding="utf8") for n in ("app.html", "index.html", "auth.html")]
    assert 'id="mainView"' in app and 'id="mainView"' not in landing and 'id="mainView"' not in auth
    assert 'src="js/script.js"' not in landing and 'src="js/script.js"' not in auth       # no workspace code on public pages
    assert 'auth.html?mode=signup' in landing and 'auth.html?mode=signin' in landing
    for n in ("terms.html", "privacy.html", "contact.html"):
        assert f'href="{n}"' in landing
