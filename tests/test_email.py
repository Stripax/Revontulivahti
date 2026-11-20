import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import test_forecast as fixture
import revontulivahti as rv


class EmailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        points = rv.sampling_points(fixture.CITY["latitude"], fixture.CITY["longitude"], 0)
        cls.result = rv.evaluate(fixture.CITY, points, fixture.weather_fixture(points), fixture.kp_fixture(), fixture.NOW, days=7)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.chart = Path(self.temp.name) / "chart.png"
        self.chart.write_bytes(b"\x89PNG\r\n\x1a\nTEST_IMAGE")
        self.reply = {"status": "authorization_required", "authorization_url": "https://mail.example.com/authorize/" + "a" * 43}

    def test_text_contains_uncertainty_and_every_night(self):
        text = rv.report_text(self.result)
        self.assertIn("ARVIO EPÄVARMA", text)
        self.assertIn("Revontulien esiintymistä ei arvioida", text)
        for night, _, _ in self.result.nights:
            self.assertIn(f"Yö {rv.date.fromisoformat(night):%d.%m.}", text)

    def test_single_address_validation(self):
        self.assertEqual(rv.validate_email(" name+tag@example.com "), "name+tag@example.com")
        for invalid in ("", "name", "a@@example.com", "a@example.com,b@example.com",
                        "a@example.com\r\nBcc: other@example.com", "Name <a@example.com>", "name@"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                rv.validate_email(invalid)

    def test_backend_payload_is_stable_and_contains_no_credentials(self):
        def transport(request, timeout):
            payload = json.loads(request.data)
            self.assertNotIn("Authorization", request.headers)
            self.assertEqual(set(payload), {"recipient", "city", "text", "chart_base64"})
            self.assertEqual(payload["text"], rv.report_text(self.result))
            return io.BytesIO(json.dumps(self.reply).encode())
        with patch.object(rv, "urlopen", side_effect=transport) as network, patch("builtins.input") as prompt:
            for _ in range(2):
                response = rv.send_via_backend(self.result, "recipient@example.com", self.chart, "https://mail.example.com")
                self.assertEqual(response, self.reply)
            keys = [call.args[0].headers["Idempotency-key"] for call in network.call_args_list]
            self.assertEqual(keys[0], keys[1])
            prompt.assert_not_called()

    def test_backend_errors_and_redirect_host_are_checked(self):
        for reply in ({"status": "authorization_required", "authorization_url": "https://evil.example/authorize/" + "a" * 43},
                      {"status": "authorization_required", "authorization_url": "https://mail.example.com/authorize/x?bad"},
                      {"status": "something-else"}):
            with patch.object(rv, "urlopen", return_value=io.BytesIO(json.dumps(reply).encode())), self.assertRaises(RuntimeError):
                rv.send_via_backend(self.result, "recipient@example.com", self.chart, "https://mail.example.com")
        with patch.object(rv, "urlopen", side_effect=HTTPError("url", 429, "limit", {}, None)) as call:
            with self.assertRaises(RuntimeError):
                rv.send_via_backend(self.result, "recipient@example.com", self.chart, "https://mail.example.com")
            self.assertEqual(call.call_count, 1)

    def test_notebook_delivers_only_selected_format_and_handles_error(self):
        root = Path(__file__).resolve().parents[1]
        notebook = json.loads((root / "Revontulivahti.ipynb").read_text(encoding="utf-8"))
        sources = ["".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code"]
        for method in ("ZIP-tiedosto", "Sähköposti"):
            namespace = dict(rv.__dict__)
            namespace.update(tulos=self.result, toimitustapa=method, vastaanottaja="recipient@example.com",
                             plot_report=MagicMock(), save_outputs=MagicMock(return_value=Path(self.temp.name)),
                             send_via_backend=MagicMock(return_value=self.reply), SAHKOPOSTIPALVELU_URL="https://mail.example.com")
            with patch("shutil.make_archive", return_value="example.zip") as archive, \
                 patch("matplotlib.pyplot.show"), patch("IPython.display.display") as display:
                exec(sources[4], namespace)
            if method == "ZIP-tiedosto":
                archive.assert_called_once()
                namespace["send_via_backend"].assert_not_called()
            else:
                archive.assert_not_called()
                namespace["send_via_backend"].assert_called_once()
                self.assertIn("Valtuuta Gmail", str(display.call_args.args[0].data))
                namespace["send_via_backend"].side_effect = RuntimeError("Testattu lähetysvirhe")
                with patch("matplotlib.pyplot.show"), patch("builtins.print") as output:
                    exec(sources[4], namespace)
                self.assertTrue(any("ei onnistunut" in str(call) for call in output.call_args_list))

    def test_recipient_is_requested_only_for_email(self):
        root = Path(__file__).resolve().parents[1]
        notebook = json.loads((root / "Revontulivahti.ipynb").read_text(encoding="utf-8"))
        source = ["".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code"][2]
        namespace = {"validate_email": rv.validate_email}
        with patch("builtins.input") as ask:
            exec(source, namespace)
            ask.assert_not_called()
        source = source.replace('toimitustapa = "ZIP-tiedosto"', 'toimitustapa = "Sähköposti"')
        with patch("builtins.input", side_effect=["bad", "recipient@example.com"]), patch("builtins.print"):
            exec(source, namespace)
        self.assertEqual(namespace["vastaanottaja"], "recipient@example.com")


if __name__ == "__main__":
    unittest.main()
