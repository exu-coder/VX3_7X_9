from flask import Flask, request, Response
import requests
import binascii
from datetime import datetime

app = Flask(__name__)
TARGET = "https://clientbp.ggpolarbear.com"
LOG_FILE = "capture.txt"

def log_entry(endpoint, method, headers, req_data, resp_data, status):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hdr = "\n".join(f"{k}: {v}" for k, v in headers.items() if k.lower() != "host")
    entry = f"""
{'='*60}
[{ts}] {method} {endpoint} -> {status}
{'='*60}
REQUEST HEADERS:
{hdr}

REQUEST BODY (hex):
{binascii.hexlify(req_data).decode() if req_data else '(empty)'}

RESPONSE BODY (hex):
{binascii.hexlify(resp_data).decode() if resp_data else '(empty)'}
{'='*60}
"""
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(entry)

@app.route("/", defaults={"path": ""}, methods=["GET","POST","PUT","DELETE","PATCH","OPTIONS","HEAD"])
@app.route("/<path:path>", methods=["GET","POST","PUT","DELETE","PATCH","OPTIONS","HEAD"])
def proxy(path):
    endpoint = f"/{path}" if path else "/"
    req_data = request.get_data()
    headers = {k: v for k, v in request.headers if k.lower() != "host"}
    url = f"{TARGET}{endpoint}"
    if request.query_string:
        url += f"?{request.query_string.decode()}"
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
        log_entry(endpoint, request.method, headers, req_data, resp_body, resp.status_code)
        excluded = ["content-encoding", "transfer-encoding", "connection"]
        resp_headers = [(k, v) for k, v in resp.raw.headers.items() if k.lower() not in excluded]
        return Response(resp_body, resp.status_code, resp_headers)
    except Exception as e:
        return Response(f"Proxy error: {e}", 502)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)