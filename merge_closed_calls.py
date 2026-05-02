"""
merge_closed_calls.py
─────────────────────
Unisce i file closed_calls_batch_N.json prodotti dallo scraper in batch
in un unico closed_calls.json + closed_calls_autofill_index.json.

Uso:
    python merge_closed_calls.py
    python merge_closed_calls.py --dir /path/to/jsons --out closed_calls.json
    python merge_closed_calls.py --total-batches 5
"""

import json
import argparse
from datetime import datetime, timezone
from pathlib import Path


def merge(source_dir: Path, out_path: Path, total_batches: int = 5):
    all_calls    = []
    all_autofill = {}
    seen_urls    = set()
    batch_stats  = {}

    for n in range(1, total_batches + 1):
        batch_file = source_dir / f"closed_calls_batch_{n}.json"
        if not batch_file.exists():
            print(f"  ⚠️  {batch_file} non trovato — saltato")
            continue

        print(f"  Carico {batch_file} ...", end="", flush=True)
        data = json.loads(batch_file.read_text(encoding="utf-8"))

        calls_batch   = data.get("calls", [])
        autofill_batch = data.get("autofill_index", {})

        added = 0
        for c in calls_batch:
            url = c.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_calls.append(c)
                added += 1

        # Merge autofill (first-write wins per evitare sovrascritture)
        for k, v in autofill_batch.items():
            if k not in all_autofill:
                all_autofill[k] = v

        page_from = data.get("page_from", "?")
        page_to   = data.get("page_to",   "?")
        batch_stats[n] = {"found": len(calls_batch), "added": added,
                          "pages": f"{page_from}-{page_to}"}
        print(f" pagine {page_from}-{page_to} | {len(calls_batch)} call ({added} nuove dopo dedup)", flush=True)

    print(f"\nTotale call unite:      {len(all_calls)}")
    print(f"Totale autofill entries: {len(all_autofill)}")

    generated = datetime.now(timezone.utc).isoformat()

    # ── closed_calls.json ─────────────────────────────────────────────────────
    payload = {
        "generated":      generated,
        "status":         "closed",
        "total_scraped":  len(all_calls),
        "batch_stats":    batch_stats,
        "calls":          all_calls,
        "autofill_index": all_autofill,
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Scritto {out_path} ({len(all_calls)} call)")

    # ── closed_calls_autofill_index.json ──────────────────────────────────────
    index_path = out_path.parent / "closed_calls_autofill_index.json"
    index_path.write_text(
        json.dumps({"generated": generated, "index": all_autofill},
                   ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"Scritto {index_path} ({len(all_autofill)} entries)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Merge file closed_calls_batch_N.json -> closed_calls.json"
    )
    parser.add_argument("--dir",           default=".",
                        help="Cartella contenente i file batch (default: .)")
    parser.add_argument("--out",           default="closed_calls.json",
                        help="File di output (default: closed_calls.json)")
    parser.add_argument("--total-batches", type=int, default=5,
                        help="Numero totale di batch (default: 5)")
    args = parser.parse_args()

    merge(Path(args.dir), Path(args.out), args.total_batches)
