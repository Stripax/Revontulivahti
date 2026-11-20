"""Revontulivahti: läpinäkyvä, kokeellinen 1–7 yön havaintoarvio."""
from __future__ import annotations

import json
import base64
import hashlib
import math
import re
import time
from email.headerregistry import Address
from email.errors import HeaderParseError
from dataclasses import dataclass
from datetime import datetime, date, time as clock_time, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import ephem
import numpy as np
import pandas as pd

UTC = timezone.utc
KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
OVATION_URL = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"
WEATHER_VARS = ["cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
                "visibility", "precipitation", "weather_code", "temperature_2m", "wind_speed_10m"]
REQUIRED_WEATHER = WEATHER_VARS[:6]


def get_json(url, params=None):
    """Aikakatkaisu ja rajattu uudelleenyritys; ei hiljaista vanhaa välimuistia."""
    target = url + ("?" + urlencode(params) if params else "")
    for attempt in range(3):
        try:
            req = Request(target, headers={"User-Agent": "Revontulivahti/0.1 (educational notebook)"})
            with urlopen(req, timeout=40) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError(f"Tietolähde vastasi HTTP {exc.code}: {url}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            if attempt == 2:
                raise RuntimeError(f"Tietolähteeseen ei saatu yhteyttä: {url}: {exc}") from exc
        time.sleep(1 + attempt)
    raise RuntimeError("Tietojen haku epäonnistui.")


def search_city(name, country_code="FI"):
    if len(name.strip()) < 2:
        raise ValueError("Anna vähintään kaksikirjaiminen kaupungin nimi.")
    params = {"name": name.strip(), "count": 10, "language": "fi", "format": "json"}
    if country_code.strip():
        params["countryCode"] = country_code.strip().upper()
    results = get_json(GEO_URL, params).get("results", [])
    if not results:
        raise ValueError("Kaupunkia ei löytynyt. Tarkista nimi ja maakoodi (esim. FI).")
    return results


def choose_city(name, country_code="FI"):
    results = search_city(name, country_code)
    for index, item in enumerate(results, 1):
        print(f"{index}: {item['name']}, {item.get('admin1', '')}, {item.get('country', '')} "
              f"({item['latitude']:.3f}, {item['longitude']:.3f})")
    choice = 1
    if len(results) > 1:
        text = input("Valitse kaupungin numero [1]: ").strip()
        choice = int(text) if text else 1
    if not 1 <= choice <= len(results):
        raise ValueError("Kaupungin numero ei ole luettelossa.")
    city = results[choice - 1]
    if not city.get("timezone"):
        raise ValueError("Kaupungin aikavyöhyke puuttuu.")
    return city


def distance_km(lat1, lon1, lat2, lon2):
    a, b = np.radians(lat1), np.radians(lat2)
    dlat, dlon = b - a, np.radians(np.asarray(lon2) - lon1)
    h = np.sin(dlat / 2)**2 + np.cos(a) * np.cos(b) * np.sin(dlon / 2)**2
    return 6371.0088 * 2 * np.arcsin(np.sqrt(np.clip(h, 0, 1)))


def destination(lat, lon, radius_km, bearing):
    p, l, b = map(math.radians, (lat, lon, bearing))
    d = radius_km / 6371.0088
    q = math.asin(math.sin(p) * math.cos(d) + math.cos(p) * math.sin(d) * math.cos(b))
    m = l + math.atan2(math.sin(b) * math.sin(d) * math.cos(p), math.cos(d) - math.sin(p) * math.sin(q))
    return math.degrees(q), (math.degrees(m) + 180) % 360 - 180


def sampling_points(lat, lon, radius_km):
    if not math.isfinite(radius_km) or not 0 <= radius_km <= 200:
        raise ValueError("Hakusäteen pitää olla 0–200 km. Säde on linnuntie-etäisyys.")
    rows = [{"site": "Keskusta", "latitude": lat, "longitude": lon, "distance_km": 0.0}]
    if radius_km:
        directions = ["P", "Ko", "I", "Ka", "E", "Lo", "L", "Lu"]
        for fraction in (0.5, 1.0):
            for bearing, label in zip(range(0, 360, 45), directions):
                p, q = destination(lat, lon, radius_km * fraction, bearing)
                rows.append({"site": f"{label} {radius_km * fraction:g} km", "latitude": p,
                             "longitude": q, "distance_km": radius_km * fraction})
    return pd.DataFrame(rows)


def astronomy(lat, lon, timestamp):
    observer = ephem.Observer()
    observer.lat, observer.lon = str(lat), str(lon)
    observer.pressure = 0  # Geometrinen korkeus; ilmakehän taittumista ei käytetä.
    observer.date = pd.Timestamp(timestamp).to_pydatetime().astimezone(UTC).replace(tzinfo=None)
    sun, moon = ephem.Sun(observer), ephem.Moon(observer)
    return math.degrees(float(sun.alt)), math.degrees(float(moon.alt)), float(moon.phase)


def validate_days(days):
    """Hyväksy vain kokonainen päivien määrä väliltä 1–7, myös lomakkeen numeroarvona."""
    try:
        value = float(days)
    except (TypeError, ValueError):
        raise ValueError("Ennustepäivien määrän pitää olla kokonaisluku väliltä 1–7.") from None
    if isinstance(days, bool) or not math.isfinite(value) or not value.is_integer() or not 1 <= value <= 7:
        raise ValueError("Ennustepäivien määrän pitää olla kokonaisluku väliltä 1–7.")
    return int(value)


def night_ranges(now, tz_name, lat, lon, days=3):
    """Paikallinen keskipäivä–keskipäivä; mukaan meneillään oleva pimeä aamuyö."""
    days = validate_days(days)
    tz = ZoneInfo(tz_name)
    local = now.astimezone(tz)
    first = local.date()
    if local.hour < 12 and astronomy(lat, lon, now)[0] < -6:
        first -= timedelta(days=1)
    result = []
    for offset in range(days):
        day = first + timedelta(days=offset)
        start = datetime.combine(day, clock_time(12), tzinfo=tz).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), clock_time(12), tzinfo=tz).astimezone(UTC)
        result.append((day.isoformat(), max(start, now), end))
    return result


