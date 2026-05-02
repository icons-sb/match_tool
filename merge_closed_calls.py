"""
merge_closed_calls.py
─────────────────────
Unisce i file closed_calls_YYYY.json prodotti dallo scraper per-anno
in un unico closed_calls.json + closed_calls_autofill_index.json.

Uso:
    python merge_closed_calls.py
    python merge_closed_calls.py --dir /path/to/jsons --out closed_calls.json
    python merge_closed_calls.py --years 2021 2022 2023 2024 2025
"""

import json
import argparse
from datetime import datetime, timezone
from pathlib import Path


def merge(source_dir: Path, out_path: Path, years: list[int] | None = None):
    current_year = datetime.now().year
    # Se non specificati, cerca tutti gli anni dal 2021 all'anno corrente
    if years is None:
        years = list(range(2021, current_year + 1))

    all_calls       = []
    all_autofill    = {}
    seen_urls       = set()
    year_stats      = {}

    for year in sorted(years):
        year_file = source_dir / f"closed_calls_{year}.json"
        if not year_file.exists():
            print(f"  ⚠️  {year_file} non trovato — saltato")
            continue

        print(f"  📂 Carico {year_file} …", end="", flush=True)
        data = json.loads(year_file.read_text(encoding="utf-8"))

        calls_year = data.get("calls", [])
        autofill_year = data.get("autofill_index", {})

        added = 0
        for c in calls_year:
            url = c.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_calls.append(c)
                added += 1

        # Merge autofill (existing keys preserved — first-write wins)
        for k, v in autofill_year.items():
            if k not in all_autofill:
                all_autofill[k] = v

        year_stats[year] = {"found": len(calls_year), "added": added}
        print(f" {len(calls_year)} call ({added} nuove dopo dedup)", flush=True)

    print(f"\nTotale call unite: {len(all_calls)}")
    print(f"Totale autofill entries: {len(all_autofill)}")

    generated = datetime.now(timezone.utc).isoformat()

    # ── closed_calls.json ────────────────────────────────────────────────────
    payload = {
        "generated":      generated,
        "status":         "closed",
        "total_scraped":  len(all_calls),
        "year_stats":     year_stats,
        "calls":          all_calls,
        "autofill_index": all_autofill,
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"✅ Scritto {out_path} ({len(all_calls)} call)")

    # ── closed_calls_autofill_index.json ─────────────────────────────────────
    index_path = out_path.parent / "closed_calls_autofill_index.json"
    index_path.write_text(
        json.dumps({"generated": generated, "index": all_autofill},
                   ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"✅ Scritto {index_path} ({len(all_autofill)} entries)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Merge file closed_calls_YYYY.json → closed_calls.json")
    parser.add_argument("--dir",   default=".", help="Cartella contenente i file per-anno (default: .)")
    parser.add_argument("--out",   default="closed_calls.json", help="File di output (default: closed_calls.json)")
    parser.add_argument("--years", type=int, nargs="+", default=None,
                        help="Anni da includere (default: 2021..anno corrente)")
    args = parser.parse_args()

    merge(Path(args.dir), Path(args.out), args.years)
