"""Reproduce docs examples: .venv-qa/bin/python tests/qa_guides_demo.py"""
import asyncio, base64, http.server, json, os, re, tempfile, threading
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'site/assets'
HTML = '''<!doctype html><html lang="en"><meta charset="utf-8"><title>QA lab: deliberately broken shop</title>
<style>body{margin:0;background:#f3f5ef;color:#173426;font:18px/1.6 system-ui}main{max-width:1050px;margin:50px auto;padding:24px}h1{font-size:42px;line-height:1.2}.tag{color:#536952;font-size:14px}.card{background:white;padding:26px;border:1px solid #bfcdbb;border-radius:12px;margin:24px 0}button{background:#245b38;color:white;border:0;border-radius:7px;padding:14px 20px;font:inherit}#bad{display:flex;width:700px;gap:20px}.tile{width:220px;flex-shrink:0;border:1px solid #aaa;padding:14px}.notice{color:#9c241a}</style>
<main><p class="tag">ASCENDED BROWSER / LOCAL QA FIXTURE / INTENTIONALLY BROKEN</p><h1>Check the app, not just the code.</h1>
<section class="card"><h2>1. Broken request</h2><p>The button deliberately requests an unavailable API.</p><button onclick="fetch('/api/products').then(r=>{if(!r.ok){console.error('Product load failed: HTTP '+r.status);document.getElementById('error').textContent='Products could not load (HTTP '+r.status+').'}})">Load products</button><p id="error" class="notice" role="status"></p></section>
<section class="card"><h2>2. Narrow layout defect</h2><p>The fixed-width cards deliberately overflow a phone viewport.</p><div id="bad"><div class="tile">Starter plan</div><div class="tile">Team plan</div><div class="tile">Business plan</div></div></section>
<section class="card"><h2>3. Test session</h2><p>No password, real account or third-party login is used.</p><button onclick="location.href='/test-login'">Start disposable test session</button><p><a href="/protected">Open protected fixture</a></p></section></main></html>'''

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/products':
            self.send_response(503); self.end_headers(); self.wfile.write(b'Fixture: service unavailable'); return
        if self.path == '/test-login':
            self.send_response(302); self.send_header('Set-Cookie','qa_demo=allowed; Max-Age=3600; HttpOnly; SameSite=Lax'); self.send_header('Location','/protected'); self.end_headers(); return
        if self.path == '/protected':
            ok='qa_demo=allowed' in self.headers.get('Cookie','')
            self.send_response(200 if ok else 401); self.send_header('Content-Type','text/html'); self.end_headers()
            self.wfile.write(('<!doctype html><html lang="en"><title>Disposable protected fixture</title><body style="background:#f3f5ef;color:#173426;font:24px system-ui;padding:60px"><h1>'+('Test session active' if ok else 'Sign-in required')+'</h1><p>Local fixture only. No credentials used.</p></body></html>').encode()); return
        self.send_response(200); self.send_header('Content-Type','text/html'); self.end_headers(); self.wfile.write(HTML.encode())
    def log_message(self,*args): pass

def text(result): return '\n'.join(c.text for c in result.content if c.type=='text')
LOG=[]
async def call(s,name,args):
    r=await s.call_tool(name,args); t=text(r); LOG.append({'tool':name,'args':args,'result':t}); assert not r.isError,t; return r,t
async def shot(s,tab,name):
    r,_=await call(s,'browser_screenshot',{'tab_id':tab})
    image=next(c for c in r.content if c.type=='image'); (OUT/name).write_bytes(base64.b64decode(image.data))
async def run():
    OUT.mkdir(exist_ok=True)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler); threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}/'
    data=tempfile.mkdtemp(prefix='ab-guides-')
    params=StdioServerParameters(command=str(ROOT/'.venv-qa/bin/ascended-browser'),env={**os.environ,'ASCENDED_DATA_DIR':data})
    async with stdio_client(params) as (r,w):
      async with ClientSession(r,w) as s:
        await s.initialize(); _,t=await call(s,'browser_open',{'url':url}); tab=re.search(r'tab_[0-9a-f]{32}',t)[0]
        _,t=await call(s,'browser_observe',{'tab_id':tab,'query':'Load products'})
        # The observation carries the exact control ref in element records.
        refs=re.findall(r'"ref":\s*"(e[0-9a-f]+)"[^}]*?"role":\s*"button"',t)
        if not refs:
            refs=re.findall(r'"text":\s*"Load products"[^}]*?"control_refs":\s*\["(e[0-9a-f]+)"',t)
        assert refs,t
        await call(s,'browser_act',{'tab_id':tab,'action':{'kind':'click','ref':refs[0]}})
        _,console=await call(s,'browser_extract',{'tab_id':tab,'read':'console'})
        _,network=await call(s,'browser_extract',{'tab_id':tab,'read':'network','failed_only':True})
        assert '503' in console and '503' in network,(console,network)
        await call(s,'browser_viewport',{'tab_id':tab,'action':'set','preset':'desktop'}); await shot(s,tab,'qa-network.png')
        await call(s,'browser_act',{'tab_id':tab,'action':{'kind':'scroll','direction':'down','delta_y':430}})
        await shot(s,tab,'qa-layout-desktop.png')
        await call(s,'browser_viewport',{'tab_id':tab,'action':'set','preset':'phone'})
        await call(s,'browser_act',{'tab_id':tab,'action':{'kind':'scroll','direction':'down','delta_y':330}})
        await shot(s,tab,'qa-phone.png')
        await call(s,'browser_viewport',{'tab_id':tab,'action':'restore'})
        await call(s,'browser_act',{'tab_id':tab,'action':{'kind':'navigate','url':url+'protected'}})
        _,t=await call(s,'browser_extract',{'tab_id':tab}); assert 'Sign-in required' in t
        await call(s,'browser_act',{'tab_id':tab,'action':{'kind':'navigate','url':url+'test-login'}})
        _,t=await call(s,'browser_extract',{'tab_id':tab}); assert 'Test session active' in t
        await shot(s,tab,'qa-session.png')
    async with stdio_client(params) as (r,w):
      async with ClientSession(r,w) as s:
        await s.initialize(); _,t=await call(s,'browser_open',{'url':url+'protected'}); assert 'Test session active' in t,t
    (OUT/'qa-evidence.json').write_text(json.dumps(LOG,indent=2))
    server.shutdown(); print('PASS: console 503, network 503, phone screenshot, cookie reuse after MCP restart')
if __name__=='__main__': asyncio.run(run())
