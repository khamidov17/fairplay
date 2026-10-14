#!/usr/bin/env python3
"""Local stand-in for the OpenAI-compatible LLM gateway (LiteLLM) used by judge.llm.

Development / testing only. It speaks ``POST /v1/chat/completions`` like LiteLLM, recognises
which feature is calling from the system prompt, and returns deterministic, well-formed
replies. A failure mode can be switched at runtime so error handling can be tested end to end.

Run:
    python3 scripts/ai_stub.py --port 8199 [--mode ok]

Point the site at it (dev, through dmoj/local_settings.py):
    CLASSROOM_LLM_API_BASE=http://127.0.0.1:8199  CLASSROOM_LLM_TIMEOUT=5

Control endpoints (plain HTTP, no auth; bind to 127.0.0.1 only):
    GET  /__mode                 current mode
    POST /__mode?mode=<mode>     switch mode (also ?delay=<seconds> for ``timeout``)
    GET  /__log                  JSON list of the requests received (feature, model, messages)
    POST /__reset                clear the log and go back to ``ok``

Modes:
    ok        well-formed replies (each rubric row scores 75% of its max, rounded to 0.5)
    error     HTTP 500 whose body carries a fake provider secret (must never reach students)
    ratelimit HTTP 429
    timeout   sleep ``delay`` seconds (default 30) before answering
    garbage   HTTP 200, but the message content is prose instead of the JSON asked for
    badjson   HTTP 200 with a body that is not JSON at all (broken gateway)
    empty     HTTP 200 with an empty message content
    leak      grading feedback that quotes the reference answer / accepted blank answers
    partial   grading reply with only the first rubric row, scored full marks
    full      every rubric row at full marks
"""
import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

FAKE_SECRET = 'sk-stub-PROVIDER-SECRET-0000'
MODES = ('ok', 'error', 'ratelimit', 'timeout', 'garbage', 'badjson', 'empty', 'leak', 'partial', 'full')

STATE = {'mode': 'ok', 'delay': 30.0, 'log': []}
LOCK = threading.Lock()


def classify(system):
    s = system or ''
    if 'short-answer grader' in s:
        return 'grade_short_answer'
    if 'fill-in-the-blank grader' in s:
        return 'grade_fill_blank'
    if 'code grader' in s:
        return 'grade_code'
    if 'programming tutor' in s:
        return 'hint'
    if 'classify educational/problem difficulty' in s:
        return 'difficulty'
    if 'by the algorithm or solution technique' in s:
        return 'tags'
    if 'decompose educational problems' in s:
        return 'decompose'
    if 'format educational questions' in s:
        return 'question_import'
    return 'unknown'


_ROW_RE = re.compile(r'^\s+(\d+)\.\s+(.*?):\s+max\s+([0-9.]+)\s+pts', re.M)
_FB_ROW_RE = re.compile(r'^\s+(\d+)\.\s+(.*?)\s+\(max\s+([0-9.]+)\s+pts\):\s*(.*)$', re.M)


def _rows(kind, user):
    if kind == 'grade_fill_blank':
        block = user.split('<reference_answers>', 1)[-1].split('</reference_answers>', 1)[0]
        return [(m.group(2), float(m.group(3)), m.group(4)) for m in _FB_ROW_RE.finditer(block)]
    table = user.split('Grading Table:', 1)[-1]
    return [(m.group(2), float(m.group(3)), '') for m in _ROW_RE.finditer(table)]


def _between(text, tag):
    if f'<{tag}>' not in text:
        return ''
    return text.split(f'<{tag}>', 1)[1].split(f'</{tag}>', 1)[0].strip()


def grading_reply(kind, user, mode):
    rows = _rows(kind, user) or [('Overall', 10.0, '')]
    reference = _between(user, 'reference_answer')
    items = []
    for name, max_pts, accepted in rows:
        if mode == 'full':
            score = max_pts
        else:
            score = round(max_pts * 0.75 * 2) / 2
        feedback = f'Stub feedback for {name}: reasoning is incomplete.'
        if mode == 'leak':
            leaked = reference or accepted
            feedback = f'The correct answer is: {leaked}'
        items.append({'name': name, 'score': score, 'feedback': feedback})
    if mode == 'partial':
        items = items[:1]
        items[0]['score'] = rows[0][1]
    return json.dumps({'sub_scores': items, 'total_score': sum(i['score'] for i in items)})


