"""Build the static GitHub Pages docs: python scripts/build_site.py."""
from pathlib import Path
import html
import re
import shutil
import xml.etree.ElementTree as ET
import markdown

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '_site'
BASE = 'https://aceatdev.github.io/ascended-browser/'
REPO = 'https://github.com/AceAtDev/ascended-browser'
PAGES = [
    ('', ROOT / 'site/index.md', 'Ascended Browser | Local browser MCP for coding-agent QA', 'A local browser MCP for Codex CLI and Claude Code. Inspect console and network errors, collect screenshots and check interrupted actions.'),
    ('qa-quickstart', ROOT / 'docs/qa-quickstart.md', 'Browser MCP setup and QA guide for Codex CLI and Claude Code', 'Install Ascended Browser, connect your coding client and run a bounded QA check on a local app, with console logs, failed requests and evidence.'),
    ('interrupted-actions', ROOT / 'docs/interrupted-actions.md', 'Interrupted browser actions: recovery warnings and retry risks', 'Reproduce cancelled browser actions, SIGTERM and simulated SSH hangups. Learn what Ascended Browser 0.1.5 reports and why retries need fresh observations.'),
]

def build():
    OUT.mkdir(exist_ok=True)
    shutil.copyfile(ROOT / 'site/style.css', OUT / 'style.css')
    for slug, source, title, description in PAGES:
        text = source.read_text()
        text = re.sub(r'\.\./README\.md(#[\w-]+)?', lambda m: REPO + (m[1] or '#readme'), text)
        md = markdown.Markdown(extensions=['fenced_code', 'tables', 'toc'])
        body = md.convert(text)
        canonical = BASE + (slug + '/' if slug else '')
        prefix = '../' if slug else './'
        links = [('Overview', ''), ('QA setup', 'qa-quickstart'), ('Interruptions', 'interrupted-actions')]
        nav = ''.join(f'<a href="{prefix}{s + "/" if s else ""}"' + (' aria-current="page"' if s == slug else '') + f'>{name}</a>' for name, s in links)
        toc = f'<aside class="toc" aria-label="On this page"><strong>On this page</strong>{md.toc}</aside>' if slug else ''
        page = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><meta name="description" content="{html.escape(description)}"><link rel="canonical" href="{canonical}"><meta property="og:title" content="{html.escape(title)}"><meta property="og:description" content="{html.escape(description)}"><meta property="og:url" content="{canonical}"><meta property="og:type" content="website"><meta name="theme-color" content="#245b38"><link rel="stylesheet" href="{prefix}style.css"></head>
<body class="{'home' if not slug else 'guide'}"><a class="skip" href="#main">Skip to content</a><header class="top"><nav class="nav" aria-label="Main navigation"><a class="brand" href="{prefix}"><span aria-hidden="true">↗</span>Ascended Browser</a><div class="navlinks">{nav}<a href="{REPO}">GitHub ↗</a></div></nav></header><div class="wrap"><main id="main"><div class="eyebrow">Local browser MCP / coding-agent QA</div>{body}</main>{toc}</div><footer><div><span>Ascended Browser · Open source, MIT licensed</span><span><a href="{REPO}/issues">Report an issue</a> · No analytics or tracking scripts</span></div></footer></body></html>'''
        dest = OUT / slug
        dest.mkdir(exist_ok=True)
        (dest / 'index.html').write_text(page)
    root = ET.Element('urlset', xmlns='http://www.sitemaps.org/schemas/sitemap/0.9')
    for slug, *_ in PAGES:
        ET.SubElement(ET.SubElement(root, 'url'), 'loc').text = BASE + (slug + '/' if slug else '')
    ET.ElementTree(root).write(OUT / 'sitemap.xml', encoding='utf-8', xml_declaration=True)
    (OUT / '.nojekyll').touch()
    (OUT / '404.html').write_text('<!doctype html><html lang="en"><title>Page not found | Ascended Browser</title><h1>Page not found</h1><p><a href="/ascended-browser/">Return to Ascended Browser docs</a></p></html>')
    print(f'Built {len(PAGES)} pages in {OUT}')

if __name__ == '__main__':
    build()
