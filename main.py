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

# ============================================================
# TARGET SERVER — change this to whichever region you're on:
#   BD / TH / ME / EU / VN / TW / RU / SG → clientbp.ggpolarbear.com
#   IND                                   → client.ind.freefiremobile.com
#   US / NA / SAC / BR                    → client.us.freefiremobile.com
# ============================================================
TARGET = "https://clientbp.ggpolarbear.com"

LOG_FILE = "capture.txt"
LOG_JSON = "capture_logs.json"
GACHA_HEX_FILE = "gacha_payload.hex"      # <-- auto-saved latest gacha payload

# Endpoints we care about (case-insensitive substring match)
GACHA_KEYWORDS = ["purchasegacha", "gacha", "lottery", "spin"]

# Store logs in memory
captured_logs = []
latest_gacha = None       # holds the most recent PurchaseGacha entry
MAX_LOGS = 500


def is_gacha_endpoint(endpoint: str) -> bool:
    low = endpoint.lower()
    return any(kw in low for kw in GACHA_KEYWORDS)


def log_entry(endpoint, method, headers, req_data, resp_data, status, duration=0):
    global latest_gacha
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    entry = {
        "id": int(time.time() * 1000),
        "timestamp": ts,
        "endpoint": endpoint,
        "method": method,
        "status": status,
        "is_gacha": is_gacha_endpoint(endpoint),
        "request_headers": dict(headers),
        "request_body_hex": binascii.hexlify(req_data).decode() if req_data else None,
        "request_body_text": req_data.decode('utf-8', errors='ignore') if req_data else None,
        "request_body_base64": binascii.b2a_base64(req_data).decode().strip() if req_data else None,
        "response_body_hex": binascii.hexlify(resp_data).decode() if resp_data else None,
        "response_body_text": resp_data.decode('utf-8', errors='ignore') if resp_data else None,
        "response_body_base64": binascii.b2a_base64(resp_data).decode().strip() if resp_data else None,
        "request_size": len(req_data) if req_data else 0,
        "response_size": len(resp_data) if resp_data else 0,
        "duration": round(duration * 1000, 2)
    }

    captured_logs.insert(0, entry)
    if len(captured_logs) > MAX_LOGS:
        captured_logs.pop()

    # ---- Highlight gacha captures in the console ----
    if entry["is_gacha"]:
        latest_gacha = entry
        req_hex = entry["request_body_hex"] or "(empty)"
        resp_hex = entry["response_body_hex"] or "(empty)"

        print("\n" + "=" * 70)
        print("  🎯  GACHA REQUEST CAPTURED")
        print("=" * 70)
        print(f"  Endpoint : {method} {endpoint}")
        print(f"  Status   : {status}   ({entry['duration']}ms)")
        print(f"  Req Size : {entry['request_size']} bytes")
        print(f"  Resp Size: {entry['response_size']} bytes")
        print("-" * 70)
        print("  REQUEST BODY (HEX)  ← paste this into app.py RAW_HEX_PAYLOAD")
        print(f"  {req_hex}")
        print("-" * 70)
        print("  RESPONSE BODY (HEX)")
        print(f"  {resp_hex[:200]}{'...' if len(resp_hex) > 200 else ''}")
        print("=" * 70 + "\n")

        # Auto-save to file
        try:
            with open(GACHA_HEX_FILE, "w", encoding="utf-8") as f:
                f.write(req_hex + "\n")
        except Exception as e:
            print(f"[!] Could not save {GACHA_HEX_FILE}: {e}")

    # ---- Emit to WebSocket ----
    socketio.emit('new_log', entry)
    if entry["is_gacha"]:
        socketio.emit('new_gacha', entry)

    # ---- Save to JSON ----
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

    # ---- Append to text log ----
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


# ============================================================
# DASHBOARD
# ============================================================
@app.route('/')
def index():
    if os.path.exists('index.html'):
        return send_file('index.html')
    return Response(
        "<h1>Proxy Capture Running</h1>"
        "<p>Dashboard file index.html not found. API endpoints still work:</p>"
        "<ul>"
        "<li><a href='/api/gacha'>/api/gacha</a> – latest gacha request</li>"
        "<li><a href='/api/logs'>/api/logs</a> – all logs</li>"
        "<li><a href='/api/stats'>/api/stats</a> – stats</li>"
        "</ul>",
        mimetype='text/html'
    )


