"""Verkkoyhteyttä käyttävä kokonaisuuden tarkistus kehittäjälle."""
import json
from pathlib import Path
from unittest.mock import patch

import matplotlib
matplotlib.use("Agg")

import revontulivahti as rv


def main():
    cities = rv.search_city("Helsinki", "FI")
    city = next(item for item in cities if item["name"] == "Helsinki")
    result = rv.run_forecast(city, 50)
    assert len(result.points) == 17
    assert len(result.nights) == 3
    assert len(result.hours) > 500
    assert result.hours.cloud_cover.notna().any(), "Säädata puuttuu"
    assert not result.kp_data.empty, "Kp-aineisto puuttuu"
    assert result.hours.score.notna().any(), "Kp ja sää eivät leikkaa tulevilla tunneilla"
    with patch("IPython.display.display") as display:
        rv.show_report(result)
        assert display.call_count >= 5
    directory = rv.save_outputs(result, "outputs/live")
    rv.plot_report(result).savefig(directory / "havaintoarvio.png", dpi=130)
    print("OVATION:", rv.ovation_context(city))
    print("Arvioitavat tunnit:", int(result.hours.score.notna().sum()), "/", len(result.hours))
    for night, _, _ in result.nights:
        choices = rv.best_hours(result, night)
        print(night, [(str(item.time), item.site, item.score) for item in choices])
    # Colabin varsinaiset koodisolut suoritetaan samalla datalla ilman uutta API-hakua.
    notebook = json.loads(Path("Revontulivahti.ipynb").read_text(encoding="utf-8"))
    code_cells = ["".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code"]
    namespace = {}
    exec(compile(code_cells[1], "colab_engine", "exec"), namespace)
    exec(compile(code_cells[2], "colab_parameters", "exec"), namespace)
    namespace["choose_city"] = lambda *args: city
    namespace["run_forecast"] = lambda *args, **kwargs: result
    namespace["ovation_context"] = lambda *args: "Lähitilanne testattu erikseen."
    with patch("IPython.display.display"):
        exec(compile(code_cells[3], "colab_run", "exec"), namespace)
        assert namespace["tulos"] is result
        exec(compile(code_cells[4], "colab_export", "exec"), namespace)
    print("Kokonaisuustesti ja Colab-koodisolut OK.")


if __name__ == "__main__":
    main()
