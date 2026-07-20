"""Listing analysis contracts: per-slide coverage and the two completeness gates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import slide_contract as sc


def _originals(tmp_path: Path, count: int) -> Path:
    d = tmp_path / "original"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(1, count + 1):
        (d / f"{i:02d}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return d


def _contract(index: int, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "index": index,
        "sells": "custom connect sounds",
        "hero": "app UI inside the car's dashboard screen",
        "key_elements": ["dashboard UI", "social proof badge"],
    }
    base.update(over)
    return base


def _write(contracts_dir: Path, index: int, payload: dict[str, Any] | str) -> None:
    contracts_dir.mkdir(parents=True, exist_ok=True)
    path = contracts_dir / sc.CONTRACT_NAME.format(index=index)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))


def test_verify_passes_when_every_slide_has_a_contract(tmp_path: Path) -> None:
    originals = _originals(tmp_path, 3)
    contracts = tmp_path / "contracts"
    for i in (1, 2, 3):
        _write(contracts, i, _contract(i))

    loaded = sc.verify_analysis(originals, contracts)
    assert [c["index"] for c in loaded] == [1, 2, 3]


def test_verify_blocks_and_names_the_unanalysed_slides(tmp_path: Path) -> None:
    # the exact failure the pipeline used to hide: 8 sources in, 5 slides out
    originals = _originals(tmp_path, 8)
    contracts = tmp_path / "contracts"
    for i in (1, 2, 3, 4, 5):
        _write(contracts, i, _contract(i))

    with pytest.raises(sc.IncompleteAnalysisError) as err:
        sc.verify_analysis(originals, contracts)
    message = str(err.value)
    assert "Не все экраны были проанализированы" in message
    assert "5 из 8" in message
    assert "06" in message and "07" in message and "08" in message


def test_verify_rejects_a_broken_or_empty_contract(tmp_path: Path) -> None:
    originals = _originals(tmp_path, 2)
    contracts = tmp_path / "contracts"
    _write(contracts, 1, _contract(1))
    _write(contracts, 2, "{not json")
    with pytest.raises(sc.IncompleteAnalysisError, match="битый JSON"):
        sc.verify_analysis(originals, contracts)

    _write(contracts, 2, {"index": 2})  # present but says nothing
    with pytest.raises(sc.IncompleteAnalysisError, match="пустой контракт"):
        sc.verify_analysis(originals, contracts)


def test_verify_refuses_when_there_is_nothing_to_analyse(tmp_path: Path) -> None:
    empty = tmp_path / "original"
    empty.mkdir()
    with pytest.raises(sc.IncompleteAnalysisError, match="ни одного экрана"):
        sc.verify_analysis(empty, tmp_path / "contracts")


def test_missing_slides_reports_what_rendering_dropped(tmp_path: Path) -> None:
    contracts = [_contract(i) for i in (1, 2, 3, 4)]
    rendered = [tmp_path / "01.png", tmp_path / "03.png"]
    assert sc.missing_slides(contracts, rendered) == ["02", "04"]
    assert sc.missing_slides(contracts, [tmp_path / f"{i:02d}.png" for i in (1, 2, 3, 4)]) == []


def test_analyse_prompt_forbids_summarising_the_set() -> None:
    p = sc.ANALYSE_PROMPT
    assert "Analyse EVERY file" in p
    assert "do not describe two slides together" in p
    assert "each gets its own contract" in p
    # the anti-loss inventory is the point of the contract
    assert "key_elements" in p and "anti-loss list" in p
    # structural things that used to vanish must be called out by name
    for element in ("map", "chart", "player", "carousel", "bottom sheet", "floating button"):
        assert element in p


def test_analyse_prompt_records_rather_than_invents() -> None:
    assert "This is analysis; invent nothing here" in sc.ANALYSE_PROMPT