def magnetic_latitude(lat, lon, year):
    """Karkea keskitetty dipoli, IGRF14:n ensimmäisen asteen kertoimet 2025 + SV.

    Tämä EI ole AACGM-koordinaatti eikä tarkka revontulivyöhykkeen malli.
    V1 käyttää sitä vain läpinäkyvään alueelliseen Kp-heuristiikkaan.
    """
    if not 2025 <= year <= 2030:
        raise ValueError("V1:n magneettimalli on rajattu vuosiin 2025–2030; päivitä IGRF-kertoimet.")
    dt = year - 2025
    g10, g11, h11 = -29350 + 12.6 * dt, -1410.3 + 10 * dt, 4545.5 - 21.5 * dt
    axis = np.array([-g11, -h11, -g10])
    axis /= np.linalg.norm(axis)
    # Geodeettisesta leveydestä likimääräinen geosentrinen leveys (WGS84).
    p = math.atan((1 - 6.69437999014e-3) * math.tan(math.radians(lat)))
    l = math.radians(lon)
    position = np.array([math.cos(p) * math.cos(l), math.cos(p) * math.sin(l), math.sin(p)])
    return math.degrees(math.asin(float(np.clip(position @ axis, -1, 1))))


def parse_kp(payload):
    # NOAA on käyttänyt sekä oliolistoja että otsikkorivillä varustettuja taulukoita.
    if not isinstance(payload, list) or not payload:
        raise ValueError("Kp-aineisto on tyhjä tai sen muoto on muuttunut.")
    if isinstance(payload[0], list):
        payload = [dict(zip(payload[0], row)) for row in payload[1:]]
    frame = pd.DataFrame(payload)
    if not {"time_tag", "kp", "observed"}.issubset(frame.columns):
        raise ValueError("Kp-aineiston vaaditut sarakkeet puuttuvat.")
    frame["time"] = pd.to_datetime(frame["time_tag"], utc=True, errors="coerce")
    frame["kp"] = pd.to_numeric(frame["kp"], errors="coerce")
    frame = frame[frame["time"].notna() & frame["kp"].between(0, 9)
                  & frame["observed"].isin(["observed", "estimated", "predicted"])]
    return frame.sort_values("time").drop_duplicates("time", keep="last").reset_index(drop=True)


def kp_for_times(frame, times, now):
    """Kolmen tunnin lohkot; ei interpolointia, ei täyttöä aineiston loppuun."""
    times = pd.DatetimeIndex(times)
    output = pd.DataFrame({"time": times, "kp": np.nan, "kp_type": "puuttuu"})
    if frame.empty:
        return output
    recent = frame[(frame["time"] <= pd.Timestamp(now)) & frame["observed"].isin(["observed", "estimated"])]
    if recent.empty or pd.Timestamp(now) - recent["time"].max() > pd.Timedelta(hours=12):
        return output  # Vanhentunut syöte ei tuota suositusta.
    for row in frame.itertuples():
        mask = (times >= row.time) & (times < row.time + pd.Timedelta(hours=3))
        # Havaittua menneisyyttä ei levitetä tulevan tunnin ennusteeksi.
        if row.observed == "observed":
            continue
        output.loc[mask, ["kp", "kp_type"]] = [row.kp, row.observed]
    return output


