import os
import re
import json
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_bcrypt import Bcrypt
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
import anthropic
from supabase import create_client
from cryptography.fernet import Fernet

app = Flask(__name__, static_folder='.')
CORS(app)
bcrypt = Bcrypt(app)

app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'change-this-in-production')
jwt = JWTManager(app)

anthropic_client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
supabase = create_client(os.environ.get('SUPABASE_URL'), os.environ.get('SUPABASE_KEY'))
fernet = Fernet(os.environ.get('ENCRYPTION_KEY').encode())

def encrypt(text):
    return fernet.encrypt(text.encode()).decode()

def decrypt(text):
    try:
        return fernet.decrypt(text.encode()).decode()
    except Exception:
        return text  # fallback for any unencrypted legacy entries

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    email = data.get('email')
    password = data.get('password')
    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400
    existing = supabase.table('users').select('id').eq('email', email).execute()
    if existing.data:
        return jsonify({'error': 'Email already registered'}), 409
    password_hash = bcrypt.generate_password_hash(password).decode('utf-8')
    user = supabase.table('users').insert({'email': email, 'password_hash': password_hash}).execute()
    token = create_access_token(identity=user.data[0]['id'])
    return jsonify({'token': token, 'email': email}), 201

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    email = data.get('email')
    password = data.get('password')
    user = supabase.table('users').select('*').eq('email', email).execute()
    if not user.data or not bcrypt.check_password_hash(user.data[0]['password_hash'], password):
        return jsonify({'error': 'Invalid credentials'}), 401
    token = create_access_token(identity=user.data[0]['id'])
    return jsonify({'token': token, 'email': email})

@app.route('/api/entries', methods=['GET'])
@jwt_required()
def get_entries():
    user_id = get_jwt_identity()
    entries = supabase.table('entries').select('*').eq('user_id', user_id).order('created_at', desc=True).execute()
    for entry in entries.data:
        entry['content'] = decrypt(entry['content'])
    return jsonify(entries.data)

@app.route('/api/entries', methods=['POST'])
@jwt_required()
def create_entry():
    user_id = get_jwt_identity()
    data = request.json
    content = data.get('content', '')

    # Single AI call: mood + reflection + tags together
    summary_response = anthropic_client.messages.create(
        model='claude-haiku-4-5',
        max_tokens=200,
        system='You are a journalling assistant. Respond ONLY with valid JSON, no markdown, no explanation.',
        messages=[{
            'role': 'user',
            'content': (
                f'Analyze this journal entry and return a JSON object with these exact keys:\n'
                f'- "mood": one word from [anxious, frustrated, sad, confused, positive, tired, reflective]\n'
                f'- "reflection": one warm sentence (under 20 words) reflecting what the person is processing\n'
                f'- "tags": array of 2-3 lowercase topic words (e.g. ["work", "family", "anxiety"])\n\n'
                f'Entry: "{content}"'
            )
        }]
    )

    try:
        summary_data = json.loads(summary_response.content[0].text.strip())
        mood_raw = summary_data.get('mood', 'reflective').strip().lower()
        mood_match = re.search(r'(anxious|frustrated|sad|confused|positive|tired|reflective)', mood_raw)
        mood = mood_match.group(1) if mood_match else 'reflective'
        reflection = summary_data.get('reflection', '')
        tags = summary_data.get('tags', [])
        if not isinstance(tags, list):
            tags = []
        tags = [str(t).lower().strip() for t in tags[:3]]
    except Exception:
        mood = 'reflective'
        reflection = ''
        tags = []

    # AI follow-up for chat thread (kept as before)
    ai_response = anthropic_client.messages.create(
        model='claude-haiku-4-5',
        max_tokens=300,
        system='You are Reflekt, a warm and emotionally intelligent journalling companion. Your role is to help the user explore their thoughts and feelings with curiosity and compassion. Ask one thoughtful follow-up question at a time. Reflect back what you hear. Validate their emotions without judgment. Never give direct advice or diagnoses. Keep responses concise — 2 to 4 sentences max.',
        messages=[{'role': 'user', 'content': f'The user just wrote their first journal entry: "{content}". Respond warmly and gently — acknowledge what they shared, then ask one thoughtful question to help them explore further. Be brief and human.'}]
    )
    ai_message = ai_response.content[0].text

    default_title = datetime.utcnow().strftime('%B %-d, %Y')

    insert_data = {
        'user_id': user_id,
        'content': encrypt(content),
        'mood': mood,
        'title': default_title,
    }
    # Save reflection and tags if columns exist
    if reflection:
        insert_data['ai_reflection'] = reflection
    if tags:
        insert_data['tags'] = tags
        insert_data['topic_tags'] = tags

    try:
        entry = supabase.table('entries').insert(insert_data).execute()
    except Exception:
        # Fallback without optional columns if they don't exist yet
        insert_data.pop('ai_reflection', None)
        insert_data.pop('tags', None)
        insert_data.pop('topic_tags', None)
        entry = supabase.table('entries').insert(insert_data).execute()
        reflection = ''
        tags = []

    entry_id = entry.data[0]['id']

    supabase.table('messages').insert([
        {'entry_id': entry_id, 'role': 'assistant', 'content': encrypt(ai_message)}
    ]).execute()

    # Re-fetch so created_at and all server-set fields are guaranteed present
    full_entry = supabase.table('entries').select('*').eq('id', entry_id).execute()
    result = full_entry.data[0]
    result['content'] = content  # return decrypted to frontend
    result['reflection'] = reflection
    result['tags'] = tags
    return jsonify(result), 201

