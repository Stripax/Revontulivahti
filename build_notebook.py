"""Luo itsenäinen Colab-muistikirja paikallisesta ohjelmakoodista."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def cell(kind, source, **metadata):
    result = {"cell_type": kind, "metadata": metadata, "source": source.splitlines(keepends=True)}
    if kind == "code":
        result.update(execution_count=None, outputs=[])
    return result


cells = [
    cell("markdown", """# Revontulivahti · 1–7 yön havaintoarvio

Syötä kaupunki, hakusäde ja haluamasi ennusteen pituus (1–7 päivää).
Ohjelma vertailee keskustaa ja 16 lähialueen pistettä, etsii valitun määrän
seuraavien öiden parhaat täydet tunnit ja kertoo perustelut suomeksi.
NOAA:n Kp-ennuste voi loppua ennen valitun jakson loppua. Näille öille näytetään
parhaat ajat pilvisyyden ja pimeyden perusteella merkinnällä **Arvio epävarma**.
Tällöin revontulien esiintymistä ei arvioida.

**Käyttö:** valitse Colabissa **Suoritusaika → Suorita kaikki** (Runtime → Run all).
Muokkaa ensin kohdan **3. Toimitustapa ja ennusteen asetukset** lomaketta. Valitse
ZIP-lataus tai sähköposti. Sähköpostissa kuvaaja on PNG-liite ja arviot ovat suoraan
viestin tekstissä. Vastaanottajan osoite kysytään ajon alussa, muita sähköpostiasetuksia
ei kysytä. Tulosten jälkeen avataan Gmail-valtuutuslinkki, valitaan Google-tili ja
hyväksytään lähetyslupa. Tarkistussivulla painetaan **Lähetä raportti Gmailista**. Jos haku löytää
useita samannimisiä paikkoja, valitse oikea numero suorituksen aikana.
GPU:ta tai API-avaimia ei tarvita. Kaupungin nimi lähetetään Open-Meteon
paikkahakuun ja vertailupisteiden koordinaatit sen säärajapintaan.

**Tulkinta:** 0–100 pistettä on kokeellinen järjestysasteikko, ei todennäköisyys.
Heikoksi arvioidun yön paras tunti voi silti olla huono havaintohetki.
Ohjelma arvioi paljain silmin havaitsemisen edellytyksiä pimeällä havaintopaikalla.
"""),
    cell("code", '#@title 1. Asenna riippuvuudet (CPU riittää)\n%pip -q install "numpy>=1.26,<3" "pandas>=2.2,<4" "matplotlib>=3.8,<4" "ephem==4.2.1" "tzdata>=2025.2"\n'),
    cell("code", "#@title 2. Ohjelmakoodi – suorita tämä solu\n" + (ROOT / "revontulivahti.py").read_text(encoding="utf-8"), cellView="form"),
    cell("code", '''#@title 3. Toimitustapa ja ennusteen asetukset
toimitustapa = "ZIP-tiedosto" #@param ["ZIP-tiedosto", "Sähköposti"]
kaupunki = "Helsinki" #@param {type:"string"}
hakusade_km = 50 #@param {type:"number"}
ennustepaivat = 3 #@param {type:"slider", min:1, max:7, step:1}
maakoodi = "FI" #@param {type:"string"}
# Maakoodi: FI = Suomi, SE = Ruotsi, NO = Norja. Tyhjä = koko maailma.
# Säde on linnuntietä; 0 vertailee vain keskustaa, enimmäisarvo on 200 km.
# Ennustepäivät tarkoittavat havaintoöiden määrää (1–7).
# Ylläpitäjä asettaa tämän kerran ennen muistikirjan jakamista. Osoite ei ole salaisuus.
SAHKOPOSTIPALVELU_URL = ""
vastaanottaja = None
if toimitustapa == "Sähköposti":
    while True:
        try:
            vastaanottaja = validate_email(input("Vastaanottajan sähköpostiosoite: "))
            break
        except ValueError as virhe:
            print(virhe)
elif toimitustapa != "ZIP-tiedosto":
    raise ValueError("Valitse toimitustavaksi ZIP-tiedosto tai Sähköposti.")
'''),
    cell("code", '''#@title 4. Hae tuore ennuste ja näytä parhaat havaintoajat
# Tyhjennä vanha tulos ennen hakua: epäonnistunut uusi haku ei saa tallentaa vanhaa tulosta.
tulos = None
try:
    paivat = validate_days(ennustepaivat)
    sade = float(hakusade_km)
    if not math.isfinite(sade) or not 0 <= sade <= 200:
        raise ValueError("Hakusäteen pitää olla 0–200 km.")
    paikka = choose_city(kaupunki, maakoodi)
    tulos = run_forecast(paikka, sade, days=paivat)
    show_report(tulos)
    print("\\n" + ovation_context(paikka))
except (ValueError, RuntimeError, KeyError) as virhe:
    print(f"Arviota ei voitu muodostaa: {virhe}")
    print("Tarkista syötteet ja verkkoyhteys. Kokeile hetken kuluttua uudelleen.")
'''),
    cell("code", '''#@title 5. Kuvaaja ja tulosten toimitus
if tulos is not None:
    import matplotlib.pyplot as plt
    import shutil
    kuva = plot_report(tulos)
    plt.show()
    kansio = save_outputs(tulos)
    kuva_polku = kansio / "havaintoarvio.png"
    kuva.savefig(kuva_polku, dpi=150)
    if toimitustapa == "ZIP-tiedosto":
        paketti = shutil.make_archive("revontulivahti_tulokset", "zip", kansio)
        print("Tulokset tallennettu:", paketti)
        try:
            from google.colab import files
            files.download(paketti)
        except ImportError:
            from IPython.display import FileLink, display
            display(FileLink(paketti))
    elif toimitustapa == "Sähköposti":
        try:
            vastaus = send_via_backend(tulos, vastaanottaja, kuva_polku, SAHKOPOSTIPALVELU_URL)
            if vastaus["status"] == "authorization_required":
                from IPython.display import Markdown, display
                display(Markdown(f"[Valtuuta Gmail-lähetys ja tarkista raportti]({vastaus['authorization_url']})"))
                print("Avaa linkki. Valitse sama Google-tili kuin Colabissa ja hyväksy lähetyslupa.")
                print("Tarkista sisältö ja paina Lähetä raportti Gmailista. Linkki on voimassa 30 minuuttia.")
            else:
                print("Palvelu on jo hyväksynyt tämän raportin lähetettäväksi osoitteeseen", vastaanottaja)
            print("Raportissa kuvaaja on liitteenä ja arviot viestin tekstissä. Tarkista myös roskaposti.")
        except (ValueError, RuntimeError, OSError) as virhe:
            print("Sähköpostitoimitus ei onnistunut:", virhe)
            print("Tulokset ovat silti tallessa Colabin outputs-kansiossa. Voit valita ZIP-tiedoston "
                  "kohdassa 3 ja suorittaa sen sekä tämän solun uudelleen.")
    else:
        print("Tuntematon toimitustapa. Valitse toimitustapa kohdassa 3.")
else:
    print("Suorita ensin onnistunut ennustehaku kohdassa 4.")
'''),
    cell("markdown", """## Tulosten toimitus