def fetch_weather(points, days=3):
    days = validate_days(days)
    data = get_json(WEATHER_URL, {
        "latitude": ",".join(f"{v:.5f}" for v in points.latitude),
        "longitude": ",".join(f"{v:.5f}" for v in points.longitude),
        "hourly": ",".join(WEATHER_VARS), "timezone": "GMT", "timeformat": "unixtime",
        # Lisäpäivät kattavat viimeisen yön aamun ja paikallisajan UTC-eron.
        "forecast_days": days + 2, "past_days": 1, "wind_speed_unit": "ms",
        "cell_selection": "land",
    })
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Sääpalvelun virhe: {data.get('reason')}")
    payloads = data if isinstance(data, list) else [data]
    if len(payloads) != len(points):
        raise ValueError("Sääpalvelu palautti väärän määrän sijainteja.")
    frames = []
    for point, payload in zip(points.to_dict("records"), payloads):
        hourly = payload.get("hourly", {})
        if "time" not in hourly:
            raise ValueError("Sääennusteen aikaleimat puuttuvat.")
        frame = pd.DataFrame(hourly)
        frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
        for variable in WEATHER_VARS:
            frame[variable] = pd.to_numeric(frame.get(variable, np.nan), errors="coerce")
        # Open-Meteon sade on edeltävän tunnin kertymä. Tunnin t–t+1 arviolle
        # käytetään t+1-leiman kertymää, ei jo menneen tunnin sadetta.
        frame["precipitation"] = frame["precipitation"].shift(-1)
        for key, value in point.items():
            frame[key] = value
        frame["weather_latitude"] = payload.get("latitude", np.nan)
        frame["weather_longitude"] = payload.get("longitude", np.nan)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def score_hour(row):
    """0–100 vertailupistettä. Kaikki painot ovat kokeellisia, eivät kalibroituja."""
    if not all(pd.notna(row.get(key)) for key in REQUIRED_WEATHER + ["kp", "sun_alt", "mag_lat"]):
        return np.nan, "Arvio epävarma", "Sää- tai Kp-tietoja puuttuu tai Kp-aineisto on vanhentunut."
    cloud = [row[k] for k in WEATHER_VARS[:4]]
    if any(not 0 <= value <= 100 for value in cloud) or row["visibility"] < 0 or row["precipitation"] < 0:
        return np.nan, "Arvio epävarma", "Sääaineistossa on kelvottomia arvoja."
    if row["sun_alt"] >= -6:
        return 0.0, "Liian valoisaa", f"Aurinko korkeimmillaan {row['sun_alt']:.1f}° tunnin aikana; ei riittävän pimeää."

    # NOAA:n karkea leveyssuhde 66 - 2*Kp. Sallitut 5° etäisyyttä huomioivat
    # horisontin lähellä näkymisen hyvin likimääräisesti, eivät geometrisena takeena.
    edge = 66 - 2 * row["kp"]
    gap = max(0.0, edge - abs(row["mag_lat"]))
    activity = float(np.clip(1 - gap / 7, 0, 1))
    darkness = float(np.clip((-row["sun_alt"] - 6) / 12, 0, 1))
    obstruction = max(row["cloud_cover"], row["cloud_cover_low"],
                      row["cloud_cover_mid"], 0.6 * row["cloud_cover_high"])
    sky = (1 - obstruction / 100)**1.3
    visibility_factor = float(np.clip(row["visibility"] / 15000, 0, 1))
    rain_factor = math.exp(-0.7 * row["precipitation"])
    moon_factor = 1 - 0.25 * (row["moon_phase"] / 100) * max(0, math.sin(math.radians(row["moon_alt"])))
    score = 100 * activity * darkness * sky * visibility_factor * rain_factor * moon_factor
    label = "Hyvät" if score >= 65 else "Kohtalaiset" if score >= 35 else "Heikot"
    reason = (f"Kp {row['kp']:.1f} ({'ennuste' if row['kp_type'] == 'predicted' else 'arvio'}); "
              f"magneettinen leveys noin {abs(row['mag_lat']):.1f}°. "
              f"Pilvisyys {row['cloud_cover']:.0f} % (ala {row['cloud_cover_low']:.0f}, "
              f"keski {row['cloud_cover_mid']:.0f}, ylä {row['cloud_cover_high']:.0f} %). "
              f"Näkyvyys {row['visibility']/1000:.1f} km, sade {row['precipitation']:.1f} mm/h. "
              f"Aurinko enintään {row['sun_alt']:.1f}°; Kuu {row['moon_alt']:.0f}°, valaistu {row['moon_phase']:.0f} %.")
    if gap > 3:
        reason += " Kp on sijaintiin nähden vaatimaton; mahdolliset revontulet voivat jäädä matalalle horisonttiin."
    if row["cloud_cover_low"] >= 80 or row["visibility"] < 2000:
        reason += " Alapilvet tai heikko näkyvyys haittaavat havaintoja voimakkaasti."
    return round(score, 1), label, reason


