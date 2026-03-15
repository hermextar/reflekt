#!/usr/bin/env python3
"""
Reflekt API Server — lightweight backend for the journal app.
Uses Anthropic claude-haiku for fast, affordable AI responses.
"""
import json
import re
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import anthropic

client = anthropic.Anthropic()

# In-memory storage
entries = {}
messages = {}
entry_counter = [1]
message_counter = [1]

SYSTEM_PROMPT = """You are Reflekt — a compassionate, non-judgmental journaling companion helping people explore their thoughts.

Rules:
- NEVER give direct advice or tell the user what to do
- Ask ONE focused follow-up question — never multiple at once
- Reflect what you hear before asking (validate first)
- Keep responses brief: 2-3 sentences + one question
- Use warm, curious, open-ended language
- Help the user reach insights through gentle Socratic questioning
- Match the emotional weight of what they share — don't be artificially cheerful"""

def detect_mood(text):
    lower = text.lower()
    if re.search(r'anxious|worry|worried|nervous|scared|fear|panic', lower): return 'anxious'
    if re.search(r'angry|frustrated|mad|annoyed|furious|irritated', lower): return 'frustrated'
    if re.search(r'sad|depressed|low|hopeless|grief|lonely|miss', lower): return 'sad'
    if re.search(r'confused|lost|unsure|unclear|overwhelmed', lower): return 'confused'
    if re.search(r'happy|excited|good|great|grateful|thankful|proud', lower): return 'positive'
    if re.search(r'tired|exhausted|drained|burned|burnout', lower): return 'tired'
    return 'reflective'

def ai_respond(entry_content, conversation_history):
    msgs = [{"role": "user", "content": f"Here is my journal entry:\n\n{entry_content}"}]
    for m in conversation_history:
        msgs.append({"role": m["role"], "content": m["content"]})
    
    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=250,
            system=SYSTEM_PROMPT,
            messages=msgs
        )
        return response.content[0].text
    except Exception as e:
        print(f"AI error: {e}")
        return "Thank you for sharing that. What part of this feels most important for you to understand right now?"

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[{self.command}] {self.path} -> {args[0] if args else ''}")

    def send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def serve_static(self, path):
        try:
            fname = 'index.html' if path in ('/', '') else path.lstrip('/')
            with open(fname, 'rb') as f:
                content = f.read()
            ct = 'text/html' if fname.endswith('.html') else 'application/octet-stream'
            self.send_response(200)
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            # SPA fallback
            with open('index.html', 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)

    def do_GET(self):
        parsed = urlparse(self.path)
        p = parsed.path

        if p == '/api/entries':
            result = sorted(entries.values(), key=lambda x: x['createdAt'], reverse=True)
            return self.send_json(200, result)

        m = re.match(r'^/api/entries/(\d+)$', p)
        if m:
            eid = int(m.group(1))
            if eid not in entries:
                return self.send_json(404, {'error': 'Not found'})
            return self.send_json(200, entries[eid])

        m = re.match(r'^/api/entries/(\d+)/messages$', p)
        if m:
            eid = int(m.group(1))
            msgs = [v for v in messages.values() if v['entryId'] == eid]
            msgs.sort(key=lambda x: x['createdAt'])
            return self.send_json(200, msgs)

        self.serve_static(p)

    def do_POST(self):
        parsed = urlparse(self.path)
        p = parsed.path

        if p == '/api/entries':
            data = self.read_body()
            content = data.get('content', '').strip()
            if not content:
                return self.send_json(400, {'error': 'Content required'})
            
            eid = entry_counter[0]
            entry_counter[0] += 1
            entry = {
                'id': eid,
                'content': content,
                'title': data.get('title', ''),
                'mood': detect_mood(content),
                'createdAt': time.time()
            }
            entries[eid] = entry

            # Generate first AI message
            ai_text = ai_respond(content, [])
            mid = message_counter[0]
            message_counter[0] += 1
            messages[mid] = {
                'id': mid, 'entryId': eid,
                'role': 'assistant', 'content': ai_text,
                'createdAt': time.time()
            }

            return self.send_json(201, entry)

        m = re.match(r'^/api/entries/(\d+)/reply$', p)
        if m:
            eid = int(m.group(1))
            if eid not in entries:
                return self.send_json(404, {'error': 'Not found'})

            data = self.read_body()
            content = data.get('content', '').strip()
            if not content:
                return self.send_json(400, {'error': 'Content required'})

            # Save user message
            mid = message_counter[0]
            message_counter[0] += 1
            messages[mid] = {
                'id': mid, 'entryId': eid,
                'role': 'user', 'content': content,
                'createdAt': time.time()
            }

            # Build conversation history for AI
            history = [v for v in messages.values() if v['entryId'] == eid]
            history.sort(key=lambda x: x['createdAt'])

            ai_text = ai_respond(entries[eid]['content'], history)
            
            mid2 = message_counter[0]
            message_counter[0] += 1
            ai_msg = {
                'id': mid2, 'entryId': eid,
                'role': 'assistant', 'content': ai_text,
                'createdAt': time.time()
            }
            messages[mid2] = ai_msg
            return self.send_json(200, ai_msg)

        self.send_json(404, {'error': 'Not found'})

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    httpd = HTTPServer(('0.0.0.0', port), Handler)
    print(f"Reflekt server running on port {port}")
    httpd.serve_forever()
