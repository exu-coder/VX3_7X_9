from flask import Flask, request, Response, jsonify, send_file
from flask_socketio import SocketIO, emit
import requests
import binascii
import json
import os
import time
import io
import zipfile
import threading
from datetime import datetime

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

# ============================================================
# TARGET
# ============================================================
TARGET = "https://clientbp.ggpolarbear.com"

# ============================================================
# AES KEYS
# ============================================================
AES_KEY = b'Yg&tc%DEuh6%Zc^8'
AES_IV  = b'6oyZDr22E3ychjM%'

ALT_KEYS = [
    (b'Yg&tc%DEuh6%Zc^8', b'6oyZDr22E3ychjM%'),
]

# ============================================================
# CONFIG
# ============================================================
LOG_FILE = "capture.txt"
LOG_JSON = "capture_logs.json"
GACHA_HEX_FILE = "gacha_payload.hex"
DECRYPTED_DIR = "decrypted"
INTERESTING_DIR = "interesting"

INTERESTING_KEYWORDS = [
    "purchasegacha", "gacha", "lottery", "spin",
    "reward", "chest", "draw", "prize", "item",
    "shop", "buy", "purchase", "redeem", "coupon",
    "lotteryid", "lottery_id", "inventory", "equip",
]
MIN_INTERESTING_SIZE = 32
MAX_LOGS = 1000
PORT = 8080

captured_logs = []
latest_gacha = None
LOG_LOCK = threading.Lock()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'exu-proxy-capture-secret-key-2024'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')


# ============================================================
# AES HELPERS
# ============================================================
def try_aes_decrypt(raw: bytes, key: bytes, iv: bytes) -> bytes:
    if not raw:
        return None
    if len(raw) % 16 != 0:
        raw_padded = raw + b'\x00' * (16 - (len(raw) % 16))
    else:
        raw_padded = raw
    try:
        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(raw_padded)
        try:
            return unpad(decrypted, AES.block_size)
        except ValueError:
            return decrypted
    except Exception:
        return None


def looks_like_protobuf(b: bytes) -> bool:
    if not b or len(b) < 2:
        return False
    for i in range(min(4, len(b))):
        tag = b[i]
        if tag == 0:
            continue
        wire_type = tag & 0x07
        field_num = tag >> 3
        if field_num >= 1 and wire_type in (0, 1, 2, 5):
            return True
    return False


def try_all_keys(raw: bytes):
    results = []
    for idx, (k, v) in enumerate(ALT_KEYS):
        dec = try_aes_decrypt(raw, k, v)
        if dec is None:
            continue
        results.append({
            "key_index": idx,
            "key": k.decode(errors="ignore"),
            "iv": v.decode(errors="ignore"),
            "decrypted_hex": dec.hex(),
            "decrypted_len": len(dec),
            "decrypted_text_preview": dec[:64].decode('utf-8', errors='replace'),
            "looks_like_protobuf": looks_like_protobuf(dec),
        })
    return results


def best_decryption(raw: bytes):
    if not raw or len(raw) < 16:
        return None
    results = try_all_keys(raw)
    if not results:
        return None
    for r in results:
        if r["looks_like_protobuf"]:
            return r
    for r in results:
        try:
            raw[:32].decode('utf-8')
            return r
        except Exception:
            continue
    return results[0]


# ============================================================
# LOGGING
# ============================================================
def is_interesting(endpoint, req_size, resp_size):
    low = endpoint.lower()
    if any(kw in low for kw in INTERESTING_KEYWORDS):
        return True
    if resp_size >= MIN_INTERESTING_SIZE:
        return True
    return False


def contains_hacks(raw):
    if not raw:
        return False
    return b"hacks" in raw or b"HACKS" in raw or b"hack" in raw