def content_for(kind, system, user, mode):
    if mode == 'garbage':
        return 'I think the student did a great job overall! Ten out of ten.'
    if mode == 'empty':
        return ''
    if kind.startswith('grade_'):
        return grading_reply(kind, user, mode)
    if kind == 'hint':
        if mode == 'leak':
            return 'Hint: the hidden test case 3 expects 42; your verdict was Wrong Answer.'
        return 'Stub hint: re-check how your loop handles the boundary values.'
    if kind == 'difficulty':
        return 'medium'
    if kind == 'tags':
        cands = system.split('Candidate labels:', 1)[-1].split(',')
        return cands[0].strip() if cands and cands[0].strip() else 'General'
    if kind == 'decompose':
        total = 1.0
        m = re.search(r'Total points:\s*([0-9.]+)', user)
        if m:
            total = float(m.group(1))
        half = round(total / 2 * 2) / 2
        return json.dumps({'sub_questions': [
            {'name': 'Concept', 'max_points': half},
            {'name': 'Application', 'max_points': total - half},
        ]})
    if kind == 'question_import':
        desc = user.split('Description:\n', 1)[-1].split('\n\nAnswer key', 1)[0]
        return json.dumps({'description': desc, 'options': [], 'option_block': '', 'blanks': []})
    return 'stub reply'


class Handler(BaseHTTPRequestHandler):
    server_version = 'ai-stub/1'

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code, body, ctype='application/json'):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == '/__mode':
            return self._send(200, json.dumps({'mode': STATE['mode'], 'delay': STATE['delay']}))
        if url.path == '/__log':
            with LOCK:
                return self._send(200, json.dumps(STATE['log']))
        if url.path.rstrip('/') in ('/v1/models', '/models'):
            return self._send(200, json.dumps({'object': 'list', 'data': [{'id': 'stub-model', 'object': 'model'}]}))
        return self._send(404, '{"error": "not found"}')

    def do_POST(self):
        url = urlparse(self.path)
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        if url.path == '/__mode':
            qs = parse_qs(url.query)
            mode = (qs.get('mode') or ['ok'])[0]
            if mode not in MODES:
                return self._send(400, json.dumps({'error': f'unknown mode {mode}', 'modes': MODES}))
            STATE['mode'] = mode
            if 'delay' in qs:
                STATE['delay'] = float(qs['delay'][0])
            return self._send(200, json.dumps({'mode': mode, 'delay': STATE['delay']}))
        if url.path == '/__reset':
            with LOCK:
                STATE['log'].clear()
            STATE['mode'] = 'ok'
            return self._send(200, '{"ok": true}')
        if url.path.rstrip('/') not in ('/v1/chat/completions', '/chat/completions'):
            return self._send(404, '{"error": "not found"}')

        try:
            req = json.loads(raw or b'{}')
        except ValueError:
            return self._send(400, '{"error": "bad request"}')
        messages = req.get('messages') or []
        system = next((m.get('content', '') for m in messages if m.get('role') == 'system'), '')
        user = next((m.get('content', '') for m in messages if m.get('role') == 'user'), '')
        kind = classify(system)
        mode = STATE['mode']
        with LOCK:
            STATE['log'].append({
                'time': time.time(), 'feature': kind, 'mode': mode, 'model': req.get('model'),
                'response_format': req.get('response_format'),
                'has_auth': bool(self.headers.get('Authorization')),
                'messages': messages,
            })

        if mode == 'error':
            return self._send(500, json.dumps({'error': {
                'message': f'Upstream provider failure (key {FAKE_SECRET}) at http://10.0.0.5:4000/internal',
                'type': 'internal_error', 'code': 500}}))
        if mode == 'ratelimit':
            return self._send(429, json.dumps({'error': {'message': 'Rate limit exceeded', 'code': 429}}))
        if mode == 'timeout':
            time.sleep(STATE['delay'])
        if mode == 'badjson':
            return self._send(200, '<html>502 Bad Gateway from upstream</html>', ctype='text/html')

        content = content_for(kind, system, user, mode)
        body = {
            'id': f'chatcmpl-stub-{len(STATE["log"])}',
            'object': 'chat.completion',
            'created': int(time.time()),
            'model': req.get('model') or 'stub-model',
            'choices': [{
                'index': 0,
                'message': {'role': 'assistant', 'content': content},
                'finish_reason': 'stop',
            }],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120},
        }
        return self._send(200, json.dumps(body))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8199)
    parser.add_argument('--mode', default='ok', choices=MODES)
    parser.add_argument('--delay', type=float, default=30.0)
    args = parser.parse_args()
    STATE['mode'] = args.mode
    STATE['delay'] = args.delay
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'ai_stub listening on http://{args.host}:{args.port} mode={args.mode}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