def score_weather_hour(row):
    """Pilvisyys ja pimeys ilman oletusta avaruussäästä; oma vertailuasteikko."""
    required = WEATHER_VARS[:4] + ["sun_alt"]
    if not all(pd.notna(row.get(key)) and math.isfinite(row[key]) for key in required):
        return np.nan, "Ei sääarviota", "Pilvisyys- tai pimeystietoja puuttuu."
    if any(not 0 <= row[key] <= 100 for key in WEATHER_VARS[:4]):
        return np.nan, "Ei sääarviota", "Pilvisyysaineistossa on kelvottomia arvoja."
    darkness = float(np.clip((-row["sun_alt"] - 6) / 12, 0, 1))
    obstruction = max(row["cloud_cover"], row["cloud_cover_low"],
                      row["cloud_cover_mid"], 0.6 * row["cloud_cover_high"])
    score = round(100 * darkness * (1 - obstruction / 100)**1.3, 1)
    label = "Hyvät" if score >= 65 else "Kohtalaiset" if score >= 35 else "Heikot"
    if row["sun_alt"] >= -6:
        label = "Liian valoisaa"
    reason = (f"Pilvisyys {row['cloud_cover']:.0f} % (ala {row['cloud_cover_low']:.0f}, "
              f"keski {row['cloud_cover_mid']:.0f}, ylä {row['cloud_cover_high']:.0f} %). "
              f"Aurinko korkeimmillaan {row['sun_alt']:.1f}° tunnin aikana. "
              "Vain pilvisyyden ja pimeyden vertailu; revontulien esiintymistä ei arvioida.")
    return score, label, reason


@dataclass
class Forecast:
    city: dict
    points: pd.DataFrame
    hours: pd.DataFrame
    nights: list
    fetched_at: datetime
    messages: list
    kp_data: pd.DataFrame


def evaluate(city, points, weather, kp_data, now=None, days=3):
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("Ajan pitää sisältää aikavyöhyke.")
    nights = night_ranges(now, city["timezone"], city["latitude"], city["longitude"], days=days)
    times, labels = [], {}
    for label, start, end in nights:
        for stamp in pd.date_range(pd.Timestamp(start).ceil("h"), end, freq="h", inclusive="left"):
            if stamp + pd.Timedelta(hours=1) <= end:
                times.append(stamp)
                labels[stamp] = label
    requested = pd.DataFrame({"time": times}).merge(points, how="cross")
    weather_cols = ["time", "site"] + WEATHER_VARS + ["weather_latitude", "weather_longitude"]
    hours = requested.merge(weather[weather_cols], on=["time", "site"], how="left", validate="one_to_one")
    kp_hours = kp_for_times(kp_data, times, now)
    hours = hours.merge(kp_hours, on="time", how="left", validate="many_to_one")
    hours["night"] = hours["time"].map(labels)
    positions = []
    for row in hours.itertuples():
        # Pimeyden pitää toteutua tunnin alussa, keskellä ja lopussa.
        sample = [astronomy(row.latitude, row.longitude, row.time + pd.Timedelta(minutes=m)) for m in (0, 30, 60)]
        positions.append((max(s[0] for s in sample), sample[1][1], sample[1][2],
                          magnetic_latitude(row.latitude, row.longitude, now.year + (now.timetuple().tm_yday - 1) / 365.25)))
    hours[["sun_alt", "moon_alt", "moon_phase", "mag_lat"]] = positions
    scores = hours.apply(score_hour, axis=1, result_type="expand")
    scores.columns = ["score", "assessment", "reason"]
    hours = pd.concat([hours, scores], axis=1)
    weather_scores = hours.apply(score_weather_hour, axis=1, result_type="expand")
    weather_scores.columns = ["weather_score", "weather_assessment", "weather_reason"]
    hours = pd.concat([hours, weather_scores], axis=1)
    hours["basis"] = np.select(
        [hours.score.notna(), hours.kp.isna() & hours.weather_score.notna()],
        ["kp_weather", "weather_only"], default="unavailable")
    hours["uncertain"] = hours.basis != "kp_weather"
    messages = []
    if hours.kp.isna().any():
        messages.append("Kp-ennuste ei kata kaikkia tunteja tai aineisto on vanhentunut. "
                        "Näille tunneille näytetään epävarma arvio vain pilvisyyden ja pimeyden perusteella, jos tiedot riittävät.")
    if hours[REQUIRED_WEATHER].isna().any(axis=None):
        messages.append("Osalta tunneista puuttuu säätietoja. Niitä ei luokitella hyviksi tai huonoiksi.")
    return Forecast(city, points, hours, nights, now, messages, kp_data)


