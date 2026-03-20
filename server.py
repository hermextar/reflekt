import os
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

    # Run mood, reflection, and tags in parallel via separate Claude calls
    mood_response = anthropic_client.messages.create(
        model='claude-haiku-4-5',
        max_tokens=10,
        messages=[{'role': 'user', 'content': f'Classify the mood of this journal entry with ONE word from: anxious, frustrated, sad, confused, positive, tired, reflective. Entry: "{content}"'}]
    )
    mood = mood_response.content[0].text.strip().lower()
    # Sanitise to known values
    valid_moods = {'anxious', 'frustrated', 'sad', 'confused', 'positive', 'tired', 'reflective'}
    if mood not in valid_moods:
        mood = 'reflective'

    summary_response = anthropic_client.messages.create(
        model='claude-haiku-4-5',
        max_tokens=200,
        system='You are a warm, perceptive journalling companion. Respond ONLY with valid JSON, no markdown, no explanation.',
        messages=[{'role': 'user', 'content': f'''Read this journal entry and return JSON with exactly these keys:
- "reflection": one warm, specific sentence that names what the writer seems to be processing (not generic — reference their actual situation)
- "tags": array of 2-3 lowercase topic words (e.g. ["work", "relationships", "anxiety"])

Entry: "{content}"'''}]
    )
    try:
        raw = summary_response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        summary_data = json.loads(raw)
        reflection = summary_data.get('reflection', '')
        tags = summary_data.get('tags', [])
        if not isinstance(tags, list):
            tags = []
        tags = [str(t).lower().strip() for t in tags[:3]]
    except Exception:
        reflection = ''
        tags = []

    default_title = datetime.utcnow().strftime('%B %-d, %Y')
    entry = supabase.table('entries').insert({
        'user_id': user_id,
        'content': encrypt(content),
        'mood': mood,
        'title': default_title,
        'tags': tags
    }).execute()
    entry_id = entry.data[0]['id']

    # Re-fetch so created_at and all server-set fields are guaranteed present
    full_entry = supabase.table('entries').select('*').eq('id', entry_id).execute()
    result = full_entry.data[0]
    result['content'] = content  # return decrypted to frontend
    result['reflection'] = reflection
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


@app.route('/api/nudge', methods=['POST'])
@jwt_required()
def nudge():
    """
    Given the current entry text, return a single reflective follow-up question.
    Uses claude-haiku-3-5 for speed.
    """
    content = request.json.get('content', '').strip()
    if not content:
        return jsonify({'error': 'No content provided'}), 400

    response = anthropic_client.messages.create(
        model='claude-haiku-4-5',
        max_tokens=80,
        system=(
            'You are a quiet, perceptive journalling companion. '
            'Read the journal entry carefully and ask ONE short, specific reflective question '
            'that invites the writer to go deeper. '
            'The question must reference their actual situation — never generic. '
            'No preamble. No quotation marks. Just the question itself, ending with a question mark. '
            'Keep it under 20 words.'
        ),
        messages=[{'role': 'user', 'content': content}]
    )
    prompt = response.content[0].text.strip().strip('"').strip("'")
    return jsonify({'prompt': prompt})


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

    if not update_fields:
        return jsonify({'error': 'Nothing to update'}), 400

    supabase.table('entries').update(update_fields).eq('id', entry_id).execute()
    return jsonify({'id': entry_id, **{k: data.get(k) for k in update_fields if k != 'content'}})


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


@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def server_error(e):
    return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