**ZIP-tiedosto:** selaimeen ladataan kuvaaja, tekstiraportti, tuntiarviot CSV-muodossa,
vertailupisteet ja haun tiedot. Sähköpostiasetuksia ei kysytä.

**Sähköposti Gmailista:** syötä vastaanottajan osoite. Tulosten valmistuttua avaa
**Valtuuta Gmail-lähetys ja tarkista raportti** -linkki. Valitse Googlen omassa ikkunassa
sama Google-tili kuin Colabissa ja hyväksy sähköpostin lähetyslupa.
Tarkista lähettäjä, vastaanottaja, viestin teksti ja kuvaaja ja paina
**Lähetä raportti Gmailista**. Viesti lähtee valitun tilin Gmailista: raportti on
viestin tekstissä ja kuvaaja PNG-liitteenä. Voit myös peruuttaa lähetyksen.
Linkki vanhenee 30 minuutissa. Ohjelma ei voi automaattisesti varmistaa, että
valitsit Colabin käyttämän tilin; tarkista lähettäjän osoite itse.

Ylläpitäjän pitää ensin ottaa erillinen taustapalvelu käyttöön projektin
`backend/KAYTTOONOTTO.md`-ohjeella ja asettaa sen julkinen HTTPS-osoite kohdan 3
`SAHKOPOSTIPALVELU_URL`-vakioon. Tyhjä osoite tarkoittaa, ettei sähköpostia ole vielä otettu käyttöön.
Ylläpitäjä määrittää Google Cloud -projektin, Gmail API:n ja OAuth-sovelluksen.
OAuth-asiakassalaisuus ja lyhytikäiset käyttöoikeudet ovat vain palvelimella;
niitä ei siirretä Colabiin. Tilillä pitää olla Gmail käytössä ja lähetyslupa sallittuna.

