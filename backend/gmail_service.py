"""Gmail OAuth with browser-bound authorization and explicit send confirmation."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import html
import io
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import httpx
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.starlette_client import OAuth
from cryptography.fernet import Fernet, InvalidToken
from joserfc.errors import JoseError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"
SCOPES = "openid email " + GMAIL_SCOPE
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
MAX_BODY, MAX_IMAGE, TTL = 4_000_000, 2_500_000, 1800
TERMINAL = {"accepted", "delivery_unknown", "failed", "expired", "cancelled"}
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
           "Content-Security-Policy": "default-src 'none'; img-src 'self'; form-action 'self'; frame-ancestors 'none'"}


def email_address(value):
    value = value.strip()
    try:
        if not value.isascii() or any(c in value for c in "\r\n,;<> \t"):
            raise ValueError
        address = Address(addr_spec=value)
        if not address.username or not address.domain or address.addr_spec != value:
            raise ValueError
    except (ValueError, IndexError, HeaderParseError):
        raise ValueError("Virheellinen sähköpostiosoite.") from None
    return value


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient: str = Field(min_length=3, max_length=254)
    city: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=20, max_length=60_000)
    chart_base64: str = Field(min_length=20, max_length=3_400_000)

    @field_validator("recipient")
    @classmethod
    def check_address(cls, value):
        return email_address(value)

    @field_validator("city")
    @classmethod
    def check_city(cls, value):
        if any(ord(c) < 32 for c in value):
            raise ValueError("Virheellinen kaupungin nimi.")
        return value

    def checked_image(self):
        try:
            data = base64.b64decode(self.chart_base64, validate=True)
            if len(data) > MAX_IMAGE or not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "PNG" or image.width > 3000 or image.height > 9000 or image.width * image.height > 20_000_000:
                    raise ValueError
                image.verify()
        except (ValueError, OSError, binascii.Error, Image.DecompressionBombError):
            raise ValueError("Kuvaajan pitää olla kelvollinen PNG (enintään 2,5 MB).") from None
        return data


@dataclass
class Settings:
    client_id: str
    client_secret: str
    public_url: str
    token_secret: str
    database: str
    hourly_limit: int = 30
    daily_limit: int = 100
    recipient_limit: int = 3
    sender_limit: int = 10

    @classmethod
    def from_env(cls):
        return cls(os.getenv("GOOGLE_CLIENT_ID", ""), os.getenv("GOOGLE_CLIENT_SECRET", ""),
                   os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL", ""),
                   os.getenv("TOKEN_SECRET", ""), os.getenv("DATABASE_PATH", "/var/data/gmail.sqlite3"),
                   int(os.getenv("HOURLY_LIMIT", "30")), int(os.getenv("DAILY_LIMIT", "100")),
                   int(os.getenv("RECIPIENT_HOURLY_LIMIT", "3")), int(os.getenv("SENDER_HOURLY_LIMIT", "10")))

    def validate(self):
        if not self.client_id or not self.client_secret or len(self.token_secret) < 32:
            raise ValueError("Aseta GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET ja vähintään 32-merkkinen TOKEN_SECRET.")
        url = urlsplit(self.public_url)
        if (url.scheme != "https" or not url.netloc or url.username or url.password or url.query or url.fragment or url.path not in ("", "/")):
            raise ValueError("PUBLIC_BASE_URL pitää olla palvelun julkinen HTTPS-juuriosoite.")
        self.public_url = self.public_url.rstrip("/")
        if min(self.hourly_limit, self.daily_limit, self.recipient_limit, self.sender_limit) < 1:
            raise ValueError("Pyyntö- ja lähetysrajojen pitää olla positiivisia.")

    def derive(self, purpose, value=""):
        return hmac.new(self.token_secret.encode(), (purpose + ":" + value).encode(), hashlib.sha256).digest()

    def report_token(self, key):
        return base64.urlsafe_b64encode(self.derive("report-link", key)).decode().rstrip("=")

    def cipher(self):
        return Fernet(base64.urlsafe_b64encode(self.derive("database-encryption")))


class Store:
    def __init__(self, settings):
        self.settings, self.cipher = settings, settings.cipher()
        Path(settings.database).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS gmail_reports (
                request_key TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
                recipient_hash TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                payload BLOB, status TEXT NOT NULL, access_token BLOB, token_expires REAL,
                sender BLOB, owner_hash TEXT, browser_hash TEXT, sent_at REAL)""")
            db.execute("CREATE INDEX IF NOT EXISTS gmail_created ON gmail_reports(created)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.settings.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def cleanup(self):
        with self.connect() as db:
            db.execute("""UPDATE gmail_reports SET payload=NULL, access_token=NULL, sender=NULL, browser_hash=NULL,
                status=CASE WHEN status IN ('accepted','delivery_unknown','failed','cancelled') THEN status ELSE 'expired' END
                WHERE expires < ?""", (time.time(),))
            db.execute("DELETE FROM gmail_reports WHERE created < ?", (time.time() - 86400,))

    def reserve(self, report, key, payload_hash):
        self.cleanup()
        now = time.time()
        recipient_hash = self.settings.derive("recipient", report.recipient.lower()).hex()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM gmail_reports WHERE request_key=?", (key,)).fetchone()
            if row:
                if row["payload_hash"] != payload_hash:
                    raise HTTPException(409, "Pyyntötunniste on jo käytössä eri raportille.")
                return dict(row)
            hourly = db.execute("SELECT count(*) FROM gmail_reports WHERE created>?", (now - 3600,)).fetchone()[0]
            daily = db.execute("SELECT count(*) FROM gmail_reports WHERE created>?", (now - 86400,)).fetchone()[0]
            personal = db.execute("SELECT count(*) FROM gmail_reports WHERE created>? AND recipient_hash=?", (now - 3600, recipient_hash)).fetchone()[0]
            if hourly >= self.settings.hourly_limit or daily >= self.settings.daily_limit or personal >= self.settings.recipient_limit:
                raise HTTPException(429, "Palvelun pyyntöraja täyttyi. Käytä ZIP-latausta tai kokeile myöhemmin.")
            token_hash = hashlib.sha256(self.settings.report_token(key).encode()).hexdigest()
            payload = self.cipher.encrypt(report.model_dump_json().encode())
            db.execute("""INSERT INTO gmail_reports (request_key,payload_hash,token_hash,recipient_hash,created,expires,payload,status)
                VALUES (?,?,?,?,?,?,?,?)""", (key, payload_hash, token_hash, recipient_hash, now, now + TTL, payload, "pending"))
            return {"request_key": key, "status": "pending"}

    def lookup(self, token):
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise HTTPException(404, "Raporttilinkkiä ei löydy.")
        with self.connect() as db:
            row = db.execute("SELECT * FROM gmail_reports WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row:
            raise HTTPException(404, "Raporttilinkkiä ei löydy.")
        if row["expires"] < time.time():
            raise HTTPException(410, "Raporttilinkki vanheni. Tee Colabissa uusi ennustehaku.")
        return dict(row)

    def authorize(self, token, browser, identity, access_token, expires):
        row = self.lookup(token)
        browser_hash = self.settings.derive("browser", browser).hex()
        owner_hash = self.settings.derive("google-sub", identity["sub"]).hex()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM gmail_reports WHERE request_key=?", (row["request_key"],)).fetchone()
            if current["status"] not in ("pending", "authorized"):
                raise HTTPException(409, "Tämän raportin lähetys on jo käsitelty.")
            if current["browser_hash"] and not hmac.compare_digest(current["browser_hash"], browser_hash):
                raise HTTPException(403, "Raportti on jo valtuutettu toisessa selainistunnossa.")
            if current["owner_hash"] and not hmac.compare_digest(current["owner_hash"], owner_hash):
                raise HTTPException(403, "Raportille on jo valittu toinen Google-tili.")
            db.execute("""UPDATE gmail_reports SET status='authorized', access_token=?, token_expires=?,
                sender=?, owner_hash=?, browser_hash=? WHERE request_key=?""",
                       (self.cipher.encrypt(access_token.encode()), min(expires, row["expires"]),
                        self.cipher.encrypt(identity["email"].encode()), owner_hash, browser_hash, row["request_key"]))

    def browser_row(self, token, browser, claim=False):
        found = self.lookup(token)
        expected = self.settings.derive("browser", browser).hex()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = dict(db.execute("SELECT * FROM gmail_reports WHERE request_key=?", (found["request_key"],)).fetchone())
            if not row["browser_hash"] or not hmac.compare_digest(expected, row["browser_hash"]):
                raise HTTPException(403, "Avaa raportin Gmail-valtuutus samassa selaimessa.")
            if claim == "cancel" and row["status"] == "authorized":
                db.execute("UPDATE gmail_reports SET status='cancelled', access_token=NULL, sender=NULL, payload=NULL WHERE request_key=?", (row["request_key"],))
            elif claim and row["status"] == "authorized":
                if row["token_expires"] <= time.time() + 5:
                    raise HTTPException(401, "Gmail-valtuutus vanheni. Avaa raportin valtuutuslinkki uudelleen.")
                sent = db.execute("SELECT count(*) FROM gmail_reports WHERE owner_hash=? AND sent_at>?", (row["owner_hash"], time.time() - 3600)).fetchone()[0]
                if sent >= self.settings.sender_limit:
                    raise HTTPException(429, "Google-tilin palvelukohtainen lähetysraja täyttyi.")
                db.execute("UPDATE gmail_reports SET status='sending', sent_at=? WHERE request_key=?", (time.time(), row["request_key"]))
            return row

    def finish(self, key, status):
        with self.connect() as db:
            db.execute("UPDATE gmail_reports SET status=?, access_token=NULL, sender=NULL, payload=NULL WHERE request_key=?", (status, key))

    def report(self, row):
        try:
            return Report.model_validate_json(self.cipher.decrypt(row["payload"]))
        except (InvalidToken, TypeError, ValidationError):
            raise HTTPException(410, "Raportti ei ole enää saatavilla. Tee uusi haku.") from None


def create_google_client(config):
    oauth = OAuth()
    oauth.register("google", client_id=config.client_id, client_secret=config.client_secret,
                   server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                   client_kwargs={"scope": SCOPES, "code_challenge_method": "S256",
                                  "token_endpoint_auth_method": "client_secret_post", "timeout": 25})
    return oauth.create_client("google")


def identity_from_token(token):
    # Authlib verifies OAuth state, ID token signature/issuer/audience/expiry and nonce.
    info = token.get("userinfo", {})
    if (GMAIL_SCOPE not in token.get("scope", SCOPES).split() or not token.get("access_token") or not token.get("id_token")
            or not info.get("sub") or info.get("email_verified") is not True):
        raise HTTPException(403, "Gmailin lähetyslupa tai vahvistettu Google-tilin osoite puuttuu.")
    try:
        return {"sub": str(info["sub"]), "email": email_address(info.get("email", ""))}
    except ValueError:
        raise HTTPException(403, "Google-tilin sähköpostiosoitetta ei voitu vahvistaa.") from None


def message_raw(report, sender, key, domain, created):
    message = EmailMessage()
    message["From"], message["To"] = email_address(sender), report.recipient
    message["Subject"] = f"Revontulivahti: {report.city} – havaintoarvio"
    message["Date"] = format_datetime(datetime.fromtimestamp(created, timezone.utc))
    message["Message-ID"] = f"<revontulivahti.{key}@{domain}>"
    message.set_content(report.text)
    message.add_attachment(base64.b64decode(report.chart_base64), maintype="image", subtype="png", filename="revontulivahti_kuvaaja.png")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


async def gmail_send(access_token, raw):
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.post(GMAIL_SEND_URL, headers={"Authorization": "Bearer " + access_token}, json={"raw": raw})
        response.raise_for_status()
        result = response.json()
        if not isinstance(result.get("id"), str) or not result["id"]:
            raise ValueError("Missing Gmail message ID")
        return result["id"]


def page(text, extra="", status=200):
    return HTMLResponse('<!doctype html><html lang="fi"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                        f'<title>Revontulivahti · Gmail</title><body><h1>Revontulivahti</h1><p>{html.escape(text)}</p>{extra}</body></html>',
                        status_code=status, headers=HEADERS)


def create_app(settings=None, google_client=None):
    config = settings or Settings.from_env()
    google = google_client or create_google_client(config)

    @asynccontextmanager
    async def lifespan(app):
        config.validate()
        app.state.store, app.state.google = Store(config), google
        stopping = asyncio.Event()
        async def sweep():
            while not stopping.is_set():
                await run_in_threadpool(app.state.store.cleanup)
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=60)
                except TimeoutError:
                    pass
        worker = asyncio.create_task(sweep())
        try:
            yield
        finally:
            stopping.set()
            await worker

    app = FastAPI(title="Revontulivahti Gmail", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=config.derive("cookie-signing").hex(),
                       session_cookie="__Host-revontulivahti", max_age=TTL, same_site="lax", https_only=True)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.update(HEADERS)
        return response

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/v1/reports", status_code=202)
    async def request_report(request: Request):
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "Lähetä aineisto JSON-muodossa.")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise HTTPException(413, "Raportti on liian suuri.")
        key = request.headers.get("idempotency-key", "")
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise HTTPException(422, "Pyyntötunniste on virheellinen.")
        try:
            report = Report.model_validate_json(body)
            await run_in_threadpool(report.checked_image)
        except (ValidationError, ValueError):
            raise HTTPException(422, "Tarkista vastaanottaja, raportin teksti ja PNG-kuvaaja.") from None
        row = await run_in_threadpool(request.app.state.store.reserve, report, key, hashlib.sha256(body).hexdigest())
        if row["status"] in TERMINAL - {"accepted"}:
            raise HTTPException(410, "Raporttipyyntö on jo päättynyt. Tarkista posti ennen uutta ennustehakua.")
        if row["status"] == "accepted":
            return {"status": "accepted"}
        return {"status": "authorization_required", "authorization_url": config.public_url + "/authorize/" + config.report_token(key)}

    @app.get("/authorize/{token}")
    async def authorize(token: str, request: Request):
        row = await run_in_threadpool(request.app.state.store.lookup, token)
        if row["status"] in TERMINAL or row["status"] == "sending":
            return page("Raportin lähetys on jo käsitelty. Tarkista lähetetyt viestit ennen uutta pyyntöä.")
        browser = request.session.get("browser") or secrets.token_urlsafe(32)
        request.session.clear()
        request.session.update(browser=browser, report=token, csrf=secrets.token_urlsafe(32))
        try:
            return await google.authorize_redirect(request, config.public_url + "/oauth/callback",
                                                   access_type="online", prompt="select_account consent")
        except (OAuthError, httpx.HTTPError, ValueError):
            return page("Googlen valtuutusta ei voitu avata. Kokeile myöhemmin tai käytä ZIP-latausta.", status=502)

    @app.get("/oauth/callback")
    async def callback(request: Request):
        token, browser = request.session.get("report"), request.session.get("browser")
        if not token or not browser:
            return page("Valtuutusistunto puuttuu. Avaa Colabin valtuutuslinkki uudelleen.", status=400)
        try:
            credentials = await google.authorize_access_token(request)
            identity = identity_from_token(credentials)
            expires = float(credentials.get("expires_at", time.time() + float(credentials.get("expires_in", 0))))
            if not math.isfinite(expires) or expires <= time.time() + 5:
                raise ValueError("Expired token")
            await run_in_threadpool(request.app.state.store.authorize, token, browser, identity, credentials["access_token"], expires)
        except HTTPException as error:
            return page(str(error.detail), status=error.status_code)
        except (OAuthError, JoseError, httpx.HTTPError, ValueError, TypeError, KeyError):
            return page("Google-valtuutus peruttiin tai sen tarkistus epäonnistui. Mitään ei lähetetty. Avaa Colabin valtuutuslinkki uudelleen.", status=400)
        request.session.clear()
        request.session.update(browser=browser, report=token, csrf=secrets.token_urlsafe(32))
        return RedirectResponse("/review", status_code=303)

    def session_info(request):
        token, browser = request.session.get("report"), request.session.get("browser")
        if not token or not browser:
            raise HTTPException(403, "Avaa Colabin Gmail-valtuutuslinkki ensin.")
        return token, browser

    @app.get("/review")
    async def review(request: Request):
        token, browser = session_info(request)
        store = request.app.state.store
        row = await run_in_threadpool(store.browser_row, token, browser)
        if row["status"] != "authorized":
            return page("Raportin lähetys on jo käsitelty. Tarkista lähetetyt viestit.")
        report = store.report(row)
        sender = store.cipher.decrypt(row["sender"]).decode()
        csrf = html.escape(request.session["csrf"], quote=True)
        extra = (f'<p><b>Lähettäjä:</b> {html.escape(sender)}<br><b>Vastaanottaja:</b> {html.escape(report.recipient)}</p>'
                 f'<h2>Viestin teksti</h2><pre>{html.escape(report.text)}</pre>'
                 '<h2>Kuvaajaliite</h2><img src="/preview.png" alt="Lähetettävä kuvaaja" width="900">'
                 f'<form method="post" action="/send"><input type="hidden" name="csrf" value="{csrf}">'
                 '<button type="submit">Lähetä raportti Gmailista</button></form>'
                 f'<form method="post" action="/cancel"><input type="hidden" name="csrf" value="{csrf}">'
                 '<button type="submit">Peruuta lähetys</button></form>')
        return page("Tarkista lähettäjä, vastaanottaja ja koko sisältö. Valitse sama Google-tili kuin Colabissa. Viesti lähtee vasta Lähetä-painikkeesta.", extra)

    @app.get("/preview.png")
    async def preview(request: Request):
        token, browser = session_info(request)
        store = request.app.state.store
        row = await run_in_threadpool(store.browser_row, token, browser)
        if row["status"] != "authorized":
            raise HTTPException(410, "Esikatselu ei ole enää saatavilla.")
        return Response(base64.b64decode(store.report(row).chart_base64), media_type="image/png", headers=HEADERS)

    async def validate_post(request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024:
                raise HTTPException(413, "Lomake on liian suuri.")
        try:
            supplied = parse_qs(body.decode("ascii"), strict_parsing=True).get("csrf", [""])[0]
        except (UnicodeDecodeError, ValueError):
            supplied = ""
        expected = request.session.get("csrf", "")
        if not supplied or not supplied.isascii() or not expected or not hmac.compare_digest(supplied, expected):
            raise HTTPException(403, "Lähetyksen vahvistus puuttuu tai on virheellinen.")
        return session_info(request)

    @app.post("/send")
    async def send(request: Request):
        token, browser = await validate_post(request)
        store = request.app.state.store
        row = await run_in_threadpool(store.browser_row, token, browser, True)
        if row["status"] != "authorized":
            return page("Lähetys on jo käsitelty tai sen tulos on epävarma. Uutta viestiä ei lähetetty. Tarkista Gmailin Lähetetyt-kansio.")
        status = "delivery_unknown"
        try:
            report = store.report(row)
            sender = store.cipher.decrypt(row["sender"]).decode()
            access = store.cipher.decrypt(row["access_token"]).decode()
            raw = message_raw(report, sender, row["request_key"], urlsplit(config.public_url).hostname, row["created"])
            await gmail_send(access, raw)
            status = "accepted"
        except httpx.HTTPStatusError:
            status = "failed"
        except (httpx.HTTPError, ValueError, InvalidToken, HTTPException):
            pass
        finally:
            await run_in_threadpool(store.finish, row["request_key"], status)
        if status == "accepted":
            return page("Gmail hyväksyi raportin lähetettäväksi valitulta Google-tililtä. Kuvaaja on liitteenä ja raportti viestin tekstissä. Voit sulkea sivun.")
        if status == "failed":
            return page("Gmail ei hyväksynyt lähetystä. Tarkista tilin Gmail-käyttö, lähetyslupa ja kiintiöt. Käytä tarvittaessa ZIP-latausta.", status=502)
        return page("Lähetyksen tulosta ei voitu vahvistaa. Tarkista Gmailin Lähetetyt-kansio ennen uutta pyyntöä. Viestiä ei lähetetä automaattisesti uudelleen.", status=502)

    @app.post("/cancel")
    async def cancel(request: Request):
        token, browser = await validate_post(request)
        store = request.app.state.store
        await run_in_threadpool(store.browser_row, token, browser, "cancel")
        request.session.clear()
        return page("Lähetys peruutettiin tai se oli jo käsitelty. Voit sulkea sivun.")

    return app


app = create_app()
