from flask import Flask, request, Response, jsonify, send_file
from flask_socketio import SocketIO, emit
import requests
import binascii
from datetime import datetime
import json
import os
import time
import threading
import io
import zipfile

app = Flask(__name__)
app.config['SECRET_KEY'] = 'exu-proxy-capture-secret-key-2024'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

TARGET = "https://clientbp.ggpolarbear.com"
LOG_FILE = "capture.txt"
LOG_JSON = "capture_logs.json"

# Store logs in memory
captured_logs = []
MAX_LOGS = 500

def log_entry(endpoint, method, headers, req_data, resp_data, status, duration=0):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    
    # Create log entry with ALL formats
    entry = {
        "id": int(time.time() * 1000),
        "timestamp": ts,
        "endpoint": endpoint,
        "method": method,
        "status": status,
        "request_headers": dict(headers),
        # Store raw hex (what you need)
        "request_body_hex": binascii.hexlify(req_data).decode() if req_data else None,
        # Store as text (may be garbage for binary)
        "request_body_text": req_data.decode('utf-8', errors='ignore') if req_data else None,
        # Store as base64 for complete preservation
        "request_body_base64": binascii.b2a_base64(req_data).decode().strip() if req_data else None,
        "response_body_hex": binascii.hexlify(resp_data).decode() if resp_data else None,
        "response_body_text": resp_data.decode('utf-8', errors='ignore') if resp_data else None,
        "response_body_base64": binascii.b2a_base64(resp_data).decode().strip() if resp_data else None,
        "request_size": len(req_data) if req_data else 0,
        "response_size": len(resp_data) if resp_data else 0,
        "duration": round(duration * 1000, 2)
    }
    
    # Store in memory
    captured_logs.insert(0, entry)
    if len(captured_logs) > MAX_LOGS:
        captured_logs.pop()
    
    # Emit to WebSocket clients
    socketio.emit('new_log', entry)
    
    # Save to JSON
    try:
        existing = []
        if os.path.exists(LOG_JSON):
            with open(LOG_JSON, 'r', encoding='utf-8') as f:
                existing = json.load(f)
        existing.insert(0, entry)
        if len(existing) > MAX_LOGS:
            existing = existing[:MAX_LOGS]
        with open(LOG_JSON, 'w', encoding='utf-8') as f:
            json.dump(existing, f, indent=2)
    except Exception as e:
        print(f"Error saving to JSON: {e}")
    
    # Append to text log with hex
    hdr = "\n".join(f"{k}: {v}" for k, v in headers.items() if k.lower() != "host")
    text_entry = f"""
{'='*60}
[{ts}] {method} {endpoint} -> {status} ({round(duration*1000,2)}ms)
{'='*60}
REQUEST HEADERS:
{hdr}

REQUEST BODY (hex):
{binascii.hexlify(req_data).decode() if req_data else '(empty)'}

REQUEST BODY (base64):
{binascii.b2a_base64(req_data).decode().strip() if req_data else '(empty)'}

RESPONSE BODY (hex):
{binascii.hexlify(resp_data).decode() if resp_data else '(empty)'}

RESPONSE BODY (base64):
{binascii.b2a_base64(resp_data).decode().strip() if resp_data else '(empty)'}
{'='*60}
"""
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(text_entry)

# ===== Serve Dashboard =====
@app.route('/')
def index():
    return send_file('index.html')

# ===== API Routes =====
@app.route('/api/logs')
def get_logs():
    try:
        if os.path.exists(LOG_JSON):
            with open(LOG_JSON, 'r', encoding='utf-8') as f:
                logs = json.load(f)
            return jsonify({"status": "success", "data": logs, "count": len(logs)})
    except Exception as e:
        print(f"Error reading logs: {e}")
    return jsonify({"status": "success", "data": captured_logs, "count": len(captured_logs)})

@app.route('/api/logs/clear', methods=['POST'])
def clear_logs():
    global captured_logs
    captured_logs = []
    if os.path.exists(LOG_JSON):
        os.remove(LOG_JSON)
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)
    socketio.emit('logs_cleared')
    return jsonify({"status": "success", "message": "All logs cleared"})

