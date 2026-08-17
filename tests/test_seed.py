"""Unit tests for the seed CLI helpers (no DB round trips)."""

from __future__ import annotations

import uuid

import pytest
from app.cli import CliError, IdMap, _order_seq, build_parser, remap_template_doc


def test_idmap_allocates_once_and_resolves() -> None:
    ids = IdMap()
    first = ids.alloc("b1")
    assert ids.alloc("b1") == first
    assert ids.get("b1") == first
    assert ids.many(["b1"]) == [first]
    assert ids.opt(None) is None
    assert "b2" not in ids
    with pytest.raises(CliError):
        ids.get("b2")


def test_idmap_alias_and_roundtrip() -> None:
    real = uuid.uuid4()
    ids = IdMap()
    ids.alias("rg12", real)
    assert ids.get("rg12") == real
    restored = IdMap(ids.as_dict())
    assert restored.get("rg12") == real


def test_remap_template_doc_rewrites_only_known_assets() -> None:
    ids = IdMap()
    logo = ids.alloc("as_logo")
    doc = {
        "paper": "A4",
        "elements": [
            {"id": "el_0", "type": "image", "assetId": "as_logo"},
            {"id": "el_1", "type": "image", "assetId": "data:image/png;base64,AAAA"},
            {"id": "el_2", "type": "text", "text": "{company.name}"},
        ],
    }
    out = remap_template_doc(doc, ids)
    assert out["elements"][0]["assetId"] == str(logo)
    assert out["elements"][1]["assetId"] == "data:image/png;base64,AAAA"
    assert out["elements"][2] == {"id": "el_2", "type": "text", "text": "{company.name}"}
    assert doc["elements"][0]["assetId"] == "as_logo"  # input untouched


def test_order_seq_and_parser() -> None:
    assert _order_seq("UR-000813") == 813
    assert _order_seq("weird") == 0
    args = build_parser().parse_args(["seed-demo", "--with-transactions"])
    assert args.command == "seed-demo" and args.with_transactions is True