def log_entry(endpoint, method, headers, req_data, resp_data, status, duration=0):
    global latest_gacha
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    req_hex = binascii.hexlify(req_data).decode() if req_data else None
    resp_hex = binascii.hexlify(resp_data).decode() if resp_data else None

    req_dec = best_decryption(req_data) if req_data else None
    resp_dec = best_decryption(resp_data) if resp_data else None

    interesting = is_interesting(endpoint,
                                 len(req_data) if req_data else 0,
                                 len(resp_data) if resp_data else 0)
    is_gacha = "purchasegacha" in endpoint.lower() or "gacha" in endpoint.lower()
    has_hacks = contains_hacks(req_data or b"") or contains_hacks(resp_data or b"")

    entry = {
        "id": int(time.time() * 1000),
        "timestamp": ts,
        "endpoint": endpoint,
        "method": method,
        "status": status,
        "is_interesting": interesting,
        "is_gacha": is_gacha,
        "contains_hacks": has_hacks,
        "request_headers": dict(headers),
        "request_size": len(req_data) if req_data else 0,
        "response_size": len(resp_data) if resp_data else 0,
        "duration": round(duration * 1000, 2),
        "request_body_hex": req_hex,
        "request_body_base64": binascii.b2a_base64(req_data).decode().strip() if req_data else None,
        "response_body_hex": resp_hex,
        "response_body_base64": binascii.b2a_base64(resp_data).decode().strip() if resp_data else None,
        "request_decrypted": req_dec,
        "response_decrypted": resp_dec,
    }

    with LOG_LOCK:
        captured_logs.insert(0, entry)
        if len(captured_logs) > MAX_LOGS:
            captured_logs.pop()

    # ---- Console ----
    if is_gacha:
        latest_gacha = entry
        print("\n" + "=" * 74)
        print("  🎯  GACHA REQUEST CAPTURED")
        print("=" * 74)
        print(f"  Endpoint : {method} {endpoint}")
        print(f"  Status   : {status}   ({entry['duration']}ms)")
        print(f"  Req Size : {entry['request_size']} bytes")
        print(f"  Resp Size: {entry['response_size']} bytes")
        print("-" * 74)
        print(f"  REQUEST  (raw hex)       : {req_hex}")
        if req_dec:
            print(f"  REQUEST  (AES decrypted) : {req_dec['decrypted_hex']}")
            print(f"    proto? {req_dec['looks_like_protobuf']}  preview: {req_dec['decrypted_text_preview']!r}")
        print("-" * 74)
        print(f"  RESPONSE (raw hex)       : {(resp_hex or '')[:160]}")
        if resp_dec:
            print(f"  RESPONSE (AES decrypted) : {resp_dec['decrypted_hex'][:160]}")
            print(f"    proto? {resp_dec['looks_like_protobuf']}  preview: {resp_dec['decrypted_text_preview']!r}")
        print("=" * 74 + "\n")
        try:
            with open(GACHA_HEX_FILE, "w", encoding="utf-8") as f:
                f.write((req_hex or "") + "\n")
        except Exception:
            pass
    elif has_hacks:
        print("\n" + "!" * 74)
        print("  ⚠️  'hacks' MARKER FOUND")
        print("!" * 74 + "\n")
    else:
        marker = ""
        if req_dec and req_dec["looks_like_protobuf"]:
            marker += " [req→proto]"
        if resp_dec and resp_dec["looks_like_protobuf"]:
            marker += " [resp→proto]"
        print(f"[*] {method:6s} {endpoint:45s} -> {status}  "
              f"(req {entry['request_size']}B / resp {entry['response_size']}B){marker}")

    # ---- Save interesting blobs ----
    if interesting:
        try:
            os.makedirs(DECRYPTED_DIR, exist_ok=True)
            safe_ep = endpoint.strip("/").replace("/", "_") or "root"
            base = f"{DECRYPTED_DIR}/{entry['id']}_{safe_ep}"
            if req_data:
                with open(base + "_req.raw.bin", "wb") as f:
                    f.write(req_data)
                if req_dec:
                    with open(base + "_req.dec.bin", "wb") as f:
                        f.write(bytes.fromhex(req_dec["decrypted_hex"]))
            if resp_data:
                with open(base + "_resp.raw.bin", "wb") as f:
                    f.write(resp_data)
                if resp_dec:
                    with open(base + "_resp.dec.bin", "wb") as f:
                        f.write(bytes.fromhex(resp_dec["decrypted_hex"]))
            entry["saved_to"] = base
        except Exception as e:
            print(f"[!] Save error: {e}")

    # ---- WebSocket ----
    socketio.emit('new_log', entry)
    if is_gacha:
        socketio.emit('new_gacha', entry)
    if has_hacks:
        socketio.emit('hacks_marker', entry)

    # ---- JSON ----
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
        print(f"[JSON] save err: {e}")

    # ---- Text log ----
    hdr = "\n".join(f"{k}: {v}" for k, v in headers.items() if k.lower() != "host")
    text_entry = f"""
{'='*74}
[{ts}] {method} {endpoint} -> {status} ({round(duration*1000,2)}ms)
INTERESTING: {interesting} | GACHA: {is_gacha} | HACKS: {has_hacks}
{'='*74}
REQUEST HEADERS:
{hdr}

REQUEST  (hex):
{req_hex or '(empty)'}

REQUEST  (AES decrypted hex):
{(req_dec or {}).get('decrypted_hex', '(no AES layer)')}

REQUEST  (AES decrypted preview):
{(req_dec or {}).get('decrypted_text_preview', '')}

RESPONSE (hex):
{resp_hex or '(empty)'}

RESPONSE (AES decrypted hex):
{(resp_dec or {}).get('decrypted_hex', '(no AES layer)')}

RESPONSE (AES decrypted preview):
{(resp_dec or {}).get('decrypted_text_preview', '')}
{'='*74}
"""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(text_entry)
    except Exception:
        pass


