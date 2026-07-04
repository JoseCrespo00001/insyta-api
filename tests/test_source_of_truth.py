"""AUD-1.4: _format_source_of_truth serializa la data adjunta para el judge."""

from __future__ import annotations

from app.workers.audit import _format_source_of_truth


def test_none_and_empty_return_none():
    assert _format_source_of_truth(None) is None
    assert _format_source_of_truth({}) is None


def test_serializes_prices_as_source_of_truth():
    block = _format_source_of_truth(
        {"precios": {"limpieza_profunda_50m2": 45000, "moneda": "ARS"}}
    )
    assert block is not None
    assert "FUENTE DE VERDAD" in block
    assert "45000" in block
    assert "[precios]" in block
    # instrucción anti falso-positivo de alucinación
    assert "alucinación" in block.lower()


def test_multiple_files_sorted():
    block = _format_source_of_truth(
        {"info": {"nombre": "AquaClean"}, "precios": {"x": 1}}
    )
    assert block is not None
    assert block.index("[info]") < block.index("[precios]")
