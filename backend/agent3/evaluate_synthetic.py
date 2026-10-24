"""Evaluate Agent 3 against the synthetic ground-truth labels."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .database import DocumentRepository
from .document_reader import read_upload
from .extractor import build_extractor_from_environment
from .service import Agent3Service
from .synthetic_data import load_synthetic_dataset


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, nested in value.items():
            output.update(_flatten(nested, f"{prefix}.{key}" if prefix else key))
        return output
    return {prefix: value}


def evaluate_dataset(dataset_directory: str | Path) -> dict[str, Any]:
    dataset_path = Path(dataset_directory)
    manifest = load_synthetic_dataset(dataset_path)
    extractor = build_extractor_from_environment()
    field_total = 0
    field_correct = 0
    validation_total = 0
    validation_correct = 0
    document_type_correct = 0
    extraction_failures = 0

    with tempfile.TemporaryDirectory(prefix="ghostledger-eval-", dir=dataset_path) as temp_dir:
        service = Agent3Service(DocumentRepository(Path(temp_dir) / "evaluation.db"), extractor)
        for record in manifest["records"]:
            document_path = dataset_path / "documents" / record["filename"]
            outcome = service.analyze(
                read_upload(record["filename"], document_path.read_bytes(), "text/plain")
            )
            actual = outcome.model_dump(mode="json")
            expected = record["document"]
            if actual["document_type"] == expected["document_type"]:
                document_type_correct += 1
            if actual["validation"]["status"] == "extraction_failed":
                extraction_failures += 1

            expected_flat = _flatten(expected)
            actual_flat = _flatten({
                key: actual[key]
                for key in (
                    "document_type", "document_id", "issue_date", "due_date", "currency",
                    "payment_terms", "customer", "vendor", "financial_information",
                    "transaction_information", "line_items",
                )
            })
            for field, value in expected_flat.items():
                if value is None or field.startswith("line_items"):
                    continue
                field_total += 1
                if actual_flat.get(field) == value:
                    field_correct += 1

            expected_validation = record["expected_validation"]
            actual_codes = {finding["code"] for finding in actual["validation"]["inconsistencies"]}
            checks = [
                set(actual["validation"]["missing_fields"]) == set(expected_validation["missing_fields"]),
                set(expected_validation["inconsistency_codes"]).issubset(actual_codes),
                actual["validation"]["possible_duplicate"] == expected_validation["possible_duplicate"],
            ]
            validation_total += len(checks)
            validation_correct += sum(checks)

    count = len(manifest["records"])
    report = {
        "dataset": manifest["dataset"],
        "synthetic_only": True,
        "documents_evaluated": count,
        "extractor": type(extractor).__name__,
        "document_type_accuracy": round(document_type_correct / count, 4) if count else 0,
        "non_null_field_accuracy": round(field_correct / field_total, 4) if field_total else 0,
        "validation_check_accuracy": round(validation_correct / validation_total, 4) if validation_total else 0,
        "extraction_failures": extraction_failures,
        "evaluation_notes": (
            "This measures extraction against deterministic fictional labels. "
            "It is not a fraud, approval, or production-quality assessment."
        ),
    }
    return report


if __name__ == "__main__":
    location = Path(__file__).resolve().parents[1] / "synthetic_data"
    report = evaluate_dataset(location)
    report_path = location / "evaluation_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