# ============================================================
# ROUTES
# ============================================================
@app.route('/')
def index():
    if os.path.exists('index.html'):
        return send_file('index.html')

    return Response(f"""
    <html><head><title>Proxy Capture</title>
    <style>
        body {{ font-family: monospace; background:#111; color:#0f0; padding:20px; }}
        a {{ color:#6cf; }}
        .pub {{ background:#222; padding:10px; border:1px solid #0f0; }}
        table {{ border-collapse:collapse; }}
        td,th {{ padding:6px 12px; border:1px solid #333; }}
    </style></head><body>
    <h1>Proxy Capture + AES Decrypt</h1>
    <div class="pub">
        <b>Local URL:</b> http://localhost:{PORT}<br>
        <b>Access from other devices on the same Wi-Fi:</b> http://&lt;your-PC-IP&gt;:{PORT}
    </div>
    <h2>Endpoints</h2>
    <table>
      <tr><th>Path</th><th>Description</th></tr>
      <tr><td><a href="/api/logs">/api/logs</a></td><td>All captured entries (JSON)</td></tr>
      <tr><td><a href="/api/decrypted">/api/decrypted</a></td><td>Only entries with successful AES decrypt</td></tr>
      <tr><td><a href="/api/interesting">/api/interesting</a></td><td>Gacha/shop/reward requests</td></tr>
      <tr><td><a href="/api/gacha">/api/gacha</a></td><td>Latest PurchaseGacha request (JSON)</td></tr>
      <tr><td><a href="/api/gacha/hex">/api/gacha/hex</a></td><td>Latest PurchaseGacha raw hex</td></tr>
      <tr><td><a href="/api/gacha/hex_decrypted">/api/gacha/hex_decrypted</a></td><td>Latest PurchaseGacha decrypted hex</td></tr>
      <tr><td><a href="/api/stats">/api/stats</a></td><td>Counts, AES key, decryption success rates</td></tr>
      <tr><td><a href="/download/all">/download/all</a></td><td>ZIP of everything</td></tr>
      <tr><td><a href="/download/decrypted">/download/decrypted</a></td><td>ZIP of all decrypted payloads</td></tr>
    </table>
    <p>AES key: <code>{AES_KEY.decode()}</code> / IV: <code>{AES_IV.decode()}</code></p>
    </body></html>
    """, mimetype='text/html')


