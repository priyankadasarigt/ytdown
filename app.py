from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import yt_dlp
import os
import threading
import time
from datetime import datetime, timedelta
import uuid
import secrets
import boto3
from functools import wraps
import base64

app = Flask(__name__)

# Load from environment variables
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-in-production')
FRONTEND_URL = os.environ.get('FRONTEND_URL', 'https://theyt.pages.dev')

# R2 Credentials (get these from Cloudflare dashboard)
R2_ACCOUNT_ID = os.environ.get('R2_ACCOUNT_ID')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID')
R2_SECRET_ACCESS_KEY = os.environ.get('R2_SECRET_ACCESS_KEY')
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME')
R2_PUBLIC_URL = os.environ.get('R2_PUBLIC_URL', 'https://storage.shriya.workers.dev')

# ─── Cookie support ────────────────────────────────────────────────────────────
# Three ways to supply cookies (checked in order):
#
#   1. COOKIE_BASE64 env var  →  base64-encoded contents of a cookies.txt file
#      (easiest for Koyeb – paste the base64 string in env vars)
#
#   2. COOKIE_FILE env var  →  filesystem path to a cookies.txt already on disk
#      (useful if baked into a Docker image)
#
#   3. POST /api/upload_cookies at runtime  →  upload the file via the API
#      (protected by ADMIN_SECRET env var)
#
COOKIE_FILE_ENV = os.environ.get('COOKIE_FILE', '')
COOKIE_BASE64_ENV = os.environ.get('COOKIE_BASE64', '')
COOKIE_RUNTIME_PATH = '/tmp/yt_cookies.txt'


def _write_cookie_file_from_base64():
    """Decode COOKIE_BASE64 env var and write to COOKIE_RUNTIME_PATH at startup."""
    if COOKIE_BASE64_ENV:
        try:
            decoded = base64.b64decode(COOKIE_BASE64_ENV).decode('utf-8')
            with open(COOKIE_RUNTIME_PATH, 'w') as f:
                f.write(decoded)
            print(f"[cookies] Written cookie file from COOKIE_BASE64 -> {COOKIE_RUNTIME_PATH}")
        except Exception as e:
            print(f"[cookies] Failed to decode COOKIE_BASE64: {e}")


_write_cookie_file_from_base64()


def get_cookie_file():
    """Return the best available cookie file path, or None."""
    if os.path.exists(COOKIE_RUNTIME_PATH) and os.path.getsize(COOKIE_RUNTIME_PATH) > 10:
        return COOKIE_RUNTIME_PATH
    if COOKIE_FILE_ENV and os.path.exists(COOKIE_FILE_ENV):
        return COOKIE_FILE_ENV
    return None


# ─── Bot-bypass yt-dlp options ─────────────────────────────────────────────────
def get_ydl_base_opts():
    """
    Base yt-dlp options with bot-detection mitigations:
      - Prefer android/ios player clients (less aggressive bot checks)
      - Realistic browser User-Agent
      - Exponential back-off retries
      - Node.js runtime for JS extraction
      - Cookie file injected when available
    """
    opts = {
        'http_headers': {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/125.0.0.0 Safari/537.36'
            ),
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': (
                'text/html,application/xhtml+xml,application/xml;'
                'q=0.9,image/avif,image/webp,*/*;q=0.8'
            ),
        },
        # Android client avoids the harshest bot checks on YouTube
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
            }
        },
        'sleep_interval': 1,
        'max_sleep_interval': 3,
        'retries': 5,
        'fragment_retries': 5,
        'retry_sleep_functions': {'http': lambda n: 2 ** n},
        'cachedir': '/tmp/yt_dlp_cache',
    }


    cookie_file = get_cookie_file()
    if cookie_file:
        # Validate it looks like Netscape format
        try:
            with open(cookie_file, 'r') as f:
                first_line = f.readline().strip()
            if 'Netscape' in first_line or first_line.startswith('#'):
                opts['cookiefile'] = cookie_file
                size = os.path.getsize(cookie_file)
                print(f"[yt-dlp] Using cookie file: {cookie_file} ({size} bytes)")
            else:
                print(f"[yt-dlp] WARNING: Cookie file doesn't look like Netscape format! First line: {first_line[:80]}")
                print("[yt-dlp] Skipping cookie file — export cookies as Netscape format, not JSON")
        except Exception as e:
            print(f"[yt-dlp] Could not read cookie file: {e}")
    else:
        print("[yt-dlp] No cookie file found")

    return opts