Samalle raportille käytetään samaa pyyntötunnistetta. Solun uusinta tai Lähetä-painikkeen
toinen painallus ei lähetä jo käsiteltyä raporttia uudelleen. Uusi ennustehaku tekee uuden
raportin. Gmailin hyväksyntä ei takaa sähköpostin perillemenoa.
Raportti ja kuvaaja siirtyvät HTTPS-yhteydellä taustapalvelulle ja sen kautta Gmail API:in.
Palvelu säilyttää odottavan raportin enintään noin 31 minuuttia käynnissä ollessaan;
lähetyksen tai peruutuksen jälkeen sisältö ja tallennettu käyttöoikeus poistetaan heti.
Pysyvää refresh tokenia ei pyydetä tai säilytetä. Lähetysrajoja varten säilytetään
hajautettuja tunnisteita ja tilatietoja enintään vuorokausi. Isännöinnin varmuuskopioiden
ja Gmailin säilytysajat riippuvat tilien asetuksista.
Osoitteet voivat näkyä syöttökenttien tulosteissa: tyhjennä tulosteet ennen muistikirjan jakamista.
Jos lähetys epäonnistuu, tulokset säilyvät Colabin `outputs`-kansiossa ja voit valita ZIP-latauksen.

## Miten arviota käytetään ja mitä se tarkoittaa?

- **Yö** on paikallisen keskipäivän ja seuraavan keskipäivän välinen jakso.
  Ennustepäivien valinta (1–7, oletus 3) määrää raportin öiden lukumäärän.
  Jos ohjelma käynnistetään ennen puoltapäivää ja on edelleen pimeää,
  meneillään olevan yön jäljellä olevat täydet tunnit otetaan mukaan.
  Kesäaikamuutokset käsitellään IANA-aikavyöhykkeillä. Tulosten aikavyöhyke on kaupungin vyöhyke.
- **Tunnit** ovat tuntiennusteen näytteisiin perustuvia vertailujaksoja.
  Kolme vaihtoehtoa valitaan vähintään kahden tunnin välein. Tarkkuus ei tarkoita,
  että yksittäinen revontulipurkaus voitaisiin ennustaa tunnilleen.
- **Hakusäde** tuottaa keskustan sekä kahdeksan ilmansuuntaa puolella säteellä ja
  täydellä säteellä. Se on otanta, ei kaikkien lähialueiden kattava optimointi.
  Suositeltu koordinaatti ei ole tarkistettu retkikohde: se voi olla vedessä,
  yksityisellä alueella tai tien ulkopuolella. Reittiä ja ajoaikaa ei lasketa.
- **Pimeys:** Aurinko on alle −6° koko tunnin kolmessa tarkistuspisteessä.
  Hämärä vähentää pisteitä; täysi pimeyspaino saavutetaan −18°:ssa.
  Nämä ovat ensimmäisen version omia rajoja.
- **Avaruussää:** kolmen tunnin Kp-lohkot säilytetään ilman interpolointia.
  Vain `estimated`- ja `predicted`-rivit kelpaavat tulevan ajan arvioksi.
  Aineiston viimeisen havainnon/arvion pitää olla enintään 12 tuntia vanha.
  Lohkon jälkeen ei jatketa viimeistä arvoa. Jo kolmas yö voi jäädä osittain kattamatta,
  ja viikon jakson loppupään öille ei yleensä ole vielä Kp-ennustetta.
  Näille öille lasketaan erilliset sääpisteet pilvisyydestä ja pimeydestä.
  Arvio merkitään epävarmaksi eikä se ennusta revontulien esiintymistä.
  Jos Kp kattaa vain osan yöstä, Kp:n ulkopuoliset havaintoajat näytetään omana ryhmänään:
  sääpisteitä ja Kp + sää -pisteitä ei järjestetä samaan paremmuusjärjestykseen.
- **Magneettinen sijainti:** IGRF14:n ensimmäisen asteen dipoliarvio (2025–2030).
  Se ei vastaa tarkkaa AACGM-leveyttä. NOAA:n likimääräistä suhdetta
  `revontulivyöhykkeen eteläreuna ≈ 66° − 2 × Kp` käytetään vain heuristiikkana.
  Myös horisontissa näkymisen vaikutus on karkea oletus. Suurten leveysasteiden
  napakalottia tai revontulivyöhykkeen todellista muotoa ei mallinneta.
- **Sää:** pilvisyys, pilvikerrokset, näkyvyys ja sade heikentävät pisteitä.
  Pilvet ja näkyvyys ovat jakson alun tuntinäytteitä, eivät lupaus koko tunnin säästä.
  Sademäärä kohdistetaan jaksolle käyttämällä seuraavan aikaleiman edeltävän tunnin kertymää.
  Pilvien peittävyydestä ei voi laskea suoraan näkemistodennäköisyyttä.
  Näkyvyys on säämallin arvio näkyvyydestä maanpinnan lähellä, ei tähtitieteellinen läpinäkyvyysmittari.
  Lämpötila ja tuuli tallennetaan aineistoon, mutta ne eivät muuta näkyvyyspisteitä.
- **Kuu:** kirkas korkealla oleva Kuu vähentää pisteitä enintään 25 %.
  Paino on kokeellinen. Valosaastetta, lumesta heijastuvaa valoa ja maaston esteitä ei lasketa.