# ============================================================
# GACHA-SPECIFIC ENDPOINTS
# ============================================================
@app.route('/api/gacha')
def get_latest_gacha():
    """Return the most recent PurchaseGacha request (hex + base64 + headers)."""
    if not latest_gacha:
        return jsonify({"status": "error", "message": "No gacha request captured yet"}), 404

    return jsonify({
        "status": "success",
        "timestamp": latest_gacha["timestamp"],
        "endpoint": latest_gacha["endpoint"],
        "method": latest_gacha["method"],
        "request_hex": latest_gacha["request_body_hex"],
        "request_base64": latest_gacha["request_body_base64"],
        "request_size": latest_gacha["request_size"],
        "request_headers": latest_gacha["request_headers"],
        "response_hex": latest_gacha["response_body_hex"],
        "response_size": latest_gacha["response_size"],
        "http_status": latest_gacha["status"]
    })


@app.route('/api/gacha/hex')
def get_latest_gacha_hex():
    """Plain-text endpoint: just the hex string, ready to paste into app.py."""
    if not latest_gacha:
        return Response("No gacha request captured yet", status=404, mimetype='text/plain')
    return Response(
        latest_gacha["request_body_hex"] or "",
        mimetype='text/plain'
    )


@app.route('/api/gacha/export')
def export_gacha_bin():
    """Download the latest gacha request body as a .bin file."""
    if not latest_gacha or not latest_gacha["request_body_hex"]:
        return jsonify({"error": "No gacha request captured"}), 404
    raw_bytes = bytes.fromhex(latest_gacha["request_body_hex"])
    return Response(
        raw_bytes,
        mimetype='application/octet-stream',
        headers={'Content-Disposition': 'attachment;filename=gacha_request.bin'}
    )


@app.route('/api/gacha/history')
def get_gacha_history():
    """Return all captured gacha requests (newest first)."""
    gacha_logs = [l for l in captured_logs if l.get("is_gacha")]
    return jsonify({"status": "success", "count": len(gacha_logs), "data": gacha_logs})


# ============================================================
# GENERAL API
# ============================================================
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
    global captured_logs, latest_gacha
    captured_logs = []
    latest_gacha = None
    for f in (LOG_JSON, LOG_FILE, GACHA_HEX_FILE):
        if os.path.exists(f):
            os.remove(f)
    socketio.emit('logs_cleared')
    return jsonify({"status": "success", "message": "All logs cleared"})


@app.route('/api/stats')
def get_stats():
    total = len(captured_logs)
    success = len([l for l in captured_logs if 200 <= l['status'] < 300])
    redirect = len([l for l in captured_logs if 300 <= l['status'] < 400])
    error = len([l for l in captured_logs if l['status'] >= 400])
    gacha_count = len([l for l in captured_logs if l.get("is_gacha")])
    total_size = sum([l.get('response_size', 0) for l in captured_logs])
    avg_duration = 0
    if total > 0:
        avg_duration = sum([l.get('duration', 0) for l in captured_logs]) / total
    return jsonify({
        "total": total,
        "success": success,
        "redirect": redirect,
        "error": error,
        "gacha_captures": gacha_count,
        "total_size": total_size,
        "avg_duration": round(avg_duration, 2)
    })


