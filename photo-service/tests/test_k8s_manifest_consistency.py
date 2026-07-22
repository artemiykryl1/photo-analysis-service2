"""Consistency checks between the docker-compose config files and their
duplicated copies embedded in Kubernetes `ConfigMap`s (TASK-003 C3/C4,
tasks/TASK-003/20_design.md §9.2/§11.3).

`ConfigMap`s cannot mount a file that lives outside their own `data:` block,
so `photo-service/prometheus/rules.yml` and
`photo-service/grafana/dashboards/photo-service.json` (used by
`docker-compose.yml`) are duplicated, by hand, inside
`k8s/40-prometheus.yaml`/`k8s/41-grafana.yaml`'s embedded YAML literals.
30_impl.md documents that this was checked manually ("сверено скриптом")
during implementation - this file makes that check a permanent, automated
regression guard: it is trivially easy for the two copies to drift on the
next edit (spec.md explicitly calls this out: "регрессия - их легко
рассинхронизировать").

No cluster/kubectl/Docker needed - this only parses YAML/JSON text already
committed to the repository.
"""

import json
from pathlib import Path

import pytest
import yaml

PHOTO_SERVICE_ROOT = Path(__file__).resolve().parent.parent
K8S_DIR = PHOTO_SERVICE_ROOT / "k8s"


def _read(path: Path) -> str:
    if not path.exists():
        pytest.skip(f"{path} not found (k8s/ not part of this checkout)")
    return path.read_text(encoding="utf-8")


def _load_configmap_data(k8s_manifest_path: Path, configmap_name: str) -> dict:
    """Parse a multi-document k8s YAML file and return the `data:` mapping
    of the `ConfigMap` named `configmap_name`."""
    documents = yaml.safe_load_all(_read(k8s_manifest_path))
    for doc in documents:
        if not doc:
            continue
        if doc.get("kind") == "ConfigMap" and doc.get("metadata", {}).get("name") == (
            configmap_name
        ):
            return doc["data"]
    raise AssertionError(f"ConfigMap {configmap_name!r} not found in {k8s_manifest_path}")


class TestPrometheusRulesConsistency:
    """spec.md criterion: "Правила Prometheus: prometheus/rules.yml и
    встроенная копия в k8s/40-prometheus.yaml эквивалентны"."""

    def test_standalone_rules_file_and_k8s_configmap_copy_parse_to_the_same_structure(self):
        standalone = yaml.safe_load(_read(PHOTO_SERVICE_ROOT / "prometheus" / "rules.yml"))

        configmap_data = _load_configmap_data(
            K8S_DIR / "40-prometheus.yaml", "prometheus-config"
        )
        embedded = yaml.safe_load(configmap_data["rules.yml"])

        assert embedded == standalone

    def test_rules_file_declares_all_four_documented_alerts(self):
        """Cheap sanity check independent of the byte-for-byte comparison
        above - if BOTH copies were edited identically but an alert was
        silently dropped from the design's set, the equality check alone
        would not catch it."""
        standalone = yaml.safe_load(_read(PHOTO_SERVICE_ROOT / "prometheus" / "rules.yml"))
        alert_names = {
            rule["alert"]
            for group in standalone["groups"]
            for rule in group["rules"]
        }
        assert alert_names == {
            "AnalysisFailureRateHigh",
            "AnalyzerMessageTooLarge",
            "AnalyzerUnavailable",
            "WorkerStalled",
        }

    def test_worker_stalled_alert_aggregates_both_sides_with_sum(self):
        """Review-1 NB-13 regression: `worker_messages_processed_total`
        carries a `result` label (job=photo-worker) while `photos_pending`
        is an unlabeled gauge (job=photo-api) - without `sum(...)` wrapping
        both sides, PromQL's `and` operator intersects on an always-empty
        label set and the alert can never fire."""
        standalone = yaml.safe_load(_read(PHOTO_SERVICE_ROOT / "prometheus" / "rules.yml"))
        rules = [rule for group in standalone["groups"] for rule in group["rules"]]
        worker_stalled = next(r for r in rules if r["alert"] == "WorkerStalled")

        expr = worker_stalled["expr"]
        assert "sum(increase(worker_messages_processed_total" in expr
        assert "sum(photos_pending)" in expr


class TestGrafanaProvisioningConsistency:
    """Bonus consistency guard alongside the Prometheus rules check - the
    same "ConfigMap can't mount an external file" duplication problem
    applies to the dashboard JSON and datasource provisioning (review-1
    NB-3/MAJOR-3: a missing/mismatched datasource `uid` breaks every panel)."""

    def test_dashboard_json_matches_the_k8s_configmap_copy(self):
        standalone = json.loads(
            _read(PHOTO_SERVICE_ROOT / "grafana" / "dashboards" / "photo-service.json")
        )

        configmap_data = _load_configmap_data(K8S_DIR / "41-grafana.yaml", "grafana-dashboards")
        embedded = json.loads(configmap_data["photo-service.json"])

        assert embedded == standalone

    def test_datasource_provisioning_matches_the_k8s_configmap_copy(self):
        standalone = yaml.safe_load(
            _read(
                PHOTO_SERVICE_ROOT
                / "grafana"
                / "provisioning"
                / "datasources"
                / "prometheus.yml"
            )
        )

        configmap_data = _load_configmap_data(K8S_DIR / "41-grafana.yaml", "grafana-provisioning")
        embedded = yaml.safe_load(configmap_data["datasource.yml"])

        assert embedded == standalone

    def test_datasource_uid_matches_every_dashboard_panel_datasource_uid(self):
        """review-1 NB-3/MAJOR-3: without a matching, explicit `uid` on
        both sides, Grafana assigns a random one on provisioning and every
        panel fails with "Datasource not found"."""
        datasource = yaml.safe_load(
            _read(
                PHOTO_SERVICE_ROOT
                / "grafana"
                / "provisioning"
                / "datasources"
                / "prometheus.yml"
            )
        )
        datasource_uid = datasource["datasources"][0]["uid"]

        dashboard = json.loads(
            _read(PHOTO_SERVICE_ROOT / "grafana" / "dashboards" / "photo-service.json")
        )
        panel_uids = {panel["datasource"]["uid"] for panel in dashboard["panels"]}

        assert panel_uids == {datasource_uid}
