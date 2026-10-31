"""E2E の FHIR スタブが配信する Bundle を組むヘルパ。

`sources.py` が患者ごとの Patient（crosswalk）と Observation をこの形で書き出し、
`fhir_stub.py` がそれをそのまま返す。
"""

from __future__ import annotations

from typing import Any


def bundle(resources: list[dict[str, Any]]) -> dict[str, Any]:
    """FHIR の searchset Bundle を組む。"""
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "entry": [{"resource": r} for r in resources],
    }
