from pathlib import Path

from test_aegis_engine import live_engine


def test_live_dashboard_runs_measured_recovery(live_engine, monkeypatch):
    from streamlit.testing.v1 import AppTest
    import aegis_falkordb_engine

    live_engine.seed_tri_layer_universe()
    monkeypatch.setattr(aegis_falkordb_engine, "AegisGraphEngine", lambda **kwargs: live_engine)
    app = AppTest.from_file(str(Path(__file__).with_name("aegis_dashboard.py")), default_timeout=45).run()
    start = next((button for button in app.button if button.label == "Start measured services"), None)
    assert start is not None, "Dashboard must expose measured recovery"
    start.click().run()
    next(button for button in app.button if button.label == "Run measured recovery").click().run()
    assert not app.exception
    assert app.session_state["measured_recovery_result"]["status"] == "VERIFIED", app.session_state["measured_recovery_result"]
    assert app.session_state["measured_recovery_result"]["attempts"][-1]["observation"]["errors"] == 0
