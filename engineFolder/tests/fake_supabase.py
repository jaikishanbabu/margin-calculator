"""Minimal in-process imitation of the Supabase Storage REST API, for contract tests."""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "test-service-key"


class FakeStorage:
    def __init__(self):
        self.objects = {}  # (bucket, key) -> bytes
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _auth(self):
                if self.headers.get("Authorization") != f"Bearer {KEY}":
                    self._send(401, b'{"error":"unauthorized"}')
                    return False
                return True

            def _send(self, code, body=b"", ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def do_POST(self):
                if not self._auth():
                    return
                path = urllib.parse.unquote(self.path)
                body = self._body()
                if path.startswith("/storage/v1/object/list/"):
                    bucket = path.split("/")[5]
                    req = json.loads(body)
                    prefix = req["prefix"].strip("/")
                    seen, items = set(), []
                    for (b, k) in sorted(fake.objects):
                        if b != bucket or not (k.startswith(prefix + "/") if prefix else True):
                            continue
                        rest = k[len(prefix) + 1:] if prefix else k
                        head = rest.split("/")[0]
                        if head in seen:
                            continue
                        seen.add(head)
                        if "/" in rest:
                            items.append({"name": head, "id": None, "metadata": None})
                        else:
                            items.append({"name": head, "id": "x", "metadata": {"size": len(fake.objects[(b, k)])}})
                    off, lim = req.get("offset", 0), req.get("limit", 100)
                    return self._send(200, json.dumps(items[off:off + lim]).encode())
                if path.startswith("/storage/v1/object/"):
                    parts = path[len("/storage/v1/object/"):].split("/", 1)
                    bucket, key = parts[0], parts[1]
                    if (bucket, key) in fake.objects and self.headers.get("x-upsert") != "true":
                        return self._send(400, b'{"statusCode":"409","error":"Duplicate","message":"The resource already exists"}')
                    fake.objects[(bucket, key)] = body
                    return self._send(200, json.dumps({"Key": f"{bucket}/{key}"}).encode())
                self._send(404)

            def do_GET(self):
                if not self._auth():
                    return
                path = urllib.parse.unquote(self.path)
                pre = "/storage/v1/object/authenticated/"
                if path.startswith(pre):
                    bucket, key = path[len(pre):].split("/", 1)
                    if (bucket, key) in fake.objects:
                        return self._send(200, fake.objects[(bucket, key)], "application/octet-stream")
                    return self._send(400, b'{"statusCode":"404","error":"not_found"}')
                self._send(404)

            def do_DELETE(self):
                if not self._auth():
                    return
                path = urllib.parse.unquote(self.path)
                bucket = path.split("/")[4]
                for p in json.loads(self._body())["prefixes"]:
                    fake.objects.pop((bucket, p), None)
                self._send(200, b"[]")

        return H