@app.route('/api/stats')
def get_stats():
    total = len(captured_logs)
    success = len([l for l in captured_logs if 200 <= l['status'] < 300])
    redirect = len([l for l in captured_logs if 300 <= l['status'] < 400])
    error = len([l for l in captured_logs if l['status'] >= 400])
    total_size = sum([l.get('response_size', 0) for l in captured_logs])
    avg_duration = 0
    if total > 0:
        avg_duration = sum([l.get('duration', 0) for l in captured_logs]) / total
    return jsonify({
        "total": total,
        "success": success,
        "redirect": redirect,
        "error": error,
        "total_size": total_size,
        "avg_duration": round(avg_duration, 2)
    })

# ===== Raw Data Endpoints =====
@app.route('/api/raw/<int:log_id>')
def get_raw_body(log_id):
    """Get raw hex body for a specific log entry"""
    for log in captured_logs:
        if log['id'] == log_id:
            return jsonify({
                "id": log['id'],
                "method": log['method'],
                "endpoint": log['endpoint'],
                "timestamp": log['timestamp'],
                "status": log['status'],
                "request_hex": log.get('request_body_hex', ''),
                "request_base64": log.get('request_body_base64', ''),
                "request_size": log.get('request_size', 0),
                "response_hex": log.get('response_body_hex', ''),
                "response_base64": log.get('response_body_base64', ''),
                "response_size": log.get('response_size', 0)
            })
    return jsonify({"error": "Log not found"}), 404

@app.route('/api/raw/latest')
def get_latest_raw():
    """Get raw hex of the latest request"""
    if captured_logs:
        log = captured_logs[0]
        return jsonify({
            "id": log['id'],
            "method": log['method'],
            "endpoint": log['endpoint'],
            "timestamp": log['timestamp'],
            "status": log['status'],
            "request_hex": log.get('request_body_hex', ''),
            "request_base64": log.get('request_body_base64', ''),
            "request_size": log.get('request_size', 0),
            "response_hex": log.get('response_body_hex', ''),
            "response_base64": log.get('response_body_base64', ''),
            "response_size": log.get('response_size', 0)
        })
    return jsonify({"error": "No logs available"}), 404

@app.route('/api/raw/export/<int:log_id>')
def export_raw_body(log_id):
    """Export raw body as a downloadable file"""
    for log in captured_logs:
        if log['id'] == log_id:
            hex_data = log.get('request_body_hex', '')
            if hex_data:
                # Convert hex to bytes
                raw_bytes = bytes.fromhex(hex_data)
                return Response(
                    raw_bytes,
                    mimetype='application/octet-stream',
                    headers={'Content-Disposition': f'attachment;filename=request_{log_id}.bin'}
                )
            return jsonify({"error": "No body data"}), 404
    return jsonify({"error": "Log not found"}), 404

# ===== Download Endpoints =====
@app.route('/download/json')
def download_json():
    """Download logs as JSON file"""
    try:
        if os.path.exists(LOG_JSON):
            return send_file(
                LOG_JSON, 
                as_attachment=True, 
                download_name='capture_logs.json',
                mimetype='application/json'
            )
        else:
            data = json.dumps(captured_logs, indent=2)
            return Response(
                data,
                mimetype='application/json',
                headers={'Content-Disposition': 'attachment;filename=capture_logs.json'}
            )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/download/txt')
def download_txt():
    """Download logs as text file"""
    try:
        if os.path.exists(LOG_FILE):
            return send_file(
                LOG_FILE, 
                as_attachment=True, 
                download_name='capture.txt',
                mimetype='text/plain'
            )
        else:
            text_data = ""
            for log in captured_logs:
                text_data += f"""
{'='*60}
[{log['timestamp']}] {log['method']} {log['endpoint']} -> {log['status']} ({log['duration']}ms)
{'='*60}
REQUEST HEADERS:
{json.dumps(log['request_headers'], indent=2)}

REQUEST BODY (hex):
{log['request_body_hex'] or '(empty)'}

REQUEST BODY (base64):
{log['request_body_base64'] or '(empty)'}

RESPONSE BODY (hex):
{log['response_body_hex'] or '(empty)'}

RESPONSE BODY (base64):
{log['response_body_base64'] or '(empty)'}
{'='*60}
"""
            return Response(
                text_data,
                mimetype='text/plain',
                headers={'Content-Disposition': 'attachment;filename=capture.txt'}
            )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/download/all')
