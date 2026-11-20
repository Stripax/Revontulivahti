# Gmail-lähetyksen käyttöönotto

Tässä versiossa raportti lähetetään käyttäjän valitsemalta Google-tililtä Gmail API:n
kautta. Käyttäjä syöttää Colabiin vastaanottajan osoitteen, avaa tulosten jälkeen
valtuutuslinkin, valitsee Google-tilin ja antaa lähetysluvan. Tarkistussivulla näkyvät
lähettäjä, vastaanottaja, koko viestin teksti ja kuvaajaliite. **Lähetä raportti Gmailista**
lähettää yhden viestin. **Peruuta lähetys** poistaa odottavan raportin ja sen käyttöoikeuden.

ZIP-lataus toimii ilman palvelua, Google Cloud -projektia tai Gmail-valtuutusta.
Toteutus on valmis käyttöönottoa varten, mutta sitä ei ole julkaistu eikä yhdistetty
Google-tiliisi. Ohjeen tiliasetukset tehdään kerran ylläpitäjän omilla tileillä.

## 1. Luo Google Cloud -projekti ja OAuth-sovellus

1. Avaa [Google Cloud Console](https://console.cloud.google.com/) ja luo projekti.
2. Avaa **APIs & Services → Library**, etsi **Gmail API** ja ota se käyttöön.
3. Avaa **Google Auth Platform** ja määritä sovelluksen nimi, esimerkiksi
   `Revontulivahti`, tukisähköpostiosoite ja ylläpitäjän yhteystiedot.
4. Valitse **Audience**:
   - Henkilökohtaiselle Gmail-tilille ja eri organisaatioiden käyttäjille **External**.
   - **Internal** vain, jos Workspace-organisaatiosi sallii jaossa käytettävät tilit.
5. Aloita **Testing**-tilassa ja lisää **Test users** -luetteloon ne Google-tilit,
   joilta testissä lähetetään viestejä. Vastaanottajan ei tarvitse olla testikäyttäjä.
6. Lisää **Data Access** -kohtaan vain:

   | OAuth-oikeus | Tarkoitus |
   |---|---|
   | `openid` | Google-tilin tunnistaminen |
   | `email` / `https://www.googleapis.com/auth/userinfo.email` | Vahvistettu lähettäjäosoite |
   | `https://www.googleapis.com/auth/gmail.send` | Viestin lähettäminen käyttäjän puolesta |

   Koodin OAuth-scope on `openid email https://www.googleapis.com/auth/gmail.send`.
   Saapuneita viestejä, postilaatikon sisältöä tai viestien poistamisoikeutta ei pyydetä.
7. Luo **Clients → Create client → Web application**. Tämä on palvelimella toimiva
   OAuth-sovellus: älä valitse Desktop application -tyyppiä.
8. Kopioi **Client ID** ja **Client secret** palvelimen salaisiin ympäristöasetuksiin.
   Lisää täsmällinen paluuosoite kohdassa 3, kun palvelun julkinen osoite on tiedossa.

Käyttäjän tilillä pitää olla Gmail käytössä. Pelkkä Colab-kirjautuminen ei anna
lähetyslupaa. Google-tili valitaan lupaikkunassa; ohjelma ei voi automaattisesti
todistaa, että se on sama tili kuin Colabin käyttöliittymässä. Tarkista lähettäjä itse.

## 2. Julkaise taustapalvelu Renderiin

Mukana oleva `render.yaml` julkaisee Python Web Servicen **maksullisella starter-palvelulla
ja 1 GB:n pysyvällä levyllä**. Tarkista Renderin ilmoittama hinta ennen palvelun luomista.
Pysyvä levy säilyttää keskeneräiset pyynnöt ja kaksoislähetysten eston uudelleenkäynnistyksissä.

1. Vie `backend`-hakemisto ja juuren `render.yaml` omaan Git-repositorioon.
   Voit viedä koko projektin, kun `.gitignore` on käytössä. Älä vie avaimia,
   `.env`-tiedostoja, `.deps`-kansiota tai tulosraportteja.
2. Avaa Renderissä **New → Blueprint** ja valitse repositorio.
3. Anna pyydetyt asetukset:

   | Muuttuja | Arvo |
   |---|---|
   | `GOOGLE_CLIENT_ID` | Google Cloudin Web application -asiakkaan tunniste |
   | `GOOGLE_CLIENT_SECRET` | Saman asiakkaan salainen avain |
   | `TOKEN_SECRET` | Blueprint generoi satunnaisen arvon automaattisesti |
   | `DATABASE_PATH` | `/var/data/gmail.sqlite3` (Blueprint asettaa) |

4. Käynnistä julkaisu. Tallenna palvelun HTTPS-osoite, esimerkiksi
   `https://oma-palvelu.onrender.com`.
5. Avaa `/health`. Odotettu vastaus on `{"status":"ok"}`.

Palvelu saa osoitteensa Renderin `RENDER_EXTERNAL_URL`-muuttujasta. Oman verkkotunnuksen
tai muun isännöinnin tapauksessa aseta `PUBLIC_BASE_URL` julkiseen HTTPS-juuriosoitteeseen.
Älä lisää siihen loppupolkua. Oma lähettäjäverkkotunnus tai sähköpostin DNS-asetukset
eivät ole Gmail-lähetyksen edellytys; OAuth-sovelluksen julkinen tarkistus voi kuitenkin
edellyttää omistamiesi sovellusverkkotunnusten vahvistamista Googlelle.

Jos määrität palvelun käsin, asetukset ovat:

| Asetus | Arvo |
|---|---|
| Root Directory | `backend` |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1 --no-access-log` |
| Health Check Path | `/health` |
| Pysyvän levyn liitospolku | `/var/data` |

Pidä palvelu yhdessä instanssissa ja yhdessä workerissa. Käsin asennettaessa luo myös
vähintään 32-merkkinen `TOKEN_SECRET`, esimerkiksi komennolla:

```sh
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Älä muuta `TOKEN_SECRET`-arvoa normaalin julkaisun yhteydessä: sillä suojataan istunnot,
raporttilinkit ja tietokannan salattu sisältö. Vaihto mitätöi keskeneräiset valtuutukset.

## 3. Lisää Googlen paluuosoite

Avaa Google Auth Platform → **Clients** → luomasi Web application -asiakas.
Lisää kohtaan **Authorized redirect URIs** täsmälleen:

```text
https://oma-palvelu.onrender.com/oauth/callback
```

Käytä samaa juuriosoitetta kuin palvelun `PUBLIC_BASE_URL` tai automaattinen Render-osoite.
HTTPS, palvelimen nimi ja polku `/oauth/callback` täytyy kirjoittaa täsmälleen samoin.
Jos vaihdat palvelun osoitetta, päivitä sekä palvelin että Google Cloudin paluuosoite.
Tämän toteutuksen palvelinvaltuutus ei tarvitse Colabia JavaScript origin -luetteloon.

**Yleiset virheet:**

- `redirect_uri_mismatch`: tarkista yllä oleva paluuosoite ja käytössä oleva Client ID.
- `access_denied`: käyttäjä peruutti luvan, ei ole sovelluksen testikäyttäjä tai organisaatio estää luvan.
- Gmail ei hyväksy lähetystä: varmista Gmail API:n käyttöönotto, tilin Gmail-käyttö,
  `gmail.send`-lupa, Workspace-ylläpitäjän sallimat sovellukset ja tilin lähetyskiintiöt.
- Lupaikkuna palaa mutta istunto puuttuu: käytä samaa selainta, salli palvelun evästeet
  ja varmista, että palvelun osoite on HTTPS. Avaa Colabin valtuutuslinkki uudelleen.

## 4. Liitä palvelu Colabiin

Muuta `Revontulivahti.ipynb`-muistikirjan kohdassa **3. Toimitustapa ja ennusteen asetukset**:

```python
SAHKOPOSTIPALVELU_URL = "https://oma-palvelu.onrender.com"
```

Tallenna muistikirja ennen jakamista. Tähän tulee vain julkinen osoite, ei Client secretiä,
käyttöoikeustunnusta tai muuta salasanaa. Lähdekoodia ylläpidettäessä päivitä sama vakio
`build_notebook.py`-tiedostoon ja aja `python build_notebook.py`.
Tyhjällä osoitteella sähköposti ilmoittaa puuttuvasta käyttöönotosta; ZIP toimii silti.

## 5. Tee ensimmäinen koetesti

1. Valitse Colabissa **Sähköposti**, anna oma vastaanottajaosoitteesi ja suorita ennuste.
2. Avaa lopputuloksissa **Valtuuta Gmail-lähetys ja tarkista raportti**.
3. Valitse sama Google-tili kuin Colabissa ja hyväksy Gmailin lähetyslupa.
4. Tarkista palvelun sivulla lähettäjän osoite, vastaanottaja, raportti ja kuvaaja.
5. Paina **Lähetä raportti Gmailista**.
6. Tarkista vastaanottajan viesti ja lähettäjän Gmailin **Lähetetyt**. Raportin pitää olla
   viestin tekstinä ja kuvaajan yhtenä PNG-liitteenä. Testaa lopuksi myös ZIP-lataus.

Oikeaa Google-tilin valtuutusta ja oikeaa sähköpostitoimitusta ei ole testattu tileilläsi.
Automaattiset testit simuloivat Googlen vastaukset, mutta käyttävät oikeaa OAuth-kirjastoa
allekirjoituksen, state-arvon, PKCE:n, nonce-arvon ja vastaanottavan sovelluksen tunnisteen tarkistuksissa.

## Julkinen jako ja tietojen käsittely

`gmail.send` on Googlen luokittelema arkaluonteinen käyttöoikeus. Testikäyttö ja
oman Workspace-organisaation sisäinen käyttö eroavat julkisesta jaosta.
Ennen laajaa julkistamista määritä Google Auth Platformissa vaaditut tuotantoasetukset,
sovelluksen etusivu, tietosuojaseloste ja yhteystiedot. Google voi edellyttää sovelluksen
tarkistusta. Käytä Googlen ohjetta oman käyttötapauksesi vaatimusten selvittämiseen.

- Palvelu pyytää vain `openid`, `email` ja `gmail.send`-oikeudet. Se ei lue postilaatikkoa.
- OAuthin Client secret on vain palvelimella. Käyttäjän salasanaa ei käsitellä sovelluksessa.
- Raportti, vastaanottaja, kuvaaja, lähettäjä ja lyhytikäinen Gmail-käyttöoikeus salataan
  tietokantaan Fernet-salauksella. Käyttöoikeutta ei tallenneta selaimen evästeeseen tai Colabiin.
- Valtuutus käyttää selaimeen sidottua OAuth-statea, OpenID Connect -noncea ja PKCE:tä.
  Lähetyspainike on suojattu istuntokohtaisella CSRF-tunnisteella. Lähetys on sidottu
  juuri siihen Google-tiliin ja selainistuntoon, jolla raportti valtuutettiin.
- Käyttöoikeus koskee yhtä raporttia sovelluksen sisällä. `access_type=online` tarkoittaa,
  ettei palvelu pyydä pysyvää refresh tokenia. Se ei tee taustalähetyksiä istunnon jälkeen.
- Googlelle annettu sovelluslupa voi silti jäädä Google-tilin asetuksiin. Tallennetun
  tunnuksen poistaminen palvelimelta ei peru itse sovelluslupaa. Käyttäjä voi poistaa
  sovelluksen oikeudet [Google-tilinsä yhteyksistä](https://myaccount.google.com/connections).
- Raporttilinkki vanhenee 30 minuutissa. Sisältö ja käyttöoikeus poistetaan heti
  lähetyksen tai peruutuksen jälkeen, tai viimeistään vanhentumista seuraavassa minuutin
  välein ajettavassa siivouksessa palvelun ollessa käynnissä. Tietokannan SQL-poistot ovat
  loogisia: levysivut ja isännöinnin varmuuskopiot voivat säilyttää vanhempia salattuja tietoja.
- Hajautettuja tunnisteita, tilaa ja aikaleimoja säilytetään enintään vuorokausi
  kaksoislähetysten estoa ja kiintiöitä varten (siivous noin minuutin välein).
- Oletusrajat: 30 uutta raporttipyyntöä tunnissa, 100 vuorokaudessa, 3 samalle vastaanottajalle
  tunnissa ja 10 lähetysyritystä samalta Google-tililtä tunnissa. Asetukset ovat `HOURLY_LIMIT`,
  `DAILY_LIMIT`, `RECIPIENT_HOURLY_LIMIT` ja `SENDER_HOURLY_LIMIT`. Lisäksi Gmailin omat rajat pätevät.
- Julkiseen palveluun voi tulla turhia raporttipyyntöjä, mutta se ei lähetä mitään ilman
  Google-tilin valtuutusta ja lähetyspainikkeen painamista. Suuressa jaossa lisää tarvittaessa
  palveluntarjoajan liikenteensuodatus tai käyttäjäkohtainen pääsynhallinta.
- Sama raportti lähetetään vain kerran palvelun kautta. Gmail API ei tarjoa tälle kutsulle
  idempotenssiavainta: jos yhteys katkeaa lähetyksen aikana, palvelu ei yritä automaattisesti
  uudelleen. Tarkista Gmailin Lähetetyt ennen uuden ennustehaun tekemistä. Uusi haku on uusi raportti.
- Palvelun hyväksyntä tarkoittaa Gmail API:n hyväksyntää, ei todistusta lopullisesta perillemenosta.
- Kuvaajan raja on 2,5 MB ja koko JSON-pyynnön 4 MB. PNG:n rakenne ja mitat tarkistetaan.
- Pyyntöloki on pois päältä, jotta OAuth-code ja raporttilinkit eivät tallennu siihen.
  Tarkista erikseen isännöinnin mahdollisten välityspalvelinlokien käsittely.
- Tyhjennä Colabin tulosteet ennen muistikirjan jakamista: vastaanottaja ja raporttilinkki voivat näkyä niissä.

## Päivitys aiemmasta taustapalvelusta

Korvaa palvelimen koodi ja käyttöönottomääritys tällä paketilla. Poista aiemman
sähköpostipalvelun API-avain ja kiinteä lähettäjäosoite palvelimen ympäristöstä ja
peru tarpeettomaksi jäänyt avain palveluntarjoajan hallinnassa.
Uusi tietokanta on `/var/data/gmail.sqlite3`. Aiemmat raporttilinkit eivät toimi tässä versiossa.
Vanhan palvelun tietokannan voi poistaa levystä ylläpitäjän oman säilytyskäytännön mukaan.
Muita sähköpostipalveluja tai SMTP-kirjautumista ei enää käytetä sovelluksessa.

## Paikalliset testit

```sh
python -m pip install -r requirements.txt -r backend/requirements-dev.txt
python build_notebook.py
python -m unittest discover -s tests -v
```

Testit eivät tarvitse oikeita tunnuksia eivätkä lähetä viestejä. OAuth-testit käyttävät
tilapäisiä RSA-avaimia ja simuloituja Google-päätepisteitä.

## Viralliset ohjeet

- [Gmail API: Pythonin käyttöönotto](https://developers.google.com/workspace/gmail/api/quickstart/python)
- [Google OAuth palvelinsovellukselle](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Gmailin käyttöoikeudet](https://developers.google.com/workspace/gmail/api/auth/scopes)
- [Arkaluonteisten käyttöoikeuksien tarkistus](https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification)
- [Gmail-viestin lähetys](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send)
- [Authlibin Starlette-integraatio](https://docs.authlib.org/en/v1.7.0/oauth2/client/web/starlette.html)
- [Render: FastAPI](https://render.com/docs/deploy-fastapi)
- [Render: pysyvät levyt](https://render.com/docs/disks)
