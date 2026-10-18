import pytest


def test_demo_graph_guard_prevents_accidental_production_reset():
    from aegis_demo import validate_demo_graph

    assert validate_demo_graph("aegis_demo_verified") == "aegis_demo_verified"
    assert validate_demo_graph("aegis_test_123") == "aegis_test_123"
    for name in ("aegis_production_brain", "", "other", "aegis_demo_../x"):
        with pytest.raises(ValueError):
            validate_demo_graph(name)