def download_all():
    """Download both files as a zip"""
    try:
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            if os.path.exists(LOG_JSON):
                zip_file.write(LOG_JSON, 'capture_logs.json')
            else:
                data = json.dumps(captured_logs, indent=2)
                zip_file.writestr('capture_logs.json', data)
            
            if os.path.exists(LOG_FILE):
                zip_file.write(LOG_FILE, 'capture.txt')
            else:
                text_data = ""
                for log in captured_logs:
                    text_data += f"""
{'='*60}
[{log['timestamp']}] {log['method']} {log['endpoint']} -> {log['status']} ({log['duration']}ms)
{'='*60}
REQUEST HEADERS:
{json.dumps(log['request_headers'], indent=2)}

REQUEST BODY (hex):
{log['request_body_hex'] or '(empty)'}

REQUEST BODY (base64):
{log['request_body_base64'] or '(empty)'}

RESPONSE BODY (hex):
{log['response_body_hex'] or '(empty)'}

RESPONSE BODY (base64):
{log['response_body_base64'] or '(empty)'}
{'='*60}
"""
                zip_file.writestr('capture.txt', text_data)
        
        zip_buffer.seek(0)
        return send_file(
            zip_buffer,
            as_attachment=True,
            download_name='logs.zip',
            mimetype='application/zip'
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ===== Proxy Endpoint =====
@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD'])
@app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD'])
def proxy(path):
    endpoint = f"/{path}" if path else "/"
    req_data = request.get_data()
    headers = {k: v for k, v in request.headers if k.lower() != "host"}
    url = f"{TARGET}{endpoint}"
    if request.query_string:
        url += f"?{request.query_string.decode()}"
    
    start_time = time.time()
    try:
        resp = requests.request(
            method=request.method,
            url=url,
            headers=headers,
            data=req_data,
            cookies=request.cookies,
            allow_redirects=False,
            timeout=30
        )
        resp_body = resp.content
        duration = time.time() - start_time
        
        log_entry(endpoint, request.method, headers, req_data, resp_body, resp.status_code, duration)
        
        excluded = ["content-encoding", "transfer-encoding", "connection"]
        resp_headers = [(k, v) for k, v in resp.raw.headers.items() if k.lower() not in excluded]
        return Response(resp_body, resp.status_code, resp_headers)
    except Exception as e:
        return Response(f"Proxy error: {e}", 502)

# ===== WebSocket Events =====
@socketio.on('connect')
def handle_connect():
    print(f'Client connected: {request.sid}')
    emit('connected', {'status': 'connected'})

@socketio.on('disconnect')
def handle_disconnect():
    print(f'Client disconnected: {request.sid}')

# ===== Cleanup old logs on startup =====
def cleanup_old_logs():
    if os.path.exists(LOG_JSON):
        try:
            with open(LOG_JSON, 'r', encoding='utf-8') as f:
                logs = json.load(f)
            if len(logs) > MAX_LOGS:
                logs = logs[:MAX_LOGS]
                with open(LOG_JSON, 'w', encoding='utf-8') as f:
                    json.dump(logs, f, indent=2)
        except:
            pass

if __name__ == '__main__':
    cleanup_old_logs()
    print("\n" + "="*60)
    print("   ᎬꪎՄ ─𑁍 𝐏𝐫𝐨𝐱𝐲 𝐂𝐚𝐩𝐭𝐮𝐫𝐞")
    print("="*60)
    print(f"   📡 Target: {TARGET}")
    print(f"   🌐 Dashboard: http://localhost:8080")
    print(f"   🔌 WebSocket: ws://localhost:8080")
    print("="*60)
    print("   Press Ctrl+C to stop\n")
    
    socketio.run(app, host='0.0.0.0', port=8080, debug=False, allow_unsafe_werkzeug=True)
