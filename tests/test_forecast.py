import json
import math
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import revontulivahti as rv

UTC = timezone.utc
CITY = {"name": "Helsinki", "latitude": 60.1699, "longitude": 24.9384, "timezone": "Europe/Helsinki"}
NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)


def kp_fixture():
    times = pd.date_range("2026-11-20T09:00Z", "2026-11-23T03:00Z", freq="3h")
    return rv.parse_kp([{"time_tag": stamp.isoformat(), "kp": 4,
                         "observed": "estimated" if stamp <= pd.Timestamp(NOW) else "predicted"}
                        for stamp in times])


def weather_fixture(points):
    times = pd.date_range("2026-11-20T00:00Z", "2026-11-29T00:00Z", freq="h")
    frames = []
    for site in points.site:
        frame = pd.DataFrame({"time": times, "site": site})
        for key in rv.WEATHER_VARS:
            frame[key] = 0.0
        frame["visibility"] = 24000.0
        frame["weather_latitude"] = CITY["latitude"]
        frame["weather_longitude"] = CITY["longitude"]
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


class ForecastTests(unittest.TestCase):
    def test_sampling_within_radius_and_dateline(self):
        for lat, lon in [(60, 25), (78, 179.9), (-65, -179.9)]:
            points = rv.sampling_points(lat, lon, 100)
            self.assertEqual(len(points), 17)
            self.assertTrue((rv.distance_km(lat, lon, points.latitude, points.longitude) <= 100.0001).all())
        self.assertEqual(len(rv.sampling_points(60, 25, 0)), 1)
        for radius in [-1, 201, float("nan")]:
            with self.assertRaises(ValueError):
                rv.sampling_points(60, 25, radius)

    def test_kp_boundaries_missing_and_stale(self):
        times = pd.to_datetime(["2026-11-20T14:00Z", "2026-11-20T15:00Z", "2026-11-23T05:00Z", "2026-11-23T06:00Z"])
        frame = kp_fixture()
        output = rv.kp_for_times(frame, times, NOW)
        self.assertEqual(output.kp_type.iloc[0], "estimated")
        self.assertEqual(output.kp_type.iloc[1], "predicted")
        self.assertEqual(output.kp.iloc[2], 4)
        self.assertTrue(math.isnan(output.kp.iloc[3]))
        frame.loc[frame.time == pd.Timestamp("2026-11-20T15:00Z"), "kp"] = np.nan
        self.assertTrue(math.isnan(rv.kp_for_times(frame, times, NOW).kp.iloc[1]))
        stale = frame[frame.observed == "predicted"]
        self.assertTrue(rv.kp_for_times(stale, times, NOW).kp.isna().all())

    def test_kp_table_format_and_observations_not_forecasts(self):
        data = [["time_tag", "kp", "observed"], ["2026-11-20T12:00Z", "2.33", "observed"]]
        frame = rv.parse_kp(data)
        self.assertAlmostEqual(frame.kp.iloc[0], 2.33)
        self.assertTrue(rv.kp_for_times(frame, [pd.Timestamp(NOW)], NOW).kp.isna().all())

    def test_solar_summer_winter_and_moon(self):
        summer = rv.astronomy(69.6, 18.9, datetime(2026, 6, 21, 22, tzinfo=UTC))
        winter = rv.astronomy(60.17, 24.94, datetime(2026, 12, 21, 22, tzinfo=UTC))
        self.assertGreater(summer[0], -6)
        self.assertLess(winter[0], -18)
        self.assertTrue(0 <= winter[2] <= 100)

    def test_nights_morning_and_dst(self):
        morning = datetime(2026, 11, 20, 1, tzinfo=UTC)
        ranges = rv.night_ranges(morning, CITY["timezone"], 60.17, 24.94)
        self.assertEqual(ranges[0][0], "2026-11-19")
        ranges = rv.night_ranges(datetime(2026, 10, 24, 10, tzinfo=UTC), CITY["timezone"], 60.17, 24.94)
        self.assertEqual(ranges[0][2] - ranges[0][1], timedelta(hours=24))  # Start clipped by now (13 local).
        ranges = rv.night_ranges(datetime(2026, 10, 24, 8, tzinfo=UTC), CITY["timezone"], 60.17, 24.94)
        self.assertEqual(ranges[0][2] - ranges[0][1], timedelta(hours=25))

    def test_scores_missing_clouds_and_daylight(self):
        row = {key: 0.0 for key in rv.WEATHER_VARS}
        row.update(kp=5, kp_type="predicted", mag_lat=60, sun_alt=-20,
                   moon_alt=-10, moon_phase=100, visibility=24000)
        good = rv.score_hour(row)[0]
        self.assertEqual(good, 100)
        self.assertEqual(rv.score_hour(dict(row, cloud_cover_low=100))[0], 0)
        self.assertEqual(rv.score_hour(dict(row, sun_alt=-3))[1], "Liian valoisaa")
        self.assertTrue(math.isnan(rv.score_hour(dict(row, kp=np.nan))[0]))
        self.assertTrue(math.isnan(rv.score_hour(dict(row, visibility=np.nan))[0]))
        self.assertLess(rv.score_hour(dict(row, moon_alt=60))[0], good)

    def test_weather_precipitation_alignment(self):
        points = rv.sampling_points(60, 25, 0)
        payload = {"latitude": 60, "longitude": 25, "hourly": {
            "time": [1795176000, 1795179600, 1795183200],
            **{key: [0, 0, 0] for key in rv.WEATHER_VARS},
            "precipitation": [0.1, 0.5, 1.0]}}
        with patch.object(rv, "get_json", return_value=payload):
            frame = rv.fetch_weather(points)
        self.assertEqual(frame.precipitation.iloc[0], 0.5)
        self.assertEqual(frame.precipitation.iloc[1], 1.0)
        self.assertTrue(math.isnan(frame.precipitation.iloc[2]))

    def test_days_validation_and_no_network_on_invalid_input(self):
        for days in range(1, 8):
            self.assertEqual(rv.validate_days(days), days)
        self.assertEqual(rv.validate_days("7"), 7)
        for invalid in (0, 8, -1, 2.5, float("nan"), float("inf"), "abc", None, True):
            with self.subTest(invalid=invalid), patch.object(rv, "get_json") as request:
                with self.assertRaises(ValueError):
                    rv.run_forecast(CITY, 0, now=NOW, days=invalid)
                request.assert_not_called()

    def test_all_supported_lengths_and_kp_end(self):
        points = rv.sampling_points(CITY["latitude"], CITY["longitude"], 0)
        weather = weather_fixture(points)
        for days in range(1, 8):
            with self.subTest(days=days):
                result = rv.evaluate(CITY, points, weather, kp_fixture(), NOW, days=days)
                self.assertEqual(len(result.nights), days)
                self.assertEqual(result.hours.night.nunique(), days)
                # NOW on klo 14 Helsingissä: ensimmäisestä keskipäiväjaksosta
                # kaksi tuntia on jo kulunut eikä menneisyyttä näytetä.
                self.assertEqual(len(result.hours), days * 24 - 2)
                self.assertFalse(result.hours[rv.REQUIRED_WEATHER].isna().any(axis=None))
                self.assertEqual(result.nights[-1][2] - result.nights[0][1], timedelta(days=days, hours=-2))
                if days > 3:
                    late = result.hours[result.hours.night == result.nights[-1][0]]
                    self.assertTrue(late.score.isna().all())
                    self.assertTrue(late.weather_score.notna().all())
                    self.assertTrue(late.uncertain.all())
                    choices = rv.best_hours(result, result.nights[-1][0])
                    self.assertTrue(choices)
                    self.assertTrue(all(item.basis == "weather_only" for item in choices))

    def test_weather_request_covers_selected_duration(self):
        points = rv.sampling_points(60, 25, 0)
        payload = {"hourly": {"time": [1795176000, 1795179600]}}
        for days in (1, 3, 7):
            with self.subTest(days=days), patch.object(rv, "get_json", return_value=payload) as request:
                rv.fetch_weather(points, days=days)
                self.assertEqual(request.call_args.args[1]["forecast_days"], days + 2)

    def test_plot_handles_single_and_seven_nights(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        points = rv.sampling_points(CITY["latitude"], CITY["longitude"], 0)
        for days in (1, 7):
            with self.subTest(days=days):
                result = rv.evaluate(CITY, points, weather_fixture(points), kp_fixture(), NOW, days=days)
                figure = rv.plot_report(result)
                self.assertEqual(len(figure.axes), days)
                if days == 7:
                    fallback = figure.axes[-1].lines[-1]
                    self.assertEqual(fallback.get_linestyle(), "--")
                    self.assertIn("epävarma", fallback.get_label())
                    self.assertTrue(np.isfinite(fallback.get_ydata()).all())
                plt.close(figure)

    def test_notebook_selected_days_reach_forecast(self):
        root = Path(__file__).resolve().parents[1]
        notebook = json.loads((root / "Revontulivahti.ipynb").read_text(encoding="utf-8"))
        sources = ["".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code"]
        for days in (1, 7):
            namespace = {"math": math, "validate_days": rv.validate_days}
            exec(sources[2], namespace)
            namespace["ennustepaivat"] = days
            with patch.object(rv, "run_forecast", return_value=object()) as run:
                namespace.update(choose_city=lambda *args: CITY, run_forecast=run,
                                 show_report=lambda *args: None, ovation_context=lambda *args: "OK")
                exec(sources[3], namespace)
                self.assertEqual(run.call_args.kwargs["days"], days)

    def test_end_to_end_and_export(self):
        points = rv.sampling_points(60.1699, 24.9384, 20)
        result = rv.evaluate(CITY, points, weather_fixture(points), kp_fixture(), NOW)
        self.assertEqual(len(result.nights), 3)
        self.assertTrue(result.hours.time.ge(pd.Timestamp(NOW)).all())
        self.assertFalse(result.hours.duplicated(["site", "time"]).any())
        for night, _, _ in result.nights:
            selected = rv.best_hours(result, night)
            self.assertTrue(selected)
            self.assertTrue(all(item.sun_alt < -6 for item in selected))
        self.assertTrue(result.hours.loc[result.hours.time >= pd.Timestamp("2026-11-23T06:00Z"), "score"].isna().all())
        output = rv.save_outputs(result, "outputs/test")
        self.assertTrue((output / "tuntiarviot.csv").exists())
        broken = weather_fixture(points)
        broken["cloud_cover"] = np.nan
        missing = rv.evaluate(CITY, points, broken, pd.DataFrame(), NOW)
        self.assertTrue(missing.hours.score.isna().all())
        self.assertFalse(rv.best_hours(missing, missing.nights[0][0]))

    def test_weather_score_ranks_clouds_darkness_without_kp(self):
        row = {key: 0.0 for key in rv.WEATHER_VARS[:4]}
        row.update(sun_alt=-20, kp=np.nan)
        self.assertEqual(rv.score_weather_hour(row)[0], 100)
        self.assertLess(rv.score_weather_hour(dict(row, sun_alt=-10))[0], 100)
        self.assertLess(rv.score_weather_hour(dict(row, cloud_cover=40))[0], 100)
        self.assertEqual(rv.score_weather_hour(dict(row, cloud_cover_low=100))[0], 0)
        self.assertEqual(rv.score_weather_hour(dict(row, sun_alt=-3))[1], "Liian valoisaa")
        for invalid in (np.nan, -1, 101, np.inf):
            self.assertTrue(math.isnan(rv.score_weather_hour(dict(row, cloud_cover=invalid))[0]))
        # Kuun, sateen ja näkyvyyden puute ei estä pelkän pilvisyyden ja pimeyden vertailua.
        self.assertEqual(rv.score_weather_hour(dict(row, visibility=np.nan, precipitation=np.nan))[0], 100)

    def test_weather_fallback_when_kp_missing_or_stale_and_not_in_daylight(self):
        points = rv.sampling_points(CITY["latitude"], CITY["longitude"], 0)
        for kp in (pd.DataFrame(), kp_fixture()[kp_fixture().observed == "predicted"]):
            result = rv.evaluate(CITY, points, weather_fixture(points), kp, NOW)
            choices = rv.best_hours(result, result.nights[0][0])
            self.assertTrue(choices)
            self.assertTrue(result.hours.score.isna().all())
            self.assertTrue(all(item.uncertain and item.sun_alt < -6 for item in choices))
            self.assertTrue(all(item.basis == "weather_only" for item in choices))

    def test_partial_night_keeps_weather_and_kp_rankings_separate(self):
        points = rv.sampling_points(CITY["latitude"], CITY["longitude"], 0)
        kp = kp_fixture()
        # Katkaise ennuste keskellä viimeisen yön pimeää osuutta.
        kp = kp[kp.time <= pd.Timestamp("2026-11-22T21:00Z")]
        result = rv.evaluate(CITY, points, weather_fixture(points), kp, NOW)
        last = result.nights[-1][0]
        full = rv.best_hours(result, last)
        weather = rv.best_hours(result, last, weather_only=True)
        self.assertTrue(full)
        self.assertTrue(weather)
        self.assertTrue(all(item.basis == "kp_weather" for item in full))
        self.assertTrue(all(item.basis == "weather_only" for item in weather))
        with patch("IPython.display.display") as display:
            rv.show_report(result)
            text = "\n".join(str(call.args[0].data) for call in display.call_args_list
                             if hasattr(call.args[0], "data"))
            self.assertIn("Kp-ennusteen ulkopuoliset ajat", text)
            self.assertIn("Arvio epävarma", text)
            self.assertIn("revontulien esiintymistä ei arvioida", text)

    def test_notebook_is_self_contained_and_compiles(self):
        root = Path(__file__).resolve().parents[1]
        notebook = json.loads((root / "Revontulivahti.ipynb").read_text(encoding="utf-8"))
        self.assertEqual(notebook["nbformat"], 4)
        sources = ["".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code"]
        self.assertIn((root / "revontulivahti.py").read_text(encoding="utf-8"), sources[1])
        for source in sources:
            cleaned = "\n".join(line for line in source.splitlines() if not line.startswith("%"))
            compile(cleaned, "notebook_cell", "exec")


if __name__ == "__main__":
    unittest.main()