def run_forecast(city, radius_km, now=None, days=3):
    days = validate_days(days)
    now = now or datetime.now(UTC)
    points = sampling_points(city["latitude"], city["longitude"], float(radius_km))
    print(f"Haetaan {days} yön sääennuste {len(points)} vertailupisteelle ja NOAA:n Kp-ennuste…")
    weather = fetch_weather(points, days=days)
    kp_error = None
    try:
        kp_data = parse_kp(get_json(KP_URL))
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        kp_data = pd.DataFrame()
        kp_error = str(exc)
    result = evaluate(city, points, weather, kp_data, now, days=days)
    if kp_error:
        result.messages.append(f"Kp-haku epäonnistui: {kp_error}")
    return result


def best_hours(result, night, count=3, weather_only=False):
    subset = result.hours[(result.hours.night == night) & (result.hours.sun_alt < -6)].copy()
    if weather_only or subset.score.notna().sum() == 0:
        subset = subset[subset.basis == "weather_only"].dropna(subset=["weather_score"])
        subset["ranking_score"] = subset.weather_score
    else:
        subset = subset.dropna(subset=["score"])
        subset["ranking_score"] = subset.score
    # Yksi sijainti per kellonaika; tasatilanteessa lähemmäs keskustaa.
    subset = subset.sort_values(["ranking_score", "distance_km", "time"], ascending=[False, True, True])
    selected = []
    for _, row in subset.iterrows():
        if all(abs(row.time - other.time) >= pd.Timedelta(hours=2) for other in selected):
            selected.append(row)
        if len(selected) == count:
            break
    return selected


def display_options(options, tz):
    from IPython.display import Markdown, display
    best = options[0]
    weather_only = best.basis == "weather_only"
    start_local, end_local = best.time.tz_convert(tz), (best.time + pd.Timedelta(hours=1)).tz_convert(tz)
    if weather_only:
        title = "Paras tunti pilvisyyden ja pimeyden perusteella"
        assessment = f"Arvio epävarma · Kp-ennuste puuttuu · {best.weather_assessment.lower()} sääolosuhteet"
        points_label, reason = "Sääpisteet", best.weather_reason
    else:
        title = "Paras arvioitu tunti" if best.score >= 35 else "Suhteellisesti paras tunti, mutta olosuhteet heikot"
        assessment, points_label, reason = best.assessment, "Kp + sää -pisteet", best.reason
    display(Markdown(f"**{title}: {start_local:%d.%m. %H:%M %Z} – {end_local:%d.%m. %H:%M %Z}**  \n"
                     f"**{assessment}** · {points_label} {best.ranking_score:.0f}/100 · {best.site}  \n"
                     f"{reason}  \n[Vertailupiste kartalla]({map_url(best.latitude, best.longitude)})"))
    table = []
    for option in options:
        a, b = option.time.tz_convert(tz), (option.time + pd.Timedelta(hours=1)).tz_convert(tz)
        table.append({"Aika": f"{a:%d.%m. %H:%M %Z}–{b:%H:%M %Z}", "Piste": option.site,
                      "Arvio": (f"Epävarma · {option.weather_assessment.lower()} sääolosuhteet"
                                if weather_only else option.assessment),
                      points_label + " / 100": option.ranking_score,
                      "Kp": "Puuttuu" if weather_only else option.kp, "Pilvet %": option.cloud_cover,
                      "Aurinko enintään °": round(option.sun_alt, 1),
                      "Sijainti": f"{option.latitude:.4f}, {option.longitude:.4f}"})
    display(pd.DataFrame(table))


def map_url(lat, lon):
    return f"https://www.openstreetmap.org/?mlat={lat:.5f}&mlon={lon:.5f}#map=12/{lat:.5f}/{lon:.5f}"