@app.route('/api/decrypted')
def api_decrypted():
    hits = []
    for l in captured_logs:
        if l.get("request_decrypted") or l.get("response_decrypted"):
            hits.append({
                "id": l["id"], "timestamp": l["timestamp"],
                "endpoint": l["endpoint"], "method": l["method"],
                "status": l["status"],
                "request_hex": l["request_body_hex"],
                "request_decrypted": l["request_decrypted"],
                "response_hex": l["response_body_hex"],
                "response_decrypted": l["response_decrypted"],
            })
    return jsonify({"status": "success", "count": len(hits), "data": hits})


@app.route('/api/decrypted/<int:log_id>')
def api_decrypted_one(log_id):
    for l in captured_logs:
        if l["id"] == log_id:
            return jsonify(l)
    return jsonify({"error": "not found"}), 404


@app.route('/api/gacha')
def api_gacha():
    if not latest_gacha:
        return jsonify({"error": "no gacha yet"}), 404
    return jsonify(latest_gacha)


@app.route('/api/gacha/hex')
def api_gacha_hex():
    if not latest_gacha:
        return Response("no gacha yet", status=404, mimetype='text/plain')
    return Response(latest_gacha["request_body_hex"] or "", mimetype='text/plain')


@app.route('/api/gacha/hex_decrypted')
def api_gacha_hex_decrypted():
    if not latest_gacha:
        return Response("no gacha yet", status=404, mimetype='text/plain')
    dec = latest_gacha.get("request_decrypted")
    if not dec:
        return Response("gacha request was not AES-encrypted", mimetype='text/plain')
    return Response(dec["decrypted_hex"], mimetype='text/plain')


@app.route('/api/interesting')
def api_interesting():
    hits = [l for l in captured_logs if l.get("is_interesting")]
    return jsonify({"status": "success", "count": len(hits), "data": hits})


@app.route('/api/logs')
def api_logs():
    try:
        if os.path.exists(LOG_JSON):
            with open(LOG_JSON, 'r', encoding='utf-8') as f:
                return jsonify({"status": "success", "data": json.load(f)})
    except Exception:
        pass
    return jsonify({"status": "success", "data": captured_logs})


@app.route('/api/logs/clear', methods=['POST'])
def api_clear():
    global captured_logs, latest_gacha
    captured_logs = []
    latest_gacha = None
    for f in (LOG_JSON, LOG_FILE, GACHA_HEX_FILE):
        if os.path.exists(f):
            os.remove(f)
    socketio.emit('logs_cleared')
    return jsonify({"status": "success"})


@app.route('/api/stats')
def api_stats():
    total = len(captured_logs)
    dec_req = sum(1 for l in captured_logs if l.get("request_decrypted"))
    dec_resp = sum(1 for l in captured_logs if l.get("response_decrypted"))
    proto_req = sum(1 for l in captured_logs
                    if l.get("request_decrypted") and l["request_decrypted"].get("looks_like_protobuf"))
    proto_resp = sum(1 for l in captured_logs
                     if l.get("response_decrypted") and l["response_decrypted"].get("looks_like_protobuf"))
    return jsonify({
        "total": total,
        "requests_decrypted": dec_req,
        "responses_decrypted": dec_resp,
        "request_decrypt_looks_proto": proto_req,
        "response_decrypt_looks_proto": proto_resp,
        "gacha_captures": sum(1 for l in captured_logs if l.get("is_gacha")),
        "interesting": sum(1 for l in captured_logs if l.get("is_interesting")),
        "aes_key": AES_KEY.decode(),
        "aes_iv": AES_IV.decode(),
        "local_url": f"http://localhost:{PORT}",
    })


