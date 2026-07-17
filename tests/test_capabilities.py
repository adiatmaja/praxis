import logging

from orchestrator.core.capabilities import CapabilityCatalog


def test_loads_bundled_snapshot():
    catalog = CapabilityCatalog()

    # Verify that it loaded the default path correctly
    assert catalog.as_of == "2026-07-17"
    all_models = catalog.all()
    assert len(all_models) == 4

    # Check one specific model
    opus = catalog.for_model("claude-opus-4-8")
    assert opus is not None
    assert opus["swe_bench_verified"] == 0.79
    assert opus["agentic_coding"] == "high"
    assert opus["speed_tps"] == 62
    assert opus["price_per_mtok_blended"] == 15.0


def test_missing_model_returns_none():
    catalog = CapabilityCatalog()
    assert catalog.for_model("nonexistent-model") is None


def test_missing_file_yields_empty_catalog(tmp_path, caplog):
    nonexistent_path = str(tmp_path / "does_not_exist.json")
    with caplog.at_level(logging.INFO):
        catalog = CapabilityCatalog(path=nonexistent_path)

    assert catalog.all() == {}
    assert catalog.as_of is None
    assert catalog.for_model("gemini-3-pro") is None

    assert any(
        "Capability snapshot not found" in record.message for record in caplog.records
    )


def test_bad_json_yields_empty_catalog(tmp_path, caplog):
    bad_json_path = tmp_path / "bad.json"
    bad_json_path.write_text("{ this is not valid json }")

    with caplog.at_level(logging.WARNING):
        catalog = CapabilityCatalog(path=str(bad_json_path))

    assert catalog.all() == {}
    assert catalog.as_of is None

    assert any(
        "Failed to parse capability snapshot" in record.message
        for record in caplog.records
    )


def test_all_returns_full_map():
    catalog = CapabilityCatalog()
    all_models = catalog.all()

    expected_models = {
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        "gemini-3-pro",
    }
    assert set(all_models.keys()) == expected_models
