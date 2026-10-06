#!/usr/bin/env python3
"""Validate collected observer inputs before analysis starts."""

import argparse
import json
import os
from pathlib import Path


def validate_handoff(output: Path, safe_outputs: Path, collector_exit: int) -> None:
    def fail(code, message, context=None):
        item = {
            "type": "missing_data",
            "data_type": "runtime observer required evidence",
            "reason": f"{code}: {message}"[:256],
            "context": json.dumps(context or {}, sort_keys=True, separators=(",", ":"))[:256],
            "alternatives": (
                "Review the seven-day diagnostic artifact; "
                "do not create a pull request or noop."
            ),
        }
        with safe_outputs.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item) + "\n")
        raise SystemExit(f"{code}: {message}")

    records = []
    for filename, code in (
        ("observer-manifest.json", "observer_manifest"),
        ("failure-index.json", "failure_index"),
    ):
        path = output / filename
        if not path.is_file():
            fail(f"{code}_missing", f"collector did not produce {filename}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            fail("observer_json_invalid", f"{type(error).__name__}: {error}")
        if not isinstance(value, dict):
            fail(f"{code}_invalid", f"{filename} root must be an object")
        records.append(value)
    manifest, index = records
    if collector_exit != 0 or manifest.get("complete") is not True:
        errors = manifest.get("errors")
        primary = (
            errors[0]
            if isinstance(errors, list) and errors and isinstance(errors[0], dict)
            else {}
        )
        fail(
            primary.get("code", "collector_incomplete"),
            f"{primary.get('message', 'collector marked the manifest incomplete without error detail')} "
            f"(collector exit {collector_exit})",
            primary.get("scope", {}),
        )

    checks = (
        (manifest.get("schemaVersion") == 1, "observer_schema_invalid", "manifest schemaVersion must be 1"),
        (manifest.get("kind") == "xharness.runtime-failure-observer.collect", "observer_kind_invalid", "manifest kind is not the collector contract"),
        (manifest.get("status") == "complete", "observer_status_invalid", "complete manifest status must be complete"),
        (not manifest.get("errors"), "observer_errors_present", "complete manifest contains fatal errors"),
        (not index.get("errors"), "failure_index_errors_present", "failure index contains fatal errors"),
        (isinstance(index.get("candidates"), list), "failure_index_candidates_invalid", "failure index candidates must be an array"),
        ((output / "hlx-snapshot").is_dir(), "snapshot_missing", "complete run did not produce hlx-snapshot"),
        (all((output / name).is_file() for name in ("analysis-inputs.json", "filed.tsv")), "analysis_inputs_missing", "collector did not prepare analysis inputs and filing ledger"),
    )
    for valid, code, message in checks:
        if not valid:
            fail(code, message)
    if manifest.get("empty") is not True and not index["candidates"]:
        fail("observer_candidates_missing", "non-empty observer manifest has no candidates")
    if manifest.get("empty") is True:
        with safe_outputs.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "type": "noop",
                "message": "The complete observer scan found no actionable XHarness candidates.",
            }) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    validate_handoff(
        Path(os.environ["OBSERVER_OUTPUT"]),
        Path(os.environ["GH_AW_SAFE_OUTPUTS"]),
        int(os.environ.get("OBSERVER_COLLECTOR_EXIT_CODE", "1")),
    )


if __name__ == "__main__":
    main()
