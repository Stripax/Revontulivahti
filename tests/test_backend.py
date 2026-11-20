import base64
import hashlib
import io
import json
import re
import tempfile
import time
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit, parse_qs

import httpx
from authlib.integrations.base_client.errors import OAuthError
from fastapi.testclient import TestClient
from fastapi.responses import RedirectResponse
from PIL import Image
from joserfc import jwt
from joserfc.jwk import RSAKey

from backend.gmail_service import Settings, create_app, create_google_client, SCOPES, GMAIL_SCOPE
import revontulivahti as rv
import test_forecast as fixture


def png():
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class GmailBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings("test-client", "secret-client", "https://mail.example.com", "a" * 48,
                                 str(Path(self.temp.name) / "gmail.sqlite3"))
        self.credentials = {"access_token": "secret-access-token", "id_token": "verified-id-token", "scope": SCOPES,
                            "expires_at": time.time() + 900,
                            "userinfo": {"sub": "google-user-id", "email": "sender@gmail.com", "email_verified": True}}
        self.google = type("FakeGoogle", (), {})()
        self.google.authorize_redirect = AsyncMock(return_value=RedirectResponse("https://accounts.google.com/auth"))
        self.google.authorize_access_token = AsyncMock(return_value=self.credentials)
        self.sender = self.enterContext(patch("backend.gmail_service.gmail_send", new_callable=AsyncMock, return_value="gmail-message-id"))
        self.client = self.enterContext(TestClient(create_app(self.settings, self.google), base_url=self.settings.public_url))
        self.payload = {"recipient": "recipient@example.com", "city": "Helsinki",
                        "text": "Revontulivahti: esimerkkiraportti, arvio epävarma.",
                        "chart_base64": base64.b64encode(png()).decode()}

    def post(self, payload=None, key=None):
        payload = self.payload if payload is None else payload
        key = key or hashlib.sha256(json.dumps(payload).encode()).hexdigest()
        return self.client.post("/v1/reports", json=payload, headers={"Idempotency-Key": key})

    def login(self):
        reply = self.post().json()
        self.link = reply["authorization_url"]
        response = self.client.get(self.link, follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        response = self.client.get("/oauth/callback?code=code&state=state", follow_redirects=False)
        return response

    def csrf(self):
        response = self.client.get("/review")
        self.assertEqual(response.status_code, 200)
        return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)

    def row(self):
        with self.client.app.state.store.connect() as db:
            return dict(db.execute("SELECT * FROM gmail_reports").fetchone())

    def test_upload_only_returns_link_no_email_and_is_idempotent(self):
        first = self.post()
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()["status"], "authorization_required")
        self.assertEqual(self.post().json(), first.json())
        self.sender.assert_not_called()
        self.assertNotIn("secret", first.text)
        self.assertEqual(self.client.get("/review").status_code, 403)
        self.assertEqual(self.client.post("/send", data={"csrf": "bad"}).status_code, 403)

    def test_review_and_send_from_selected_google_account_once(self):
        self.assertEqual(self.login().status_code, 303)
        self.sender.assert_not_called()
        review = self.client.get("/review")
        self.assertIn("sender@gmail.com", review.text)
        self.assertIn("recipient@example.com", review.text)
        self.assertIn("Lähetä raportti Gmailista", review.text)
        self.assertEqual(self.client.get("/preview.png").content, png())
        csrf = self.csrf()
        sent = self.client.post("/send", data={"csrf": csrf})
        self.assertEqual(sent.status_code, 200)
        self.assertIn("Gmail hyväksyi", sent.text)
        access, raw = self.sender.call_args.args
        self.assertEqual(access, "secret-access-token")
        message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
        self.assertEqual(str(message["From"]), "sender@gmail.com")
        self.assertEqual(str(message["To"]), "recipient@example.com")
        self.assertEqual(message.get_body(preferencelist=("plain",)).get_content().strip(), self.payload["text"])
        self.assertEqual(list(message.iter_attachments())[0].get_payload(decode=True), png())
        self.client.post("/send", data={"csrf": csrf})
        self.sender.assert_awaited_once()
        row = self.row()
        self.assertEqual(row["status"], "accepted")
        self.assertIsNone(row["access_token"])
        self.assertIsNone(row["payload"])
        self.assertEqual(self.post().json()["status"], "accepted")

    def test_credentials_and_payload_are_encrypted_never_in_cookie(self):
        self.login()
        row = self.row()
        self.assertNotIn(b"secret-access-token", row["access_token"])
        self.assertNotIn(b"recipient@example.com", row["payload"])
        self.assertNotIn(b"sender@gmail.com", row["sender"])
        cookie = self.client.cookies.get("__Host-revontulivahti")
        decoded = base64.b64decode(cookie.split(".")[0]).decode()
        self.assertNotIn("secret-access-token", decoded)
        self.assertNotIn("verified-id-token", decoded)
        self.assertNotIn("refresh_token", decoded)
        self.assertEqual(self.client.get("/review").headers["referrer-policy"], "no-referrer")

    def test_missing_scope_unverified_email_and_denial_never_send(self):
        for change in ({"scope": "openid email"}, {"id_token": ""},
                       {"userinfo": {"sub": "id", "email": "sender@gmail.com", "email_verified": False}}):
            self.google.authorize_access_token.return_value = dict(self.credentials, **change)
            self.assertEqual(self.login().status_code, 403)
        self.google.authorize_access_token.side_effect = OAuthError("access_denied")
        self.assertEqual(self.login().status_code, 400)
        self.sender.assert_not_called()

    def test_csrf_and_other_browser_cannot_send_or_preview(self):
        self.login()
        self.assertEqual(self.client.post("/send", data={"csrf": "wrong"}).status_code, 403)
        self.assertEqual(self.client.post("/send", content="csrf=%FF").status_code, 403)
        with TestClient(self.client.app, base_url=self.settings.public_url) as other:
            self.assertEqual(other.get("/review").status_code, 403)
            self.assertEqual(other.get("/preview.png").status_code, 403)
            other.get(self.link, follow_redirects=False)
            self.assertEqual(other.get("/oauth/callback").status_code, 403)
        self.sender.assert_not_called()

    def test_cancel_even_after_token_expiry_clears_credentials_without_sending(self):
        self.login()
        csrf = self.csrf()
        with self.client.app.state.store.connect() as db:
            db.execute("UPDATE gmail_reports SET token_expires=?", (time.time() - 1,))
        self.assertEqual(self.client.post("/send", data={"csrf": csrf}).status_code, 401)
        self.assertEqual(self.client.post("/cancel", data={"csrf": csrf}).status_code, 200)
        self.assertEqual(self.row()["status"], "cancelled")
        self.assertIsNone(self.row()["access_token"])
        self.sender.assert_not_called()

    def test_timeout_does_not_retry(self):
        self.login()
        csrf = self.csrf()
        self.sender.side_effect = httpx.ReadTimeout("secret-access-token")
        response = self.client.post("/send", data={"csrf": csrf})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("secret-access-token", response.text)
        self.assertEqual(self.row()["status"], "delivery_unknown")
        self.client.post("/send", data={"csrf": csrf})
        self.sender.assert_awaited_once()

    def test_google_rejection_clears_tokens_and_never_retries(self):
        self.login()
        csrf = self.csrf()
        response = httpx.Response(403, request=httpx.Request("POST", "https://gmail.googleapis.com"))
        self.sender.side_effect = httpx.HTTPStatusError("secret-access-token", request=response.request, response=response)
        result = self.client.post("/send", data={"csrf": csrf})
        self.assertEqual(result.status_code, 502)
        self.assertNotIn("secret-access-token", result.text)
        self.assertEqual(self.row()["status"], "failed")
        self.assertIsNone(self.row()["access_token"])
        self.client.post("/send", data={"csrf": csrf})
        self.sender.assert_awaited_once()

    def test_request_limits_invalid_payload_and_expiry(self):
        self.settings.recipient_limit = 1
        first = self.post(key="a" * 64)
        self.assertEqual(self.post(key="b" * 64).status_code, 429)
        self.assertEqual(self.post(dict(self.payload, text="Changed valid text for a conflicting request"), key="a" * 64).status_code, 409)
        self.assertEqual(self.post(dict(self.payload, recipient="bad")).status_code, 422)
        bad = dict(self.payload, chart_base64=base64.b64encode(b"bad-image" * 4).decode())
        self.assertEqual(self.post(bad).status_code, 422)
        with self.client.app.state.store.connect() as db:
            db.execute("UPDATE gmail_reports SET expires=?", (time.time() - 1,))
        self.assertEqual(self.client.get(first.json()["authorization_url"]).status_code, 410)
        self.client.app.state.store.cleanup()
        self.assertIsNone(self.row()["payload"])

    def test_report_escaping_and_sender_rate_limit(self):
        self.payload["text"] += " <script>steal()</script>"
        self.login()
        response = self.client.get("/review")
        self.assertNotIn("<script>", response.text)
        self.assertIn("&lt;script&gt;", response.text)
        with self.client.app.state.store.connect() as db:
            db.execute("UPDATE gmail_reports SET sent_at=?", (time.time(),))
        self.settings.sender_limit = 1
        self.assertEqual(self.client.post("/send", data={"csrf": self.csrf()}).status_code, 429)
        self.sender.assert_not_called()

    def test_client_backend_contract_without_google_tokens_in_colab(self):
        points = rv.sampling_points(fixture.CITY["latitude"], fixture.CITY["longitude"], 0)
        result = rv.evaluate(fixture.CITY, points, fixture.weather_fixture(points), fixture.kp_fixture(), fixture.NOW)
        chart = Path(self.temp.name) / "chart.png"
        chart.write_bytes(png())
        def transport(request, timeout):
            self.assertNotIn("Authorization", request.headers)
            response = self.client.post("/v1/reports", content=request.data, headers=request.headers)
            self.assertEqual(response.status_code, 202)
            return io.BytesIO(json.dumps(response.json()).encode())
        with patch.object(rv, "urlopen", side_effect=transport):
            response = rv.send_via_backend(result, "recipient@example.com", chart, self.settings.public_url)
        self.assertEqual(response["status"], "authorization_required")
        self.assertNotIn("access_token", response)