- **OVATION** näytetään vain erillisenä tuoreena lähitilanteena. Sitä ei käytetä
  tulevien öiden pisteissä. Puuttuva OVATION ei estä valitun jakson arviota.
- **Epävarmuus:** puuttuva Kp jättää Kp + sää -pisteet tyhjiksi, mutta mahdollistaa
  epävarman pilvisyys- ja pimeysarvion. Puuttuva tai kelvoton pilvisyystieto jättää myös sääpisteet tyhjiksi.
  Hyvä pistemäärä
  tarkoittaa mallin mielestä suotuisia edellytyksiä, ei lupausta revontulista.
  Malli vaatii havaintoaineistolla testaamista ennen prosentuaalisten todennäköisyyksien esittämistä.

### Pisteytys, avoimesti

`pisteet = 100 × aktiivisuus × pimeys × taivas × näkyvyys × sade × kuu`

Kun Kp puuttuu tai on vanhentunut, käytetään erillistä vertailua:
`sääpisteet = 100 × pimeys × taivas`. Se ei sisällä Kp:tä, näkyvyyttä,
sadetta tai kuunvaloa. Kuvaajassa tämä arvio näkyy oranssilla katkoviivalla.

Kaikki tekijät ovat välillä 0–1:

```
raja = 66 − 2 × Kp
etäisyys = max(0, raja − |magneettinen leveys|)
aktiivisuus = clip(1 − etäisyys / 7, 0, 1)
pimeys = clip((−Auringon korkein korkeus − 6) / 12, 0, 1)
pilvieste = max(kokonaispilvet, alapilvet, keskipilvet, 0.6 × yläpilvet)
taivas = (1 − pilvieste / 100)^1.3
näkyvyys = clip(näkyvyysmetrit / 15000, 0, 1)
sade = exp(−0.7 × sademäärä_mm)
kuu = 1 − 0.25 × valaistu_osuus × max(0, sin(Kuun korkeus))
```

Luokat: **hyvät ≥ 65**, **kohtalaiset ≥ 35**, muuten **heikot**.
Puuttuva Kp tuottaa **arvio epävarma** -merkinnän. Liian valoisa tunti saa nolla pistettä
eikä sitä suositella parhaaksi havaintoajaksi. CSV-tiedoston `score` tarkoittaa
Kp + sää -pisteitä, `weather_score` pilvisyys- ja pimeyspisteitä, `basis` arvion perustaa
ja `uncertain` sitä, puuttuuko Kp:n ja sään kokonaisarvio.
Painot ja rajat ovat kehityksen lähtöarvoja, eivät tutkimuksessa validoitu malli.

### Tietolähteet ja jatkokehitys

- [NOAA Kp JSON](https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json)
- [NOAA:n havainto-ohje ja Kp:n rajoitukset](https://www.swpc.noaa.gov/content/tips-viewing-aurora)
- [NOAA OVATION](https://www.swpc.noaa.gov/products/aurora-30-minute-forecast)
- [Open-Meteon säärajapinta](https://open-meteo.com/en/docs)
- [Open-Meteon paikkahaku](https://open-meteo.com/en/docs/geocoding-api) (paikannimet: GeoNames)
- [IGRF14-kertoimet](https://www.ngdc.noaa.gov/IAGA/vmod/coeffs/igrf14coeffs.txt)
- [PyEphem](https://rhodesmill.org/pyephem/) (Aurinko ja Kuu)

Open-Meteon avoimen rajapinnan käyttö on rajattu ei-kaupalliseen käyttöön sen
kulloistenkin käyttöehtojen ja kutsurajojen mukaisesti. Tarkista ehdot ennen julkaisemista.
Kaupallinen käyttö voi edellyttää eri rajapintaa ja tilausta.

Seuraavat hyödylliset parannukset: tarkempi AACGM-sijainti, paikalliset magneettikenttähavainnot,
valosaasteaineisto, käyttäjän omat havaintopaikat ja toteutuneisiin havaintoihin perustuva kalibrointi.
Colab tekee uuden haun vain soluja suoritettaessa; se ei ole jatkuvasti taustalla toimiva hälytin.
"""),
]

notebook = {"nbformat": 4, "nbformat_minor": 5, "cells": cells,
            "metadata": {"colab": {"name": "Revontulivahti.ipynb", "provenance": []},
                         "kernelspec": {"name": "python3", "display_name": "Python 3"},
                         "language_info": {"name": "python", "version": "3.12"}}}
for index, item in enumerate(cells):
    item["id"] = f"revontulivahti-{index:02d}"
(ROOT / "Revontulivahti.ipynb").write_text(json.dumps(notebook, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print("Luotu Revontulivahti.ipynb (kaikki ohjelmakoodi mukana).")
