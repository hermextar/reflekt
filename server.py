import os
import json
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_bcrypt import Bcrypt
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
import anthropic
from supabase import create_client

app = Flask(__name__, static_folder='.')
CORS(app)
bcrypt = Bcrypt(app)

app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'change-this-in-production')
jwt = JWTManager(app)

anthropic_client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
supabase = create_client(os.environ.get('SUPABASE_URL'), os.environ.get('SUPABASE_KEY'))

# Serve frontend
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

# Auth routes
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

# Entries routes
@app.route('/api/entries', methods=['GET'])
@jwt_required()
def get_entries():
    user_id = get_jwt_identity()
    entries = supabase.table('entries').select('*').eq('user_id', user_id).order('created_at', desc=True).execute()
    return jsonify(entries.data)

@app.route('/api/entries', methods=['POST'])
@jwt_required()
def create_entry():
    user_id = get_jwt_identity()
    data = request.json
    content = data.get('content', '')
    
    # Get AI opening message
    ai_response = anthropic_client.messages.create(
        model='claude-haiku-4-5',
        max_tokens=300,
        messages=[{'role': 'user', 'content': f'You are a compassionate journalling companion. The user just wrote: "{content}". Respond with one warm, thoughtful question to help them explore deeper. Be brief.'}]
    )
    ai_message = ai_response.content[0].text

    # Detect mood
    mood_response = anthropic_client.messages.create(
        model='claude-haiku-4-5',
        max_tokens=10,
        messages=[{'role': 'user', 'content': f'Classify the mood of this journal entry with ONE word from: anxious, frustrated, sad, confused, positive, tired, reflective. Entry: "{content}"'}]
    )
    mood = mood_response.content[0].text.strip().lower()

    entry = supabase.table('entries').insert({'user_id': user_id, 'content': content, 'mood': mood}).execute()
    entry_id = entry.data[0]['id']

    supabase.table('messages').insert([
        {'entry_id': entry_id, 'role': 'assistant', 'content': ai_message}
    ]).execute()

    return jsonify(entry.data[0]), 201

@app.route('/api/entries/<entry_id>', methods=['GET'])
@jwt_required()
def get_entry(entry_id):
    user_id = get_jwt_identity()
    entry = supabase.table('entries').select('*').eq('id', entry_id).eq('user_id', user_id).execute()
    if not entry.data:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(entry.data[0])

@app.route('/api/entries/<entry_id>/messages', methods=['GET'])
@jwt_required()
def get_messages(entry_id):
    msgs = supabase.table('messages').select('*').eq('entry_id', entry_id).order('created_at').execute()
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
    messages = [{'role': m['role'], 'content': m['content']} for m in history.data]
    messages.append({'role': 'user', 'content': content})

    ai_response = anthropic_client.messages.create(
        model='claude-haiku-4-5',
        max_tokens=400,
        system='You are a compassionate journalling companion. Help the user explore their thoughts and feelings with warmth and curiosity. Ask questions, reflect back what you hear, but never give direct advice.',
        messages=messages
    )
    ai_message = ai_response.content[0].text

    supabase.table('messages').insert([
        {'entry_id': entry_id, 'role': 'user', 'content': content},
        {'entry_id': entry_id, 'role': 'assistant', 'content': ai_message}
    ]).execute()

    return jsonify({'role': 'assistant', 'content': ai_message})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
