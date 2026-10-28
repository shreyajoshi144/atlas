"""Mock Atlas API + static server for browser tests. NOT the real backend: it returns canned JSON so the
redesigned frontend can be exercised end to end without Groq/Tavily/FastAPI. Usage: python mock_server.py"""
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
CALLS = []
def env(data): return {'success': True, 'data': data}
REPORTS = [
  {'report_id': 'r1a2b3c4d5', 'topic': 'State of solid-state battery commercialization', 'created_at': '2026-10-10T10:00:00Z', 'verification_verdict': 'verified', 'verification_score': 0.82, 'summary': 'Pilot lines exist but mass production remains two to three years out.'},
  {'report_id': 'r9z8y7x6w5', 'topic': 'Regulatory landscape for AI in healthcare', 'created_at': '2026-10-08T09:00:00Z', 'verification_verdict': 'partially_verified', 'verification_score': 0.58, 'summary': 'Rules differ sharply by region.'},
]
RUN = {'session': {'report_id': 'rNEW', 'total_execution_ms': 4200, 'total_tokens': 5100, 'estimated_cost_usd': 0.004},
  'report': {'report_id': 'rNEW', 'topic': 'How does retrieval-augmented generation work?', 'summary': 'RAG retrieves passages and conditions generation on them.', 'report_markdown': '# Overview\n\nRetrieval-augmented generation grounds answers in retrieved passages.\n\n## Key points\n- Retrieve first\n- Generate second\n', 'key_statistics': [], 'swot': None, 'executive_brief': None},
  'top_sources': [{'title': 'RAG survey', 'url': 'https://example.org/rag', 'domain': 'example.org', 'snippet': 'A survey of retrieval-augmented generation.', 'credibility_score': 0.8, 'relevance_score': 0.9}],
  'verification': {'overall_verification_score': 0.77, 'claims': []}}
def route(method, path, body):
    p = path.split('?')[0]
    if p.endswith('/auth/login'): return (200, env({'access_token': 'tok', 'user': {'name': 'Shreya Test', 'email': body.get('email')}})) if body.get('password') == 'correct-horse' else (401, {'detail': 'Invalid email or password.'})
    if p.endswith('/auth/register'): return (409, {'detail': 'An account with that email already exists.'}) if body.get('email') == 'taken@example.com' else (200, env({'access_token': 'tok', 'user': {'name': body.get('name'), 'email': body.get('email')}}))
    if p.endswith('/auth/me'): return 200, env({'name': 'Shreya Test', 'email': 'shreya@example.com'})
    if p.endswith('/auth/forgot-password'): return 200, {'success': True, 'message': 'If an account exists for that email, a reset link is on its way.', 'data': {'dev_reset_token': 'good'}}
    if p.endswith('/auth/reset-password'): return (200, {'success': True, 'data': None}) if body.get('token') == 'good' else (400, {'detail': 'Invalid or expired token.'})
    if p == '/health': return 200, {'status': 'ok', 'embeddings_available': True}
    if p.endswith('/history/reports'): return 200, env({'reports': REPORTS, 'total': len(REPORTS)})
    if '/history/report/' in p: return 200, env({'report': {'report_id': 'r1a2b3c4d5', 'topic': REPORTS[0]['topic'], 'summary': REPORTS[0]['summary'], 'report_markdown': '# Findings\n\nSolid-state cells are in pilot production.', 'created_at': REPORTS[0]['created_at']}, 'sources': RUN['top_sources'], 'verification': {'overall_verification_score': 0.82, 'claims': []}})
    if p.endswith('/research/run'): return 200, env(RUN)
    if p.endswith('/playground/conversations'): return 200, env({'items': [{'conversation_id': 'c1', 'title': 'Model chat', 'updated_at': '2026-10-11T10:00:00Z', 'message_count': 2}]})
    if p.endswith('/playground/models'): return 200, env({'models': [{'model': 'model-a', 'label': 'Model A'}, {'model': 'model-b', 'label': 'Model B'}, {'model': 'model-c', 'label': 'Model C'}], 'max_models': 3})
    if p.endswith('/workflows/templates'): return 200, env({'items': [{'template_id': 't1', 'name': 'Text cleanup', 'steps': [{'type': 'input_text', 'name': 'Input'}, {'type': 'normalize_whitespace', 'name': 'Normalize'}]}]})
    if p.endswith('/workflows') and method == 'GET': return 200, env({'items': [{'workflow_id': 'w1', 'name': 'My cleanup', 'definition': {'steps': [{'type': 'input_text', 'name': 'Input'}, {'type': 'normalize_whitespace', 'name': 'Normalize'}]}}]})
    if '/workflows/w1/runs' in p: return 200, env({'items': []})
    if p.endswith('/atlasbench/overview'): return 200, env({'suites': [{'title': 'Core suite', 'task_count': 3, 'suite_hash': 'abc123def4567890'}], 'configs': [], 'tasks': []})
    if p.endswith('/settings'): return 200, env({'app_name': 'Atlas', 'app_env': 'development', 'openai_model': 'llama-3.3-70b-versatile', 'tavily_max_results': 10, 'default_top_k_urls': 5, 'scrape_timeout_seconds': 20, 'scrape_max_retries': 2})
    if p.endswith('/knowledge-base'): return 200, env({'total_reports': 2, 'total_chunks': 48, 'chroma_collection_size': 48, 'recent_topics': ['Batteries', 'AI regulation'], 'report_ids': ['r1a2b3c4d5', 'r9z8y7x6w5']})
    return 200, env({'items': [], 'total': 0})
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*'); self.send_header('Access-Control-Allow-Headers', '*'); self.send_header('Access-Control-Allow-Methods', '*')
    def do_OPTIONS(self): self.send_response(204); self._cors(); self.end_headers()
    def _api(self, method):
        n = int(self.headers.get('Content-Length') or 0); raw = self.rfile.read(n) if n else b''
        try: body = json.loads(raw or b'{}')
        except Exception: body = {}
        CALLS.append(method + ' ' + self.path)
        status, payload = route(method, self.path, body)
        out = json.dumps(payload).encode(); self.send_response(status); self._cors(); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_POST(self): self._api('POST')
    def do_PUT(self): self._api('PUT')
    def do_DELETE(self): self._api('DELETE')
    def do_GET(self):
        p = urlparse(self.path).path
        if p.startswith('/api/') or p in ('/health', '/ready'): return self._api('GET')
        f = os.path.join(ROOT, p.lstrip('/') or 'index.html')
        if os.path.isdir(f): f = os.path.join(f, 'index.html')
        if not os.path.abspath(f).startswith(ROOT) or not os.path.isfile(f): self.send_response(404); self.end_headers(); return
        ct = {'.html': 'text/html', '.css': 'text/css', '.js': 'application/javascript', '.jpg': 'image/jpeg', '.svg': 'image/svg+xml'}.get(os.path.splitext(f)[1], 'application/octet-stream')
        data = open(f, 'rb').read(); self.send_response(200); self.send_header('Content-Type', ct + '; charset=utf-8' if ct.startswith('text') else ct); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
def serve(port=8000, static_port=5500):
    a = ThreadingHTTPServer(('127.0.0.1', port), H); b = ThreadingHTTPServer(('127.0.0.1', static_port), H)
    threading.Thread(target=a.serve_forever, daemon=True).start(); threading.Thread(target=b.serve_forever, daemon=True).start(); return a, b
if __name__ == '__main__':
    serve(); print('mock api :8000, static :5500'); threading.Event().wait()
