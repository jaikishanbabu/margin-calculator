"""Minimal in-process imitation of the Supabase Storage REST API, for contract tests."""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "test-service-key"


class FakeStorage:
    def __init__(self):
        self.objects = {}  # (bucket, key) -> bytes
        self.tokens = {}   # token -> ("download"|"upload", bucket, key)
        self.unsigned_hits = []   # signed-link requests seen (no Authorization header)
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

            def _signed(self, kind):
                q = urllib.parse.urlparse(self.path)
                tok = urllib.parse.parse_qs(q.query).get("token", [""])[0]
                t = fake.tokens.get(tok)
                if not t or t[0] != kind:
                    self._send(400, b'{"error":"invalid signature"}')
                    return None
                fake.unsigned_hits.append((kind, t[2]))
                return t

            def do_PUT(self):
                path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
                if path.startswith("/storage/v1/object/upload/sign/"):
                    t = self._signed("upload")
                    if not t:
                        return
                    body = self._body()
                    if (t[1], t[2]) in fake.objects:
                        return self._send(400, b'{"statusCode":"409","error":"Duplicate"}')
                    fake.objects[(t[1], t[2])] = body
                    return self._send(200, json.dumps({"Key": f"{t[1]}/{t[2]}"}).encode())
                self._send(404)

            def do_POST(self):
                if not self._auth():
                    return
                path = urllib.parse.unquote(self.path)
                body = self._body()
                import secrets as _s
                for pre, kind, field in (("/storage/v1/object/upload/sign/", "upload", "url"),
                                         ("/storage/v1/object/sign/", "download", "signedURL")):
                    if path.startswith(pre):
                        bucket, key = path[len(pre):].split("/", 1)
                        if kind == "download" and (bucket, key) not in fake.objects:
                            return self._send(400, b'{"statusCode":"404","error":"not_found"}')
                        tok = _s.token_hex(8)
                        fake.tokens[tok] = (kind, bucket, key)
                        rel = ("/object/upload/sign/" if kind == "upload" else "/object/sign/") + f"{bucket}/{urllib.parse.quote(key)}?token={tok}"
                        return self._send(200, json.dumps({field: rel}).encode())
                if path == "/storage/v1/object/copy":
                    req = json.loads(body)
                    b, src, dst = req["bucketId"], req["sourceKey"], req["destinationKey"]
                    if (b, src) not in fake.objects:
                        return self._send(400, b'{"statusCode":"404","error":"not_found","message":"Object not found"}')
                    if (b, dst) in fake.objects:
                        return self._send(400, b'{"statusCode":"409","error":"Duplicate","message":"The resource already exists"}')
                    fake.objects[(b, dst)] = fake.objects[(b, src)]
                    return self._send(200, json.dumps({"Key": f"{b}/{dst}"}).encode())
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
                if urllib.parse.urlparse(self.path).path.startswith("/storage/v1/object/sign/"):
                    t = self._signed("download")
                    if not t:
                        return
                    if (t[1], t[2]) not in fake.objects:
                        return self._send(400, b'{"error":"not_found"}')
                    return self._send(200, fake.objects[(t[1], t[2])], "application/octet-stream")
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
