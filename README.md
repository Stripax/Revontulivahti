# Revontulivahti

Colabissa ajettava versio. Kaupunki, 0–200 km hakusäde ja 1–7 ennustepäivää → valittujen öiden
parhaat tuntijaksot, perustelut, keskustan ja lähialueiden vertailukuvaaja sekä CSV-tulokset.

## Käyttö Google Colabissa

1. Avaa <https://colab.research.google.com/>.
2. Valitse **Tiedosto → Avaa muistikirja → Lähetä** ja valitse `Revontulivahti.ipynb`.
3. Valitse kohdassa **3. Toimitustapa ja ennusteen asetukset** ZIP-tiedosto tai sähköposti.
   Aseta myös kaupunki, säde, ennustepäivien määrä (1–7, oletus 3) ja tarvittaessa maakoodi.
4. Valitse **Suoritusaika → Suorita kaikki**. Usean paikkahakutuloksen tapauksessa
   valitse oikean kaupungin numero tulosteen kysymykseen.
5. Lue arviot ja vastaanota viimeisen solun kautta ZIP-tiedosto tai sähköposti.

Sähköpostivaihtoehto kysyy vastaanottajan osoitteen. Tulosten jälkeen käyttäjä avaa
Gmail-valtuutuslinkin, valitsee Google-tilin ja hyväksyy lähetysluvan. Tarkistussivulla
näkyvät lähettäjä, vastaanottaja, teksti ja kuvaaja. **Lähetä raportti Gmailista** lähettää
raportin valitun tilin Gmailista. Kuvaaja on PNG-liite ja perustelut ovat viestin tekstissä.
Google-tili valitaan erikseen; ohjelma ei voi automaattisesti varmistaa Colabissa käytettyä tiliä.

**Ylläpitäjä:** ota ensin palvelu käyttöön ohjeella [backend/KAYTTOONOTTO.md](backend/KAYTTOONOTTO.md)
ja lisää sen julkinen osoite muistikirjan `SAHKOPOSTIPALVELU_URL`-vakioon. Toteutus sisältää
FastAPI-palvelun, Google OAuth / Gmail API -integraation, salatun SQLite-sisällön ja
Renderin käyttöönottomäärityksen. Käyttöönotto edellyttää Google Cloud -projektia,
Gmail API:n käyttöönottoa, OAuth-sovellusta ja käyttäjän lähetyslupaa.
Palvelua ei ole julkaistu. ZIP-vaihtoehto toimii ilman taustapalvelua.

Lähetysvirheessä tulokset säilyvät Colabin `outputs`-kansiossa, ja toimitustavan voi vaihtaa
ZIP-lataukseen. Sama raportti käsitellään vain kerran; epävarmaa lähetystä ei yritetä
automaattisesti uudelleen. Uusi haku on uusi raportti. Tyhjennä tulosteet ennen muistikirjan jakamista.
OAuth-asiakassalaisuus ja Gmail-käyttöoikeus ovat vain palvelimella. Pysyvää refresh tokenia
ei pyydetä. ZIP toimii ilman Google-valtuutusta tai taustapalvelua.

Muistikirja sisältää kaiken ohjelmakoodin. Muita projektitiedostoja ei tarvitse siirtää Colabiin.
GPU:ta ja API-avaimia ei tarvita. Pisteet eivät ole prosentuaalisia todennäköisyyksiä.
Valosaastetta, saavutettavuutta ja maaston esteitä ei ole mallinnettu. Piste kartalla
on säävertailun koordinaatti, ei tarkistettu havaintopaikka. Kolmannen yön Kp-kattavuus
voi jäädä vajaaksi, ja viikon jakson loppupään öille ei yleensä ole vielä Kp-ennustetta.
Puuttuvaa ennustetta ei täytetä viimeisellä arvolla. Kp:n ulkopuolisille öille näytetään
parhaat ajat pilvisyyden ja pimeyden perusteella merkinnällä **Arvio epävarma**.
Tämä kertoo havainto-olosuhteista; revontulien esiintymistä ei arvioida.
Osittain katetulla yöllä sääperusteiset ajat näytetään erikseen. Sääpisteitä ei verrata
Kp + sää -pisteisiin. Kuvaajassa sääarvio näkyy oranssilla katkoviivalla, ja CSV sisältää
erikseen `score`, `weather_score`, `basis` ja `uncertain` -sarakkeet.

Yön määritelmä, lähteet, pisteytyksen kaavat ja rajoitukset on kuvattu muistikirjassa.
OVATION näytetään vain erillisenä lähitilanteena, eikä sen arvoa uloteta tuleviin öihin.

## Paikallinen kehitys

Python 3.10+:

```sh
python -m pip install -r requirements.txt -r backend/requirements-dev.txt
python -m unittest discover -s tests -v
python build_notebook.py
```

`revontulivahti.py` on ohjelmakoodin lähde. Muutoksen jälkeen `build_notebook.py`
rakentaa itsenäisen muistikirjan uudelleen. `tests/test_forecast.py` tarkistaa muun muassa
ennusteen päättymisen, puuttuvan datan, kesäajan sekä valoisat kesäyöt.

Haku Pythonista:

```python
from revontulivahti import choose_city, run_forecast, show_report, save_outputs
kaupunki = choose_city("Helsinki", "FI")
tulos = run_forecast(kaupunki, 50, days=7)
show_report(tulos)  # Jupyter/Colab
save_outputs(tulos)
```

Open-Meteo: sääennuste ja geokoodaus (paikannimet GeoNames); NOAA SWPC: Kp ja OVATION;
IGRF14: dipolimallin kertoimet; PyEphem: Auringon ja Kuun sijainti.
Open-Meteon avoin rajapinta on ei-kaupalliseen käyttöön sen käyttöehtojen mukaisesti.