# ============================================================
# DOWNLOADS
# ============================================================
@app.route('/download/all')
def download_all():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        if os.path.exists(LOG_JSON):
            z.write(LOG_JSON, 'capture_logs.json')
        if os.path.exists(LOG_FILE):
            z.write(LOG_FILE, 'capture.txt')
        if latest_gacha and latest_gacha.get("request_body_hex"):
            z.writestr('gacha_payload.hex', latest_gacha["request_body_hex"])
        if latest_gacha and latest_gacha.get("request_decrypted"):
            z.writestr('gacha_payload.decrypted.hex',
                       latest_gacha["request_decrypted"]["decrypted_hex"])
        for folder in (DECRYPTED_DIR, INTERESTING_DIR):
            if os.path.exists(folder):
                for fn in os.listdir(folder):
                    z.write(os.path.join(folder, fn), f"{folder}/{fn}")
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name='logs.zip', mimetype='application/zip')


@app.route('/download/decrypted')
def download_decrypted():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        if os.path.exists(DECRYPTED_DIR):
            for fn in os.listdir(DECRYPTED_DIR):
                z.write(os.path.join(DECRYPTED_DIR, fn), fn)
        hits = []
        for l in captured_logs:
            if l.get("request_decrypted") or l.get("response_decrypted"):
                hits.append({
                    "id": l["id"], "endpoint": l["endpoint"],
                    "request_hex": l["request_body_hex"],
                    "request_decrypted": l["request_decrypted"],
                    "response_hex": l["response_body_hex"],
                    "response_decrypted": l["response_decrypted"],
                })
        z.writestr('decrypted_summary.json', json.dumps(hits, indent=2))
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name='decrypted.zip', mimetype='application/zip')


# ============================================================
# PROXY
# ============================================================
@app.route('/<path:path>',
           methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD'])
@app.route('/', defaults={'path': ''},
           methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD'])
def proxy(path):
    endpoint = f"/{path}" if path else "/"
    req_data = request.get_data()
    headers = {k: v for k, v in request.headers if k.lower() != "host"}
    url = f"{TARGET}{endpoint}"
    if request.query_string:
        url += f"?{request.query_string.decode()}"

    t0 = time.time()
    try:
        resp = requests.request(
            method=request.method, url=url, headers=headers,
            data=req_data, cookies=request.cookies,
            allow_redirects=False, timeout=30
        )
        body = resp.content
        dur = time.time() - t0
        log_entry(endpoint, request.method, headers, req_data, body,
                  resp.status_code, dur)

        excluded = ["content-encoding", "transfer-encoding", "connection"]
        resp_headers = [(k, v) for k, v in resp.raw.headers.items()
                        if k.lower() not in excluded]
        return Response(body, resp.status_code, resp_headers)
    except Exception as e:
        return Response(f"Proxy error: {e}", 502)


# ============================================================
# WEBSOCKET
# ============================================================
@socketio.on('connect')
def ws_connect():
    emit('connected', {'status': 'connected'})


@socketio.on('disconnect')
def ws_disconnect():
    pass


# ============================================================
# MAIN
# ============================================================
if __name__ == '__main__':
    os.makedirs(DECRYPTED_DIR, exist_ok=True)
    os.makedirs(INTERESTING_DIR, exist_ok=True)

    print("\n" + "=" * 74)
    print("   PROXY CAPTURE  ─  AUTO AES DECRYPT")
    print("=" * 74)
    print(f"   📡 Target        : {TARGET}")
    print(f"   🔑 AES Key       : {AES_KEY.decode()}")
    print(f"   🔑 AES IV        : {AES_IV.decode()}")
    print(f"   🌐 Local         : http://localhost:{PORT}")
    print(f"   🌐 LAN           : http://<your-PC-IP>:{PORT}")
    print("=" * 74)
    print("   Every request/response is logged + AES-decrypted on the fly.")
    print("   Press Ctrl+C to stop\n")

    socketio.run(
        app,
        host='0.0.0.0',
        port=PORT,
        debug=False,
        allow_unsafe_werkzeug=True
    )
