#!/usr/bin/env python3
"""Generate submission.jsonl for the 30 canonical test pairs (challenge-brief.md §7.2).

Runs the official, deterministic dataset generator into a temporary directory
(nothing under dataset/ is written), then calls bot.compose() for each pair.

    python make_submission.py            # writes ./submission.jsonl
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from bot import compose

ROOT = Path(__file__).resolve().parent
FIELDS = ("test_id", "body", "cta", "send_as", "suppression_key", "rationale")


def load_dir(path: Path, key: str) -> dict[str, dict]:
    items = (json.loads(p.read_text()) for p in sorted(path.glob("*.json")))
    return {item[key]: item for item in items}


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="vera_expanded_") as tmp:
        subprocess.run([sys.executable, str(ROOT / "dataset" / "generate_dataset.py"),
                        "--seed-dir", str(ROOT / "dataset"), "--out", tmp], check=True, capture_output=True)
        base = Path(tmp)
        categories = load_dir(base / "categories", "slug")
        merchants = load_dir(base / "merchants", "merchant_id")
        customers = load_dir(base / "customers", "customer_id")
        triggers = load_dir(base / "triggers", "id")
        pairs = json.loads((base / "test_pairs.json").read_text())["pairs"]

        lines = []
        for pair in pairs:
            merchant = merchants[pair["merchant_id"]]
            result = compose(categories.get(merchant["category_slug"], {}), merchant, triggers[pair["trigger_id"]],
                             customers.get(pair.get("customer_id") or ""))
            lines.append(json.dumps({"test_id": pair["test_id"], **result}, ensure_ascii=False))

    out = ROOT / "submission.jsonl"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    empty = sum(1 for line in lines if not json.loads(line)["body"])
    print(f"wrote {out.name}: {len(lines)} lines ({empty} deliberate non-sends)")


if __name__ == "__main__":
    main()