def show_report(result):
    from IPython.display import Markdown, display
    tz = ZoneInfo(result.city["timezone"])
    display(Markdown(f"## Revontulivahti · {result.city['name']}\n"
                     f"Ennusteen pituus: **{len(result.nights)} päivää / yötä**. "
                     f"Haku {result.fetched_at.astimezone(tz):%d.%m.%Y %H:%M %Z}. "
                     f"Kaikki kellonajat vyöhykkeellä **{result.city['timezone']}**.\n\n"
                     "Pisteet 0–100 ovat kokeellinen vertailu, **eivät prosenttitodennäköisyys**. "
                     "Kp + sää -pisteet ja pelkät sääpisteet ovat eri arvioita; niitä ei verrata keskenään. "
                     "Arvio olettaa paljain silmin havainnoinnin pimeässä paikassa ja avoimen horisontin. "
                     "Valosaastetta tai maaston esteitä ei mitata."))
    for message in result.messages:
        print("HUOM:", message)
    if not result.kp_data.empty:
        print("Kp-aineiston viimeinen aikaleima (ei julkaisuhetki):", result.kp_data.time.max())
    print("Sääaineiston hakuhetki näytetään yllä; API ei ilmoita kaikkien yhdistettyjen mallien julkaisuikää.")
    for label, start, end in result.nights:
        day = date.fromisoformat(label)
        display(Markdown(f"### Yö {day:%d.%m.}–{day + timedelta(days=1):%d.%m.%Y}"))
        options = best_hours(result, label)
        subset = result.hours[result.hours.night == label]
        if not options:
            if not subset.empty and (subset.sun_alt >= -6).all():
                print("Ei riittävän pimeitä täysiä ennustetunteja: Aurinko ei laske alle −6°.")
            else:
                print("Ei arvioitavia pimeitä tunteja: tarvittavia sää- tai pilvisyystietoja puuttuu. "
                      "Tee uusi haku lähempänä tätä yötä.")
            continue
        coverage = subset[subset.sun_alt < -6].groupby("time").score.apply(lambda values: values.notna().any())
        if len(coverage) and not coverage.all():
            print(f"Osittainen kattavuus: {int(coverage.sum())}/{len(coverage)} pimeästä tunnista arvioitu. "
                  "Puuttuvan Kp-ennusteen ajat arvioidaan erikseen sääolosuhteiden perusteella.")
        display_options(options, tz)
        if options[0].basis == "kp_weather":
            weather_options = best_hours(result, label, weather_only=True)
            if weather_options:
                display(Markdown("**Lisäksi tämän yön Kp-ennusteen ulkopuoliset ajat:**"))
                display_options(weather_options, tz)
    display(Markdown("**Havaintopaikat:** säde on linnuntietä. Vertailupiste voi osua vesistöön tai paikkaan, "
                     "johon ei ole pääsyä. Tarkista kartalta kulkuyhteys, lupa ja avoin näkymä. "
                     "Sääennusteen ruutupiste voi myös poiketa pyydetystä koordinaatista.\n\n"
                     "Kp ei ennusta yksittäistä revontulipurkausta. Magneettinen leveys on karkea IGRF14-dipoliarvio; "
                     "mallia ei ole kalibroitu havaintoaineistolla. Tarkista tuore tilanne ennen lähtöä."))


def plot_report(result):
    import matplotlib.pyplot as plt
    from matplotlib.dates import DateFormatter
    tz = ZoneInfo(result.city["timezone"])
    count = len(result.nights)
    fig, axes = plt.subplots(count, 1, figsize=(12, 3 * count), constrained_layout=True, squeeze=False)
    axes = axes[:, 0]
    for ax, (label, start, end) in zip(axes, result.nights):
        subset = result.hours[result.hours.night == label]
        local = subset[subset.site == "Keskusta"].sort_values("time")
        # NaN säilyttää aukot myös kuvaajassa.
        maxima = subset.groupby("time").score.max()
        weather_maxima = subset.assign(fallback=subset.weather_score.where(subset.basis == "weather_only")).groupby("time").fallback.max()
        ax.plot(maxima.index, maxima.values, label="Paras piste (Kp + sää)", color="#159c83")
        ax.plot(local.time, local.score, label="Keskusta (Kp + sää)", color="#6d5bd0", alpha=0.8)
        if weather_maxima.notna().any():
            ax.plot(weather_maxima.index, weather_maxima.values, label="Vain pilvet ja pimeys · epävarma",
                    color="#b86a00", linestyle="--")
        if maxima.isna().all() and weather_maxima.isna().all():
            ax.text(0.5, 0.5, "Arviota ei voida laskea: tarvittavia säätietoja puuttuu",
                    transform=ax.transAxes, ha="center", va="center", wrap=True)
        ax.set_ylim(0, 100)
        ax.set_xlim(start, end)
        ax.set_title(f"Yö {label} · vertailupisteet eivät ole todennäköisyyksiä")
        ax.set_ylabel("Pistettä / 100")
        ax.xaxis.set_major_formatter(DateFormatter("%d.%m. %H:%M", tz=tz))
        ax.grid(alpha=0.2)
        ax.legend(loc="upper right")
    return fig


def ovation_context(city, now=None):
    """Erillinen lähitilanne. Ei vaikuta tulevien öiden pisteisiin."""
    now = pd.Timestamp(now or datetime.now(UTC))
    try:
        data = get_json(OVATION_URL)
        observed = pd.to_datetime(data["Observation Time"], utc=True)
        valid = pd.to_datetime(data["Forecast Time"], utc=True)
        if not pd.Timedelta(minutes=-10) <= now - observed <= pd.Timedelta(hours=2):
            return "OVATION: havaintoaineisto on vanhentunut tai aikaleima virheellinen; lähitilannetta ei käytetä."
        if not pd.Timedelta(minutes=-30) <= valid - now <= pd.Timedelta(minutes=120):
            return "OVATION: ennusteen voimassaoloaika ei ole lähellä nykyhetkeä."
        grid = np.asarray(data["coordinates"], dtype=float)
        if grid.ndim != 2 or grid.shape[1] != 3:
            raise ValueError("Ruudukon muoto on muuttunut")
        distances = distance_km(city["latitude"], city["longitude"], grid[:, 1], grid[:, 0])
        nearest = grid[np.argmin(distances), 2]
        return (f"OVATION-lähitilanne, voimassa {valid.tz_convert(city['timezone']):%d.%m. %H:%M %Z}: "
                f"lähimmän ruudun malliarvo {nearest:g}/100. "
                "Arvo ei sisällä säätä, pimeyttä tai havaintopaikkaa, eikä ole oma näkemistodennäköisyytesi. "
                "Sitä ei uloteta seuraaviin öihin eikä käytetä yllä olevissa pisteissä.")
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        return f"OVATION-lähitilanne ei ole saatavilla: {exc}"