# Configure CORS
CORS(app, resources={
    r"/*": {
        "origins": [FRONTEND_URL],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "X-Token"],
        "supports_credentials": False
    }
})

socketio = SocketIO(app,
                    cors_allowed_origins=[FRONTEND_URL],
                    async_mode='threading',
                    ping_timeout=60,
                    ping_interval=25)

DOWNLOAD_FOLDER = '/tmp/downloads'
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

last_activity_time = time.time()
downloads_db = {}
tokens_db = {}

# Initialize R2 client (S3-compatible)
s3_client = boto3.client(
    's3',
    endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name='auto'
)


def generate_token():
    return secrets.token_urlsafe(32)


def cleanup_expired_tokens():
    current_time = time.time()
    expired = [t for t, data in tokens_db.items()
               if current_time - data['created_at'] > 300]
    for token in expired:
        del tokens_db[token]


def cleanup_old_downloads():
    current_time = datetime.now()
    expired = [did for did, data in downloads_db.items()
               if current_time - data['timestamp'] > timedelta(hours=2)]
    for did in expired:
        del downloads_db[did]
    if expired:
        print(f"Cleaned up {len(expired)} old download metadata entries")


def update_activity():
    global last_activity_time
    last_activity_time = time.time()


def validate_token(token):
    if not token or token not in tokens_db:
        return False
    token_data = tokens_db[token]
    if token_data.get('used'):
        return False
    age = time.time() - token_data['created_at']
    if age > 300:
        del tokens_db[token]
        return False
    return True


