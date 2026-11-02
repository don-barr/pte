"""Cut a small test slice out of a real in-network file.

Keeps the header fields, every in_network item whose billing_code is listed, and only
the provider_references those items cite.
"""

import argparse
import json
from pathlib import Path

HEADER_FIELDS = ("reporting_entity_name", "reporting_entity_type", "version", "last_updated_on")


def make_fixture(data: dict, codes: set[str]) -> dict:
    items = [item for item in data["in_network"] if item["billing_code"] in codes]
    cited = {
        ref
        for item in items
        for rate in item["negotiated_rates"]
        for ref in rate["provider_references"]
    }
    fixture = {field: data[field] for field in HEADER_FIELDS if field in data}
    fixture["provider_references"] = [
        ref for ref in data.get("provider_references", []) if ref["provider_group_id"] in cited
    ]
    fixture["in_network"] = items
    return fixture


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="real in-network JSON file")
    parser.add_argument("output", type=Path, help="where to write the slice")
    parser.add_argument("codes", nargs="+", help="billing codes to keep")
    args = parser.parse_args()

    with args.source.open() as f:
        data = json.load(f)
    fixture = make_fixture(data, set(args.codes))
    del data
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(fixture, indent=2) + "\n")
    print(
        f"{args.output}: {len(fixture['in_network'])} items, "
        f"{len(fixture['provider_references'])} provider references"
    )


if __name__ == "__main__":
    main()