@app.route('/api/entries/<entry_id>', methods=['GET'])
@jwt_required()
def get_entry(entry_id):
    user_id = get_jwt_identity()
    entry = supabase.table('entries').select('*').eq('id', entry_id).eq('user_id', user_id).execute()
    if not entry.data:
        return jsonify({'error': 'Not found'}), 404
    result = entry.data[0]
    result['content'] = decrypt(result['content'])
    return jsonify(result)

@app.route('/api/entries/<entry_id>/messages', methods=['GET'])
@jwt_required()
def get_messages(entry_id):
    msgs = supabase.table('messages').select('*').eq('entry_id', entry_id).order('created_at').execute()
    for m in msgs.data:
        m['content'] = decrypt(m['content'])
    return jsonify(msgs.data)

@app.route('/api/entries/<entry_id>/reply', methods=['POST'])
@jwt_required()
def reply(entry_id):
    user_id = get_jwt_identity()
    content = request.json.get('content', '')

    entry = supabase.table('entries').select('*').eq('id', entry_id).eq('user_id', user_id).execute()
    if not entry.data:
        return jsonify({'error': 'Not found'}), 404

    history = supabase.table('messages').select('*').eq('entry_id', entry_id).order('created_at').execute()
    messages = [{'role': m['role'], 'content': decrypt(m['content'])} for m in history.data]
    messages.append({'role': 'user', 'content': content})

    ai_response = anthropic_client.messages.create(
        model='claude-haiku-4-5',
        max_tokens=400,
        system='You are Reflekt, a warm and emotionally intelligent journalling companion. Your role is to help the user explore their thoughts and feelings with curiosity and compassion. Ask one thoughtful follow-up question at a time. Reflect back what you hear. Validate their emotions without judgment. Never give direct advice or diagnoses. Keep responses concise — 2 to 4 sentences max.',
        messages=messages
    )
    ai_message = ai_response.content[0].text

    supabase.table('messages').insert([
        {'entry_id': entry_id, 'role': 'user', 'content': encrypt(content)},
        {'entry_id': entry_id, 'role': 'assistant', 'content': encrypt(ai_message)}
    ]).execute()

    return jsonify({'role': 'assistant', 'content': ai_message})


@app.route('/api/entries/<entry_id>', methods=['PATCH'])
@jwt_required()
def update_entry(entry_id):
    user_id = get_jwt_identity()
    data = request.json
    # Verify ownership
    existing = supabase.table('entries').select('id').eq('id', entry_id).eq('user_id', user_id).execute()
    if not existing.data:
        return jsonify({'error': 'Not found'}), 404

    update_fields = {}
    if 'title' in data:
        new_title = data['title'].strip()
        if new_title:
            update_fields['title'] = new_title
    if 'content' in data:
        new_content = data['content']
        if new_content:
            update_fields['content'] = encrypt(new_content)
    if 'tags' in data:
        update_fields['tags'] = data['tags']
        update_fields['topic_tags'] = data['tags']

    if not update_fields:
        return jsonify({'error': 'Nothing to update'}), 400

    supabase.table('entries').update(update_fields).eq('id', entry_id).execute()
    resp = {'id': entry_id}
    for k in ('title', 'tags'):
        if k in data:
            resp[k] = data[k]
    return jsonify(resp)


@app.route('/api/nudge', methods=['POST'])
@jwt_required()
def nudge():
    content = request.json.get('content', '')
    if not content or len(content.split()) < 10:
        return jsonify({'error': 'Not enough content'}), 400

    response = anthropic_client.messages.create(
        model='claude-haiku-4-5',
        max_tokens=80,
        system=(
            'You are a gentle journalling companion. Read the journal entry and return ONE short, '
            'open-ended reflective question that helps the person go deeper. '
            'No preamble, no quotes, just the question itself. Max 20 words.'
        ),
        messages=[{'role': 'user', 'content': f'Journal entry so far:\n\n{content}'}]
    )
    prompt = response.content[0].text.strip().strip('"\'')
    return jsonify({'prompt': prompt})


