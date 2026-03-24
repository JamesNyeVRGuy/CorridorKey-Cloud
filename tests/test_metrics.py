"""Tests for the Prometheus metrics endpoint (CRKY-27).

Covers: metric formatting, request counting, all metric categories,
and integration with TestClient.
"""

import os

import pytest


class TestMetricFormatting:
    """Test the Prometheus text format helpers."""

    def test_basic_metric(self):
        from web.api.metrics import _m

        result = _m("test_gauge", 42, "A test gauge")
        assert "# HELP test_gauge A test gauge" in result
        assert "# TYPE test_gauge gauge" in result
        assert "test_gauge 42" in result

    def test_metric_with_labels(self):
        from web.api.metrics import _m

        result = _m("test_counter", 10, "A counter", "counter", 'job="test"')
        assert "# TYPE test_counter counter" in result
        assert 'test_counter{job="test"} 10' in result

    def test_labeled_metric(self):
        from web.api.metrics import _l

        result = _l("cpu_percent", 75.5, 'node="box-a"')
        assert 'cpu_percent{node="box-a"} 75.5' in result

    def test_request_counter(self):
        from web.api.metrics import _request_count, increment_request_count

        before = _request_count
        increment_request_count()
        from web.api.metrics import _request_count as after

        assert after == before + 1


class TestMetricsEndpoint:
    """Integration tests for /metrics."""

    @pytest.fixture
    def client(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CK_AUTH_ENABLED", "false")
        monkeypatch.setenv("CK_DOCS_PUBLIC", "true")
        monkeypatch.setenv("CK_METRICS_ENABLED", "true")
        monkeypatch.setenv("CK_CLIPS_DIR", str(tmp_path / "clips"))
        os.makedirs(tmp_path / "clips", exist_ok=True)

        import importlib

        import web.api.openapi_config as oc

        importlib.reload(oc)

        import web.api.auth as auth_mod

        importlib.reload(auth_mod)

        # Force metrics enabled at module level
        import web.api.metrics as metrics_mod

        monkeypatch.setattr(metrics_mod, "METRICS_ENABLED", True)

        from web.api.app import create_app

        app = create_app()
        from fastapi.testclient import TestClient

        return TestClient(app, raise_server_exceptions=False)

    def test_metrics_enabled(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200
        text = resp.text
        assert "corridorkey_uptime_seconds" in text
        assert "corridorkey_jobs_running" in text
        assert "corridorkey_jobs_queued" in text
        assert "corridorkey_nodes_total" in text

    def test_metrics_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CK_AUTH_ENABLED", "false")
        monkeypatch.setenv("CK_DOCS_PUBLIC", "true")
        monkeypatch.setenv("CK_METRICS_ENABLED", "false")
        monkeypatch.setenv("CK_CLIPS_DIR", str(tmp_path / "clips2"))
        os.makedirs(tmp_path / "clips2", exist_ok=True)

        import importlib

        import web.api.openapi_config as oc

        importlib.reload(oc)

        import web.api.auth as auth_mod

        importlib.reload(auth_mod)

        # Reload metrics to pick up env change
        import web.api.metrics as metrics_mod

        monkeypatch.setattr(metrics_mod, "METRICS_ENABLED", False)

        from web.api.app import create_app

        app = create_app()
        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "Metrics disabled" in resp.text

    def test_metrics_has_job_throughput(self, client):
        resp = client.get("/metrics")
        assert "corridorkey_jobs_completed_last_hour" in resp.text

    def test_metrics_has_ws_connections(self, client):
        resp = client.get("/metrics")
        assert "corridorkey_ws_connections" in resp.text

    def test_metrics_has_disk_space(self, client):
        resp = client.get("/metrics")
        assert "corridorkey_disk_free_gb" in resp.text

    def test_metrics_has_request_counter(self, client):
        resp = client.get("/metrics")
        assert "corridorkey_api_requests_total" in resp.text

    def test_metrics_is_prometheus_format(self, client):
        resp = client.get("/metrics")
        # Every HELP line should have a corresponding TYPE line
        lines = resp.text.split("\n")
        help_names = set()
        type_names = set()
        for line in lines:
            if line.startswith("# HELP "):
                name = line.split()[2]
                help_names.add(name)
            elif line.startswith("# TYPE "):
                name = line.split()[2]
                type_names.add(name)
        # Every metric with HELP should also have TYPE
        assert help_names <= type_names or help_names == type_names


class TestMonitoringConfig:
    """Test that monitoring config files are valid."""

    def test_prometheus_config_exists(self):
        path = os.path.join(os.path.dirname(__file__), "..", "deploy", "monitoring", "prometheus", "prometheus.yml")
        assert os.path.isfile(path)

    def test_prometheus_alerts_exist(self):
        path = os.path.join(os.path.dirname(__file__), "..", "deploy", "monitoring", "prometheus", "alerts.yml")
        assert os.path.isfile(path)

    def test_grafana_datasource_exists(self):
        path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "deploy",
            "monitoring",
            "grafana",
            "provisioning",
            "datasources",
            "prometheus.yml",
        )
        assert os.path.isfile(path)

    def test_grafana_dashboards_exist(self):
        dashboards_dir = os.path.join(os.path.dirname(__file__), "..", "deploy", "monitoring", "grafana", "dashboards")
        assert os.path.isdir(dashboards_dir)
        files = os.listdir(dashboards_dir)
        assert "platform-overview.json" in files
        assert "node-detail.json" in files

    def test_compose_monitoring_exists(self):
        path = os.path.join(os.path.dirname(__file__), "..", "deploy", "docker-compose.monitoring.yml")
        assert os.path.isfile(path)
