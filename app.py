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

DOWNLOAD_FOLDER = 'downloads'
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
    """Generate a cryptographically secure token"""
    return secrets.token_urlsafe(32)

def cleanup_expired_tokens():
    """Remove expired tokens"""
    current_time = time.time()
    expired = [t for t, data in tokens_db.items() 
               if current_time - data['created_at'] > 300]
    for token in expired:
        del tokens_db[token]

def cleanup_old_downloads():
    """Remove download metadata older than 2 hours"""
    current_time = datetime.now()
    expired = [download_id for download_id, data in downloads_db.items() 
               if current_time - data['timestamp'] > timedelta(hours=2)]
    for download_id in expired:
        del downloads_db[download_id]
    if expired:
        print(f"Cleaned up {len(expired)} old download metadata entries")

def update_activity():
    """Update last activity time"""
    global last_activity_time
    last_activity_time = time.time()

def validate_token(token):
    """Check if token is valid and not expired"""
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
    from functools import wraps
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
    """Upload file directly to R2 using boto3 (supports files up to 5GB)"""
    try:
        file_size = os.path.getsize(filepath)
        file_size_mb = file_size / (1024 * 1024)
        
        print(f"Direct R2 upload: {display_filename} ({file_size_mb:.2f} MB)")
        
        # Clean filename for R2 - NO TIMESTAMP PREFIX
        clean_filename = display_filename.replace(' ', '_').replace('|', '-')
        # Remove any other problematic characters
        clean_filename = ''.join(c for c in clean_filename if c.isalnum() or c in '._-[]() ')
        clean_filename = clean_filename.replace(' ', '_')
        
        # Use just the clean filename, no timestamp
        unique_filename = clean_filename
        
        # Check for duplicates
        try:
            objects = s3_client.list_objects_v2(Bucket=R2_BUCKET_NAME)
            if 'Contents' in objects:
                for obj in objects['Contents']:
                    # Check if file with same name exists
                    if obj['Key'] == unique_filename:
                        # Check file size similarity (within 1%)
                        existing_size = obj['Size']
                        size_diff = abs(existing_size - file_size)
                        if size_diff <= (file_size * 0.01):
                            download_url = f"{R2_PUBLIC_URL}/download/{obj['Key']}"
                            print(f"Duplicate found (exact match): {obj['Key']}")
                            return {
                                'download_url': download_url,
                                'filename': obj['Key'],
                                'duplicate': True
                            }
                        else:
                            # Same name but different size - add version number
                            base_name = unique_filename.rsplit('.', 1)[0] if '.' in unique_filename else unique_filename
                            extension = '.' + unique_filename.rsplit('.', 1)[1] if '.' in unique_filename else ''
                            version = 1
                            while True:
                                versioned_filename = f"{base_name}_v{version}{extension}"
                                # Check if this version exists
                                version_exists = any(o['Key'] == versioned_filename for o in objects['Contents'])
                                if not version_exists:
                                    unique_filename = versioned_filename
                                    print(f"File exists with different size, using version: {unique_filename}")
                                    break
                                version += 1
                                if version > 10:  # Safety limit
                                    unique_filename = f"{base_name}_{int(time.time())}{extension}"
                                    break
        except Exception as e:
            print(f"Duplicate check failed: {e}")
        
        # Upload with progress callback using multipart for better progress tracking
        from boto3.s3.transfer import TransferConfig
        
        # Configure multipart upload for better progress tracking
        config = TransferConfig(
            multipart_threshold=1024 * 1024 * 50,  # 50MB
            max_concurrency=10,
            multipart_chunksize=1024 * 1024 * 10,  # 10MB chunks
            use_threads=True
        )
        
        last_emit_time = [0]
        last_percent = [0]
        
        def progress_callback(bytes_transferred):
            current_time = time.time()
            progress = (bytes_transferred / file_size) * 100
            
            # Emit if: 1% progress change OR 1 second passed
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
        
        # Upload file to R2
        # Encode filename to base64 to handle non-ASCII characters
        import base64
        encoded_filename = base64.b64encode(display_filename.encode('utf-8')).decode('ascii')
        
        extra_args = {
            'Metadata': {
                'expiry-time': str(int(time.time() * 1000) + (2 * 60 * 60 * 1000)),
                'original-filename-base64': encoded_filename
            }
        }
        
        s3_client.upload_file(
            filepath,
            R2_BUCKET_NAME,
            unique_filename,
            ExtraArgs=extra_args,
            Callback=progress_callback,
            Config=config
        )
        
        download_url = f"{R2_PUBLIC_URL}/download/{unique_filename}"
        
        print(f"Upload completed: {unique_filename}")
        
        return {
            'download_url': download_url,
            'filename': unique_filename,
            'duplicate': False
        }
        
    except Exception as e:
        raise Exception(f"R2 upload error: {str(e)}")

@socketio.on('connect')
def handle_connect():
    """Handle socket connection"""
    update_activity()
    print(f"Client connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    """Handle socket disconnection"""
    try:
        print(f"Client disconnected: {request.sid}")
    except:
        pass

@socketio.on_error()
def error_handler(e):
    """Handle socket errors"""
    print(f"Socket error (ignored): {str(e)}")
    return None

@app.route('/')
def root():
    return jsonify({'status': 'online'})

@app.route('/health')
def health():
    """Public health check"""
    return jsonify({
        'status': 'online',
        'idle_minutes': round((time.time() - last_activity_time) / 60, 2)
    })

@app.route('/api/request_token', methods=['POST', 'OPTIONS'])
def request_token():
    """Generate a one-time token"""
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
        tokens_db[token] = {
            'created_at': time.time(),
            'used': False,
            'ip': client_ip
        }
        
        print(f"Generated token for IP {client_ip}")
        
        return jsonify({
            'success': True,
            'token': token,
            'expires_in': 300
        })
        
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

        ydl_opts = {
            'quiet': False,
            'no_warnings': False,
            'extract_flat': False,
        }

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
                              key=lambda x: int(x['quality'].replace('p', '')), 
                              reverse=True)
            
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
                    percent = d.get('_percent_str', '0%').strip()
                    speed = d.get('_speed_str', 'N/A').strip()
                    eta = d.get('_eta_str', 'N/A').strip()
                    
                    socketio.emit('download_progress', {
                        'session_id': session_id,
                        'percent': percent,
                        'speed': speed,
                        'eta': eta,
                        'status': 'downloading'
                    })
                except:
                    pass
                    
            elif d['status'] == 'finished':
                socketio.emit('download_progress', {
                    'session_id': session_id,
                    'status': 'processing',
                    'message': 'Processing video...'
                })
        
        output_template = os.path.join(DOWNLOAD_FOLDER, f'{unique_prefix}%(title)s.%(ext)s')
        
        ydl_opts = {
            'format': f'{video_code}+{audio_code}',
            'outtmpl': output_template,
            'progress_hooks': [progress_hook],
            'merge_output_format': 'mp4',
            'postprocessor_args': {
                'ffmpeg': ['-c:v', 'copy', '-c:a', 'copy']
            },
        }
        
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
                        
                        # Delete local file after upload
                        os.remove(filepath)
                        
                        download_url = result.get('download_url')
                        is_duplicate = result.get('duplicate', False)
                        
                        if is_duplicate:
                            print(f"Found duplicate, reusing existing file")
                        else:
                            print(f"Uploaded to R2 and deleted local file: {actual_file}")
                        
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
            socketio.emit('download_error', {
                'session_id': session_id,
                'error': str(e)
            })
        except:
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
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