class RealOAuthTests(unittest.TestCase):
    """Real Authlib state/PKCE/OIDC verification; only Google's HTTP endpoints are mocked."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings("test-client", "test-secret", "https://mail.example.com", "a" * 48,
                                 str(Path(self.temp.name) / "gmail.sqlite3"))
        self.signing_key = RSAKey.generate_key(2048)
        self.jwk = self.signing_key.as_dict(private=False)
        self.jwk["kid"] = "test-key"
        self.nonce = None
        self.bad_nonce = False
        self.bad_audience = False
        self.bad_signature = False
        self.token_calls = 0
        google = create_google_client(self.settings)
        google.client_kwargs["transport"] = httpx.MockTransport(self.google_http)
        self.client = self.enterContext(TestClient(create_app(self.settings, google), base_url=self.settings.public_url))

    def google_http(self, request):
        url = str(request.url)
        if "openid-configuration" in url:
            return httpx.Response(200, json={"issuer": "https://accounts.google.com",
                "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_endpoint": "https://oauth2.googleapis.com/token", "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
                "id_token_signing_alg_values_supported": ["RS256"]})
        if "/certs" in url:
            return httpx.Response(200, json={"keys": [self.jwk]})
        if url == "https://oauth2.googleapis.com/token":
            self.token_calls += 1
            body = parse_qs(request.content.decode())
            self.assertIn("code_verifier", body)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"][0].encode()).digest()).decode().rstrip("=")
            self.assertEqual(challenge, self.challenge)
            claims = {"iss": "https://accounts.google.com", "aud": "wrong" if self.bad_audience else "test-client",
                      "sub": "google-id", "email": "sender@gmail.com", "email_verified": True,
                      "iat": int(time.time()), "exp": int(time.time()) + 300,
                      "nonce": "wrong" if self.bad_nonce else self.nonce}
            token = jwt.encode({"alg": "RS256", "kid": "test-key"}, claims, self.signing_key)
            if self.bad_signature:
                token = jwt.encode({"alg": "RS256", "kid": "test-key"}, claims, RSAKey.generate_key(2048))
            return httpx.Response(200, json={"access_token": "real-verified-access-token", "token_type": "Bearer",
                                            "expires_in": 300, "scope": SCOPES, "id_token": token})
        raise AssertionError("Unexpected external HTTP request: " + url)

    def start(self):
        payload = {"recipient": "recipient@example.com", "city": "Helsinki", "text": "Revontulivahti test report text.",
                   "chart_base64": base64.b64encode(png()).decode()}
        reply = self.client.post("/v1/reports", json=payload, headers={"Idempotency-Key": "a" * 64})
        response = self.client.get(reply.json()["authorization_url"], follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        params = parse_qs(urlsplit(response.headers["location"]).query)
        self.assertEqual(params["scope"][0], SCOPES)
        self.assertEqual(params["access_type"], ["online"])
        self.assertEqual(params["prompt"], ["select_account consent"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.nonce, self.challenge = params["nonce"][0], params["code_challenge"][0]
        return params["state"][0]

    def test_valid_signed_oidc_token_and_pkce(self):
        state = self.start()
        response = self.client.get("/oauth/callback", params={"code": "code", "state": state}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("sender@gmail.com", self.client.get("/review").text)
        self.assertEqual(self.token_calls, 1)

    def test_wrong_state_is_rejected_before_token_exchange(self):
        self.start()
        response = self.client.get("/oauth/callback?code=code&state=wrong", follow_redirects=False)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.token_calls, 0)

    def test_wrong_nonce_is_rejected(self):
        state = self.start()
        self.bad_nonce = True
        response = self.client.get("/oauth/callback", params={"code": "code", "state": state}, follow_redirects=False)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/review").status_code, 403)

    def test_wrong_audience_is_rejected(self):
        state = self.start()
        self.bad_audience = True
        response = self.client.get("/oauth/callback", params={"code": "code", "state": state}, follow_redirects=False)
        self.assertEqual(response.status_code, 400)

    def test_wrong_signature_is_rejected(self):
        state = self.start()
        self.bad_signature = True
        response = self.client.get("/oauth/callback", params={"code": "code", "state": state}, follow_redirects=False)
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