def require_token(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method == 'OPTIONS':
            return f(*args, **kwargs)
        token = request.headers.get('X-Token')
        if not validate_token(token):
            return jsonify({'error': 'Invalid or expired token'}), 401
        return f(*args, **kwargs)
    return decorated_function


def upload_to_r2_direct(filepath, display_filename, session_id=None):
    """Upload file to Cloudflare R2 with progress reporting."""
    try:
        file_size = os.path.getsize(filepath)
        file_size_mb = file_size / (1024 * 1024)
        print(f"Direct R2 upload: {display_filename} ({file_size_mb:.2f} MB)")

        clean_filename = display_filename.replace(' ', '_').replace('|', '-')
        clean_filename = ''.join(c for c in clean_filename if c.isalnum() or c in '._-[]() ')
        clean_filename = clean_filename.replace(' ', '_')
        unique_filename = clean_filename

        # Duplicate check
        try:
            objects = s3_client.list_objects_v2(Bucket=R2_BUCKET_NAME)
            if 'Contents' in objects:
                for obj in objects['Contents']:
                    if obj['Key'] == unique_filename:
                        existing_size = obj['Size']
                        size_diff = abs(existing_size - file_size)
                        if size_diff <= (file_size * 0.01):
                            download_url = f"{R2_PUBLIC_URL}/download/{obj['Key']}"
                            print(f"Duplicate found: {obj['Key']}")
                            return {'download_url': download_url, 'filename': obj['Key'], 'duplicate': True}
                        else:
                            base_name = unique_filename.rsplit('.', 1)[0] if '.' in unique_filename else unique_filename
                            extension = '.' + unique_filename.rsplit('.', 1)[1] if '.' in unique_filename else ''
                            version = 1
                            while True:
                                versioned = f"{base_name}_v{version}{extension}"
                                if not any(o['Key'] == versioned for o in objects['Contents']):
                                    unique_filename = versioned
                                    break
                                version += 1
                                if version > 10:
                                    unique_filename = f"{base_name}_{int(time.time())}{extension}"
                                    break
        except Exception as e:
            print(f"Duplicate check failed: {e}")

        from boto3.s3.transfer import TransferConfig
        config = TransferConfig(
            multipart_threshold=1024 * 1024 * 50,
            max_concurrency=10,
            multipart_chunksize=1024 * 1024 * 10,
            use_threads=True
        )

        last_emit_time = [0]
        last_percent = [0]

        def progress_callback(bytes_transferred):
            current_time = time.time()
            progress = (bytes_transferred / file_size) * 100
            if (progress - last_percent[0] >= 1.0) or (current_time - last_emit_time[0] >= 1.0):
                uploaded_mb = bytes_transferred / (1024 * 1024)
                if session_id:
                    socketio.emit('upload_progress', {
                        'session_id': session_id,
                        'percent': f'{progress:.1f}%',
                        'uploaded': f'{uploaded_mb:.2f}',
                        'total': f'{file_size_mb:.2f}'
                    })
                print(f"Upload: {progress:.1f}% ({uploaded_mb:.2f}/{file_size_mb:.2f} MB)")
                last_emit_time[0] = current_time
                last_percent[0] = progress

        encoded_filename = base64.b64encode(display_filename.encode('utf-8')).decode('ascii')
        extra_args = {
            'Metadata': {
                'expiry-time': str(int(time.time() * 1000) + (2 * 60 * 60 * 1000)),
                'original-filename-base64': encoded_filename
            }
        }

        s3_client.upload_file(
            filepath, R2_BUCKET_NAME, unique_filename,
            ExtraArgs=extra_args, Callback=progress_callback, Config=config
        )

        download_url = f"{R2_PUBLIC_URL}/download/{unique_filename}"
        print(f"Upload completed: {unique_filename}")
        return {'download_url': download_url, 'filename': unique_filename, 'duplicate': False}

    except Exception as e:
        raise Exception(f"R2 upload error: {str(e)}")


# ─── Cookie management endpoints ───────────────────────────────────────────────
@app.route('/api/upload_cookies', methods=['POST', 'OPTIONS'])
def upload_cookies():
    """
    Upload a cookies.txt at runtime.
    Accepts:
      • multipart/form-data  →  field name 'cookies'
      • JSON  →  { "cookies_b64": "<base64 string>" }
    Protected by ADMIN_SECRET env var (send as X-Admin-Secret header).
    If ADMIN_SECRET is not set, the endpoint is unprotected (not recommended for prod).
    """
    if request.method == 'OPTIONS':
        return '', 204

    admin_secret = os.environ.get('ADMIN_SECRET', '')
    if admin_secret:
        if request.headers.get('X-Admin-Secret', '') != admin_secret:
            return jsonify({'error': 'Unauthorized'}), 401

    try:
        if 'cookies' in request.files:
            content = request.files['cookies'].read().decode('utf-8')
        elif request.is_json:
            b64 = request.get_json().get('cookies_b64', '')
            content = base64.b64decode(b64).decode('utf-8')
        else:
            return jsonify({'error': 'No cookie data provided'}), 400

        with open(COOKIE_RUNTIME_PATH, 'w') as f:
            f.write(content)

        lines = [l for l in content.splitlines() if l.strip() and not l.startswith('#')]
        return jsonify({
            'success': True,
            'message': f'Cookie file saved ({len(lines)} entries)',
            'path': COOKIE_RUNTIME_PATH
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/cookie_status', methods=['GET'])
def cookie_status():
    """Check whether a cookie file is active."""
    cookie_file = get_cookie_file()
    if cookie_file:
        return jsonify({'active': True, 'path': cookie_file,
                        'size_bytes': os.path.getsize(cookie_file)})
    return jsonify({'active': False})


# ─── Socket events ─────────────────────────────────────────────────────────────
@socketio.on('connect')
def handle_connect():
    update_activity()
    print(f"Client connected: {request.sid}")


@socketio.on('disconnect')
def handle_disconnect():
    try:
        print(f"Client disconnected: {request.sid}")
    except Exception:
        pass


@socketio.on_error()
def error_handler(e):
    print(f"Socket error (ignored): {str(e)}")
    return None


# ─── HTTP routes ───────────────────────────────────────────────────────────────
@app.route('/')
def root():
    return jsonify({'status': 'online'})


@app.route('/health')
def health():
    cookie_file = get_cookie_file()
    return jsonify({
        'status': 'online',
        'idle_minutes': round((time.time() - last_activity_time) / 60, 2),
        'cookies_active': cookie_file is not None
    })


@app.route('/api/request_token', methods=['POST', 'OPTIONS'])
def request_token():
    if request.method == 'OPTIONS':
        return '', 204

    try:
        update_activity()
        cleanup_expired_tokens()
        cleanup_old_downloads()

        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        current_time = time.time()
        ip_tokens = [t for t, data in tokens_db.items()
                     if data.get('ip') == client_ip and current_time - data['created_at'] < 3600]

        if len(ip_tokens) >= 10:
            return jsonify({'error': 'Rate limit exceeded. Try again later.'}), 429

        token = generate_token()
        tokens_db[token] = {'created_at': time.time(), 'used': False, 'ip': client_ip}
        print(f"Generated token for IP {client_ip}")
        return jsonify({'success': True, 'token': token, 'expires_in': 300})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/fetch_formats', methods=['POST', 'OPTIONS'])
@require_token
def fetch_formats():
    if request.method == 'OPTIONS':
        return '', 204

    try:
        update_activity()
        data = request.get_json()
        url = data.get('url')

        if not url:
            return jsonify({'error': 'URL is required'}), 400

        ydl_opts = get_ydl_base_opts()
        ydl_opts.update({'quiet': False, 'no_warnings': False, 'extract_flat': False})

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            title = info.get('title', 'video')

            video_formats = {}
            audio_formats = []

            for f in formats:
                if f.get('vcodec') != 'none' and f.get('acodec') == 'none':
                    height = f.get('height')
                    if height:
                        quality = f"{height}p"
                        filesize = f.get('filesize') or f.get('filesize_approx', 0)
                        vcodec = f.get('vcodec', 'unknown')
                        if quality not in video_formats or filesize > video_formats[quality].get('filesize', 0):
                            video_formats[quality] = {
                                'format_id': f.get('format_id'),
                                'quality': quality,
                                'ext': f.get('ext', 'mp4'),
                                'filesize': filesize,
                                'fps': f.get('fps'),
                                'vcodec': vcodec
                            }
                elif f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                    audio_formats.append({
                        'format_id': f.get('format_id'),
                        'ext': f.get('ext', 'webm'),
                        'abr': f.get('abr', 0),
                        'acodec': f.get('acodec'),
                        'filesize': f.get('filesize') or f.get('filesize_approx', 0)
                    })

            audio_formats.sort(key=lambda x: x.get('abr', 0), reverse=True)
            best_audio = audio_formats[0] if audio_formats else None
            video_list = sorted(video_formats.values(),
                                key=lambda x: int(x['quality'].replace('p', '')), reverse=True)

            return jsonify({
                'success': True,
                'title': title,
                'video_formats': video_list,
                'audio_formats': audio_formats,
                'best_audio': best_audio,
                'all_formats': formats
            })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@socketio.on('download_video')
def handle_download(data):
    token = data.get('token')
    if not validate_token(token):
        emit('download_error', {'error': 'Invalid or expired token'})
        return

    if token in tokens_db:
        tokens_db[token]['used'] = True

    update_activity()

    try:
        url = data.get('url')
        video_code = data.get('video_code')
        audio_code = data.get('audio_code')
        session_id = data.get('session_id')

        if not all([url, video_code, audio_code]):
            emit('download_error', {'error': 'Missing required parameters'})
            return

        download_id = str(uuid.uuid4())
        unique_prefix = f"[Xenvu.tech]_{download_id[:8]}_"

        def progress_hook(d):
            if d['status'] == 'downloading':
                try:
                    socketio.emit('download_progress', {
                        'session_id': session_id,
                        'percent': d.get('_percent_str', '0%').strip(),
                        'speed': d.get('_speed_str', 'N/A').strip(),
                        'eta': d.get('_eta_str', 'N/A').strip(),
                        'status': 'downloading'
                    })
                except Exception:
                    pass
            elif d['status'] == 'finished':
                socketio.emit('download_progress', {
                    'session_id': session_id,
                    'status': 'processing',
                    'message': 'Processing video...'
                })

        output_template = os.path.join(DOWNLOAD_FOLDER, f'{unique_prefix}%(title)s.%(ext)s')

        ydl_opts = get_ydl_base_opts()
        ydl_opts.update({
            'format': f'{video_code}+{audio_code}',
            'outtmpl': output_template,
            'progress_hooks': [progress_hook],
            'merge_output_format': 'mp4',
            'postprocessor_args': {
                'ffmpeg': ['-c:v', 'copy', '-c:a', 'copy']
            },
        })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            actual_file = None
            for file in os.listdir(DOWNLOAD_FOLDER):
                if file.startswith(unique_prefix):
                    actual_file = file
                    break

            if actual_file:
                filepath = os.path.join(DOWNLOAD_FOLDER, actual_file)
                display_filename = actual_file.replace(unique_prefix, '[Xenvu.tech] ')

                socketio.emit('download_progress', {
                    'session_id': session_id,
                    'status': 'uploading',
                    'message': 'Uploading to cloud storage...'
                })

                def upload_to_r2():
                    try:
                        result = upload_to_r2_direct(filepath, display_filename, session_id)
                        os.remove(filepath)

                        download_url = result.get('download_url')
                        print(f"{'Reused duplicate' if result.get('duplicate') else 'Uploaded'}: {actual_file}")

                        downloads_db[download_id] = {
                            'download_url': download_url,
                            'display_filename': display_filename,
                            'title': info['title'],
                            'timestamp': datetime.now()
                        }

                        socketio.emit('download_complete', {
                            'session_id': session_id,
                            'download_id': download_id,
                            'filename': display_filename,
                            'download_url': download_url
                        })
                        update_activity()

                    except Exception as upload_error:
                        print(f"R2 upload error: {upload_error}")
                        downloads_db[download_id] = {
                            'filename': actual_file,
                            'display_filename': display_filename,
                            'title': info['title'],
                            'timestamp': datetime.now(),
                            'fallback': True
                        }
                        socketio.emit('download_complete', {
                            'session_id': session_id,
                            'download_id': download_id,
                            'filename': display_filename,
                            'fallback': True
                        })

                upload_thread = threading.Thread(target=upload_to_r2)
                upload_thread.daemon = True
                upload_thread.start()
            else:
                emit('download_error', {'error': 'File not found after download'})

    except Exception as e:
        print(f"Download error: {str(e)}")
        try:
            socketio.emit('download_error', {'session_id': session_id, 'error': str(e)})
        except Exception:
            print(f"Could not emit error to client: {str(e)}")


@app.route('/api/download/<download_id>')
@require_token
def download_file(download_id):
    try:
        if download_id not in downloads_db:
            return jsonify({'error': 'Download not found'}), 404
        download_info = downloads_db[download_id]
        if 'download_url' in download_info:
            return jsonify({
                'success': True,
                'download_url': download_info['download_url'],
                'filename': download_info['display_filename']
            })
        return jsonify({'error': 'Download not available'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    socketio.run(app, host='0.0.0.0', port=port)