def save_outputs(result, directory="outputs"):
    path = Path(directory)
    path.mkdir(exist_ok=True, parents=True)
    result.hours.to_csv(path / "tuntiarviot.csv", index=False, encoding="utf-8-sig")
    result.points.to_csv(path / "vertailupisteet.csv", index=False, encoding="utf-8-sig")
    (path / "havaintoarvio.txt").write_text(report_text(result), encoding="utf-8")
    metadata = {"city": result.city, "fetched_at_utc": result.fetched_at.isoformat(),
                "forecast_days": len(result.nights),
                "messages": result.messages, "model": "Revontulivahti 0.2; kokeellinen, ei kalibroitu",
                "score_columns": {"score": "Kp ja sää; tyhjä kun Kp puuttuu",
                                  "weather_score": "Vain pilvisyys ja pimeys; ei revontuliennuste",
                                  "basis": "kp_weather / weather_only / unavailable",
                                  "uncertain": "Tosi kun Kp:n ja sään kokonaisarviota ei voida laskea"},
                "sources": [KP_URL, WEATHER_URL, GEO_URL],
                "note": "Hakuhetki ei ole lähteiden julkaisuhetki. Valosaastetta ja maastoa ei mallinneta."}
    (path / "haun_tiedot.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    if not result.kp_data.empty:
        result.kp_data.to_csv(path / "kp_aineisto.csv", index=False)
    return path


def validate_email(value):
    """Yksi osoite ilman näyttönimeä tai ylimääräisiä vastaanottajia."""
    value = str(value).strip()
    try:
        if not value.isascii() or any(char in value for char in "\r\n,;<> "):
            raise ValueError
        address = Address(addr_spec=value)
        if not address.username or not address.domain or address.addr_spec != value:
            raise ValueError
    except (ValueError, IndexError, HeaderParseError):
        raise ValueError("Anna yksi sähköpostiosoite muodossa nimi@example.com ilman näyttönimeä.") from None
    return value


def report_text(result):
    """Sama yökohtainen tieto kuin muistikirjassa, suoraan viestin tekstiksi."""
    tz = ZoneInfo(result.city["timezone"])
    lines = [f"Revontulivahti – {result.city['name']}",
             f"Ennusteen pituus: {len(result.nights)} päivää / yötä.",
             f"Haku: {result.fetched_at.astimezone(tz):%d.%m.%Y %H:%M %Z}.",
             f"Kellonajat: {result.city['timezone']}.", "",
             "Pisteet ovat kokeellisia vertailupisteitä, eivät prosenttitodennäköisyyksiä.",
             "Kp + sää -pisteitä ja pelkkiä sääpisteitä ei voi verrata keskenään.", ""]
    lines.extend("HUOM: " + message for message in result.messages)
    if not result.kp_data.empty:
        lines.append(f"Kp-aineiston viimeinen aikaleima (ei julkaisuhetki): {result.kp_data.time.max()}")
    for label, _, _ in result.nights:
        day = date.fromisoformat(label)
        lines.extend(["", f"Yö {day:%d.%m.}–{day + timedelta(days=1):%d.%m.%Y}"])
        options = best_hours(result, label)
        if not options:
            subset = result.hours[result.hours.night == label]
            text = ("Ei riittävän pimeitä täysiä ennustetunteja."
                    if not subset.empty and (subset.sun_alt >= -6).all()
                    else "Tarvittavia tietoja puuttuu; havaintoaikoja ei voida arvioida.")
            lines.append(text)
            continue
        coverage = result.hours[(result.hours.night == label) & (result.hours.sun_alt < -6)]
        coverage = coverage.groupby("time").score.apply(lambda values: values.notna().any())
        if len(coverage) and not coverage.all():
            lines.append(f"Kp + sää -arvion kattavuus: {int(coverage.sum())}/{len(coverage)} pimeää tuntia.")
        groups = [options]
        if options[0].basis == "kp_weather":
            extra = best_hours(result, label, weather_only=True)
            if extra:
                groups.append(extra)
        for choices in groups:
            weather_only = choices[0].basis == "weather_only"
            lines.append("ARVIO EPÄVARMA – Kp-ennuste puuttuu. Vain pilvisyys ja pimeys."
                         if weather_only else "Havaintoajat Kp-ennusteen ja sään perusteella:")
            if weather_only:
                lines.append("Revontulien esiintymistä ei arvioida.")
            for index, option in enumerate(choices, 1):
                a, b = option.time.tz_convert(tz), (option.time + pd.Timedelta(hours=1)).tz_convert(tz)
                assessment = option.weather_assessment if weather_only else option.assessment
                kind = "Sääpisteet" if weather_only else "Kp + sää -pisteet"
                reason = option.weather_reason if weather_only else option.reason
                lines.extend([f"{index}. {a:%d.%m. %H:%M %Z} – {b:%d.%m. %H:%M %Z}, {option.site}",
                              f"   {assessment}; {kind} {option.ranking_score:.0f}/100.",
                              "   " + reason, "   Kartta: " + map_url(option.latitude, option.longitude)])
    lines.extend(["", "Liitteenä on koko jakson kuvaaja.",
                  "Arvio olettaa pimeän havaintopaikan ja avoimen horisontin. Valosaastetta ja maastoa ei mallinneta.",
                  "Vertailupiste ei ole tarkistettu retkikohde. Hakusäde on linnuntietä; tarkista kulkuyhteys ja lupa.",
                  "Magneettinen sijainti ja pisteytys ovat karkeita, kalibroimattomia arvioita.",
                  "Hakuhetki ei ole säämallien julkaisuhetki. Tarkista tuore tilanne ennen lähtöä.",
                  "Lähteet: NOAA SWPC, Open-Meteo (paikannimet: GeoNames), IGRF14 ja PyEphem.",
                  "https://www.swpc.noaa.gov/products/aurora-30-minute-forecast",
                  "https://open-meteo.com/en/docs"])
    return "\n".join(lines)


def send_via_backend(result, recipient, chart_path, service_url):
    """Lähetä raportti Gmail-valtuutusta varten; palauta turvallinen linkki, ei tunnuksia."""
    recipient = validate_email(recipient)
    parsed = urlsplit(service_url)
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("Sähköpostipalvelua ei ole määritetty. Ylläpitäjän pitää asettaa palvelun HTTPS-osoite; voit käyttää ZIP-latausta.")
    chart = Path(chart_path).read_bytes()
    if len(chart) > 2_500_000 or not chart.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Kuvaajan pitää olla PNG-tiedosto, enintään 2,5 MB.")
    payload = {"recipient": recipient, "city": " ".join(str(result.city["name"]).splitlines()),
               "text": report_text(result), "chart_base64": base64.b64encode(chart).decode("ascii")}
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(data) > 4_000_000:
        raise ValueError("Raportti ja kuvaaja ovat liian suuria sähköpostipalvelulle. Käytä ZIP-latausta.")
    request = Request(service_url.rstrip("/") + "/v1/reports", method="POST", data=data,
                      headers={"Content-Type": "application/json", "User-Agent": "Revontulivahti/0.3",
                               "Idempotency-Key": hashlib.sha256(data).hexdigest()})
    try:
        with urlopen(request, timeout=60) as response:
            result_data = json.load(response)
    except HTTPError as error:
        messages = {429: "Sähköpostipalvelun lähetysraja täyttyi. Kokeile myöhemmin tai käytä ZIP-latausta.",
                    410: "Raporttipyyntö päättyi. Tarkista Gmailin Lähetetyt ennen uutta hakua tai käytä ZIP-latausta.",
                    413: "Raportti on liian suuri sähköpostipalvelulle. Käytä ZIP-latausta.",
                    422: "Sähköpostipalvelu ei hyväksynyt osoitetta tai raporttia. Käytä ZIP-latausta."}
        raise RuntimeError(messages.get(error.code, "Sähköpostipalvelun pyyntö epäonnistui. Tarkista posti ennen uusintaa tai käytä ZIP-latausta.")) from None
    except (OSError, ValueError):
        raise RuntimeError("Sähköpostipalveluun ei saatu varmistettua yhteyttä. Kokeile uudelleen tai käytä ZIP-latausta.") from None
    if not isinstance(result_data, dict) or result_data.get("status") not in ("authorization_required", "accepted"):
        raise RuntimeError("Sähköpostipalvelun vastaus oli odottamaton. Käytä ZIP-latausta.")
    if result_data["status"] == "authorization_required":
        expected = service_url.rstrip("/") + "/authorize/"
        link = result_data.get("authorization_url", "")
        if not isinstance(link, str) or not link.startswith(expected) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", link[len(expected):]):
            raise RuntimeError("Palvelu palautti virheellisen Gmail-valtuutuslinkin. Käytä ZIP-latausta.")
    return result_data
