"""假的 OIDC 身份提供方：discovery、授权、令牌、RS256 JWKS，跑在本机的一个线程里（127.0.0.1 的随机端口）。

授权地址不弹登录页：直接按 next_claims 签一个授权码，302 跳回 redirect_uri（带上 state）。令牌接口核对授权码只用一次、
code_verifier 对得上 S256 的 code_challenge、client_secret 对得上，交回按 RS256 签的 id_token（nonce 原样带回）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


class FakeOidc:
    def __init__(self, *, client_id: str = "agentlab", client_secret: str = "s3cret") -> None:
        self.client_id, self.client_secret = client_id, client_secret
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = secrets.token_hex(4)
        #: 下一次授权签给谁：sub、preferred_username、groups……
        self.next_claims: dict[str, Any] = {"sub": "u-1", "preferred_username": "alice", "groups": []}
        self.codes: dict[str, dict[str, Any]] = {}
        self.token_calls = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.issuer = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeOidc":
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._server.shutdown()
        self._server.server_close()

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [{**jwk, "kid": self.kid, "alg": "RS256", "use": "sig"}]}

    def id_token(self, nonce: str) -> str:
        now = int(time.time())
        claims = {"iss": self.issuer, "aud": self.client_id, "iat": now, "exp": now + 300, "nonce": nonce,
                  **self.next_claims}
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:  # 测试输出里不要访问日志
                pass

            def _json(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                url = urlsplit(self.path)
                if url.path == "/.well-known/openid-configuration":
                    self._json(200, {"issuer": fake.issuer, "authorization_endpoint": f"{fake.issuer}/authorize",
                                     "token_endpoint": f"{fake.issuer}/token", "jwks_uri": f"{fake.issuer}/jwks",
                                     "id_token_signing_alg_values_supported": ["RS256"]})
                elif url.path == "/jwks":
                    self._json(200, fake.jwks())
                elif url.path == "/authorize":
                    q = {k: v[0] for k, v in parse_qs(url.query).items()}
                    code = secrets.token_urlsafe(16)
                    fake.codes[code] = {"nonce": q["nonce"], "challenge": q["code_challenge"],
                                        "redirect_uri": q["redirect_uri"], "client_id": q["client_id"]}
                    self.send_response(302)
                    self.send_header("location", f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}")
                    self.end_headers()
                else:
                    self._json(404, {"error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                if urlsplit(self.path).path != "/token":
                    self._json(404, {"error": "not_found"})
                    return
                fake.token_calls += 1
                length = int(self.headers.get("content-length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                grant = fake.codes.pop(form.get("code", ""), None)
                if grant is None:
                    self._json(400, {"error": "invalid_grant", "error_description": "授权码无效或已使用"})
                    return
                challenge = base64.urlsafe_b64encode(
                    hashlib.sha256(form.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
                if challenge != grant["challenge"] or form.get("redirect_uri") != grant["redirect_uri"] \
                        or form.get("client_secret") != fake.client_secret:
                    self._json(400, {"error": "invalid_grant"})
                    return
                self._json(200, {"access_token": secrets.token_urlsafe(16), "token_type": "Bearer",
                                 "id_token": fake.id_token(grant["nonce"])})

        return Handler