# ============================================================
# RAW DATA ENDPOINTS
# ============================================================
@app.route('/api/raw/<int:log_id>')
def get_raw_body(log_id):
    for log in captured_logs:
        if log['id'] == log_id:
            return jsonify({
                "id": log['id'],
                "method": log['method'],
                "endpoint": log['endpoint'],
                "timestamp": log['timestamp'],
                "status": log['status'],
                "is_gacha": log.get("is_gacha", False),
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
    if captured_logs:
        log = captured_logs[0]
        return jsonify({
            "id": log['id'],
            "method": log['method'],
            "endpoint": log['endpoint'],
            "timestamp": log['timestamp'],
            "status": log['status'],
            "is_gacha": log.get("is_gacha", False),
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
    for log in captured_logs:
        if log['id'] == log_id:
            hex_data = log.get('request_body_hex', '')
            if hex_data:
                raw_bytes = bytes.fromhex(hex_data)
                return Response(
                    raw_bytes,
                    mimetype='application/octet-stream',
                    headers={'Content-Disposition': f'attachment;filename=request_{log_id}.bin'}
                )
            return jsonify({"error": "No body data"}), 404
    return jsonify({"error": "Log not found"}), 404


# ============================================================
# DOWNLOAD ENDPOINTS
# ============================================================
@app.route('/download/json')
def download_json():
    try:
        if os.path.exists(LOG_JSON):
            return send_file(LOG_JSON, as_attachment=True,
                             download_name='capture_logs.json',
                             mimetype='application/json')
        data = json.dumps(captured_logs, indent=2)
        return Response(data, mimetype='application/json',
                        headers={'Content-Disposition': 'attachment;filename=capture_logs.json'})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/download/txt')
def download_txt():
    try:
        if os.path.exists(LOG_FILE):
            return send_file(LOG_FILE, as_attachment=True,
                             download_name='capture.txt', mimetype='text/plain')
        return Response("No logs yet", mimetype='text/plain')
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/download/gacha')
def download_gacha():
    """Download the latest gacha hex as a .txt file."""
    if not latest_gacha:
        return jsonify({"error": "No gacha captured yet"}), 404
    return Response(
        latest_gacha["request_body_hex"] or "",
        mimetype='text/plain',
        headers={'Content-Disposition': 'attachment;filename=gacha_payload.hex'}
    )


@app.route('/download/all')
def download_all():
    try:
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            if os.path.exists(LOG_JSON):
                zip_file.write(LOG_JSON, 'capture_logs.json')
            else:
                zip_file.writestr('capture_logs.json', json.dumps(captured_logs, indent=2))

            if os.path.exists(LOG_FILE):
                zip_file.write(LOG_FILE, 'capture.txt')

            if latest_gacha and latest_gacha.get("request_body_hex"):
                zip_file.writestr('gacha_payload.hex', latest_gacha["request_body_hex"])
                zip_file.writestr('gacha_headers.json', json.dumps(latest_gacha["request_headers"], indent=2))

        zip_buffer.seek(0)
        return send_file(zip_buffer, as_attachment=True,
                         download_name='logs.zip', mimetype='application/zip')
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================
# PROXY
# ============================================================
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


# ============================================================
# WEBSOCKET
# ============================================================
@socketio.on('connect')
def handle_connect():
    print(f'Client connected: {request.sid}')
    emit('connected', {'status': 'connected'})


@socketio.on('disconnect')
def handle_disconnect():
    print(f'Client disconnected: {request.sid}')


# ============================================================
# STARTUP
# ============================================================
def cleanup_old_logs():
    if os.path.exists(LOG_JSON):
        try:
            with open(LOG_JSON, 'r', encoding='utf-8') as f:
                logs = json.load(f)
            if len(logs) > MAX_LOGS:
                logs = logs[:MAX_LOGS]
                with open(LOG_JSON, 'w', encoding='utf-8') as f:
                    json.dump(logs, f, indent=2)
        except Exception:
            pass


if __name__ == '__main__':
    cleanup_old_logs()
    print("\n" + "=" * 60)
    print("   ᎬꪎՄ ─𑁍  PROXY CAPTURE  (Gacha Focus Mode)")
    print("=" * 60)
    print(f"   📡 Target     : {TARGET}")
    print(f"   🌐 Dashboard  : http://localhost:8080")
    print(f"   🎯 Gacha Hex  : http://localhost:8080/api/gacha/hex")
    print(f"   🎯 Gacha JSON : http://localhost:8080/api/gacha")
    print(f"   📥 Download   : http://localhost:8080/download/gacha")
    print(f"   💾 Auto-saved : {GACHA_HEX_FILE}")
    print("=" * 60)
    print("   Watching for endpoints containing: purchasegacha, gacha, lottery, spin")
    print("   Press Ctrl+C to stop\n")

    socketio.run(app, host='0.0.0.0', port=8080, debug=False, allow_unsafe_werkzeug=True)
