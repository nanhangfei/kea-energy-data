"""Connection points, meters and registers for the cohort.

Small (500 households), so this writes plain CSVs that 03_reference.sql
stages and resolves. The CSVs carry BUSINESS keys only - icp_identifier,
serial_number - never surrogate ids, because every id column in the source
schema is GENERATED ALWAYS AS IDENTITY and resolving them is the loader's
job. That is the same seam the extract has to cross.

Deterministic throughout: a re-run produces byte-identical files.

    python3 pipeline/scripts/gen_source/gen_reference.py
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = Path("raw/lcl")

NETWORKS = {
    "VECT": ("Vector",                 "Auckland",     -36.8485, 174.7633),
    "ORON": ("Orion New Zealand",      "Christchurch", -43.5321, 172.6362),
    "WELL": ("Wellington Electricity", "Wellington",   -41.2866, 174.7756),
    "AURA": ("Aurora Energy",          "Waikato",      -37.7870, 175.2793),
}

STREETS = ["Kauri", "Rimu", "Totara", "Matai", "Kowhai", "Puriri", "Rata",
           "Nikau", "Karaka", "Hinau", "Tawa", "Miro"]
TYPES = ["Street", "Road", "Avenue", "Place", "Terrace", "Drive"]
SUBURBS = {
    "Auckland":     ["Mount Eden", "Ponsonby", "Papatoetoe", "Glen Innes", "Henderson"],
    "Christchurch": ["Riccarton", "Papanui", "Halswell", "Linwood", "Sumner"],
    "Wellington":   ["Karori", "Newtown", "Miramar", "Johnsonville", "Island Bay"],
    "Waikato":      ["Chartwell", "Hillcrest", "Frankton", "Rototuna", "Claudelands"],
}


def _h(*parts) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12], 16)


def main() -> None:
    spec = json.loads((HERE / "cohort.json").read_text())
    cohort = spec["households"]
    nz_start = dt.date.fromisoformat(spec["nz_span"][0])
    OUT.mkdir(parents=True, exist_ok=True)

    with (OUT / "networks.csv").open("w", newline="") as f:
        w = csv.writer(f)
        for code, (name, region, _, _) in NETWORKS.items():
            w.writerow([code, name, region])

    cps = (OUT / "connection_points.csv").open("w", newline="")
    mts = (OUT / "meters.csv").open("w", newline="")
    rgs = (OUT / "registers.csv").open("w", newline="")
    wc, wm, wr = csv.writer(cps), csv.writer(mts), csv.writer(rgs)

    for c in cohort:
        icp, net = c["icp"], c["network_code"]
        _, city, lat0, lon0 = NETWORKS[net]
        subs = SUBURBS[city]
        addr = (f"{_h(icp,'num') % 300 + 1} "
                f"{STREETS[_h(icp,'st') % len(STREETS)]} "
                f"{TYPES[_h(icp,'ty') % len(TYPES)]}")
        suburb = subs[_h(icp, "sub") % len(subs)]
        # Spread over ~2 km so the points are distinct without pretending
        # to be real addresses.
        lat = lat0 + (_h(icp, "lat") % 2000 - 1000) / 100000
        lon = lon0 + (_h(icp, "lon") % 2000 - 1000) / 100000
        conn = nz_start - dt.timedelta(days=200 + _h(icp, "conn") % 2800)
        wc.writerow([icp, net, f"{addr}, {suburb}", suburb, city,
                     f"{_h(icp,'pc') % 9000 + 1000}", c["climate_region"],
                     f"{lat:.6f}", f"{lon:.6f}", conn])

        serial = f"SM{_h(icp,'ser') % 10**8:08d}"
        wm.writerow([icp, serial, "true", conn])
        wr.writerow([icp, serial, "UN", "Grid import", "false", "import"])
        if c["has_solar"]:
            wr.writerow([icp, serial, "EX", "Solar export", "false", "export"])

    for f in (cps, mts, rgs):
        f.close()
    n_solar = sum(c["has_solar"] for c in cohort)
    print(f"{len(NETWORKS)} networks, {len(cohort)} connection points, "
          f"{len(cohort)} meters, {len(cohort) + n_solar} registers -> {OUT}/")


if __name__ == "__main__":
    main()