@app.route('/api/entries/<entry_id>', methods=['DELETE'])
@jwt_required()
def delete_entry(entry_id):
    user_id = get_jwt_identity()
    existing = supabase.table('entries').select('id').eq('id', entry_id).eq('user_id', user_id).execute()
    if not existing.data:
        return jsonify({'error': 'Not found'}), 404
    supabase.table('messages').delete().eq('entry_id', entry_id).execute()
    supabase.table('entries').delete().eq('id', entry_id).execute()
    return jsonify({'success': True})


@app.route('/api/insights', methods=['POST'])
@jwt_required()
def insights():
    user_id = get_jwt_identity()
    print(f"[insights] fetching entries for user {user_id}")
    rows = supabase.table('entries').select('content,mood,created_at').eq('user_id', user_id).order('created_at', desc=True).limit(20).execute()

    if not rows.data:
        # No entries at all — return empty state gracefully
        return jsonify({
            'summary': "You haven't written any entries yet. Start journalling and come back!",
            'patterns': [],
            'growth': '',
            'question': 'What would you like to reflect on today?'
        })

    entries_text = []
    for i, e in enumerate(rows.data, 1):
        try:
            content = decrypt(e['content'])
        except Exception as dec_err:
            print(f"[insights] decrypt error for entry {i}: {dec_err}")
            content = '[entry could not be decrypted]'
        date_str = e.get('created_at', '')[:10] if e.get('created_at') else ''
        entries_text.append(f"Entry {i} ({date_str}, mood: {e.get('mood','?')}):\n{content}")
    combined = "\n\n---\n\n".join(entries_text)

    # Try preferred model, fall back to stable alternative
    models_to_try = ['claude-sonnet-4-5', 'claude-3-5-sonnet-20241022']
    response = None
    for model_name in models_to_try:
        try:
            print(f"[insights] trying model: {model_name}")
            response = anthropic_client.messages.create(
                model=model_name,
                max_tokens=900,
                system=(
                    """You are a compassionate journalling coach. Analyze the user's recent journal entries."""
                    'Return ONLY a valid JSON object (no markdown fences, no extra text) with exactly these keys: '
                    '"summary" (2-3 warm sentences about emotional state and themes), '
                    '"patterns" (array of 2-4 short observations under 20 words each), '
                    '"growth" (one sentence about positive change or resilience noticed), '
                    '"question" (one deep open-ended reflective question under 25 words). '
                    'Start your response with { and end with }.'
                ),
                messages=[{'role': 'user', 'content': f'Journal entries to analyze:\n\n{combined}'}]
            )
            print(f"[insights] model {model_name} responded OK")
            break
        except Exception as model_err:
            print(f"[insights] model {model_name} failed: {model_err}")
            response = None

    if response is None:
        return jsonify({'error': 'ai_unavailable'}), 503

    raw_text = response.content[0].text.strip()
    print(f"[insights] raw response (first 200 chars): {raw_text[:200]}")

    # Strip markdown code fences if present
    raw_text = re.sub(r'^```(?:json)?\s*', '', raw_text, flags=re.MULTILINE)
    raw_text = re.sub(r'\s*```$', '', raw_text, flags=re.MULTILINE)
    raw_text = raw_text.strip()

    # Extract JSON object if there's surrounding text
    json_match = re.search(r'\{[\s\S]*\}', raw_text)
    if json_match:
        raw_text = json_match.group(0)

    try:
        result = json.loads(raw_text)
        for k in ('summary', 'patterns', 'growth', 'question'):
            if k not in result:
                result[k] = '' if k != 'patterns' else []
        if not isinstance(result['patterns'], list):
            result['patterns'] = [str(result['patterns'])] if result['patterns'] else []
        print(f"[insights] parsed successfully: {list(result.keys())}")
        return jsonify(result)
    except json.JSONDecodeError as e:
        print(f"[insights] JSON parse error: {e}\nRaw: {raw_text[:500]}")
        return jsonify({'error': 'parse_error', 'detail': str(e)}), 500


@app.route('/api/account', methods=['DELETE'])
@jwt_required()
def delete_account():
    user_id = get_jwt_identity()
    # Get all entry IDs for this user
    entry_rows = supabase.table('entries').select('id').eq('user_id', user_id).execute()
    for entry in (entry_rows.data or []):
        supabase.table('messages').delete().eq('entry_id', entry['id']).execute()
    supabase.table('entries').delete().eq('user_id', user_id).execute()
    supabase.table('users').delete().eq('id', user_id).execute()
    return jsonify({'success': True})


@app.errorhandler(404)
@app.errorhandler(500)
@app.errorhandler(503)
def error_page(e):
    # Return JSON for API routes, error.html for everything else
    if request.path.startswith('/api/'):
        code = e.code if hasattr(e, 'code') else 500
        msgs = {404: 'Not found', 500: 'Internal server error', 503: 'Service unavailable'}
        return jsonify({'error': msgs.get(code, 'Error')}), code
    return send_from_directory('.', 'error.html'), (e.code if hasattr(e, 'code') else 500)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
