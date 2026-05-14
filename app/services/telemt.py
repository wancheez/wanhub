"""Fetch + parse the telemt proxy Prometheus endpoint.

Returns a curated TelemtSnapshot — a small subset of the ~500-line Prometheus
dump that's actually interesting to look at on a dashboard. Raw dump is not
exposed; if we ever need it, the operator runs curl directly.
"""

import re
import time

import httpx

from app.core.config import TELEMT_METRICS_URL
from app.schemas.telemt import (
    TelemtIpTracker,
    TelemtSnapshot,
    TelemtTlsProfile,
    TelemtUser,
)


class TelemtUnavailable(Exception):
    """Either the URL is unset or the upstream is unreachable / malformed."""


_LINE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>[^}]*)\})?"
    r"\s+(?P<value>\S+)"
)
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')

# Sample is (labels, value). One metric name can have many samples (one per
# label permutation, e.g. histogram buckets or per-user series).
ParsedMetrics = dict[str, list[tuple[dict[str, str], float]]]


def parse_metrics(text: str) -> ParsedMetrics:
    out: ParsedMetrics = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _LINE_RE.match(s)
        if not m:
            continue
        try:
            value = float(m.group("value"))
        except ValueError:
            continue
        labels_raw = m.group("labels") or ""
        labels = dict(_LABEL_RE.findall(labels_raw))
        out.setdefault(m.group("name"), []).append((labels, value))
    return out


def _scalar(metrics: ParsedMetrics, name: str) -> float:
    samples = metrics.get(name)
    return samples[0][1] if samples else 0.0


def _label_value(metrics: ParsedMetrics, name: str, label: str) -> str | None:
    """Return the label value of the first sample whose value is 1 — for
    info-style gauges like telemt_build_info{version="..."} 1.
    """
    for labels, value in metrics.get(name, []):
        if value == 1.0 and label in labels:
            return labels[label]
    return None


def _labeled_value(metrics: ParsedMetrics, name: str, label: str, target: str) -> float:
    """Lookup the value of the first sample where labels[label] == target."""
    for labels, value in metrics.get(name, []):
        if labels.get(label) == target:
            return value
    return 0.0


def _buckets(metrics: ParsedMetrics, name: str) -> dict[str, int]:
    """Collect histogram-like samples into a {bucket_label: count} dict,
    preserving the order they appeared in the source (Python dicts keep
    insertion order, so the UI can iterate in low→high bucket order).
    """
    out: dict[str, int] = {}
    for labels, value in metrics.get(name, []):
        if b := labels.get("bucket"):
            out[b] = int(value)
    return out


def _user_metric(metrics: ParsedMetrics, name: str, user: str) -> float:
    return _labeled_value(metrics, name, "user", user)


def _users(metrics: ParsedMetrics) -> list[TelemtUser]:
    seen: set[str] = set()
    for name in (
        "telemt_user_connections_current",
        "telemt_user_connections_total",
        "telemt_user_unique_ips_current",
    ):
        for labels, _ in metrics.get(name, []):
            if u := labels.get("user"):
                seen.add(u)

    return sorted(
        (
            TelemtUser(
                user=u,
                connections_current=int(
                    _user_metric(metrics, "telemt_user_connections_current", u)
                ),
                connections_total=int(_user_metric(metrics, "telemt_user_connections_total", u)),
                octets_from_client=int(_user_metric(metrics, "telemt_user_octets_from_client", u)),
                octets_to_client=int(_user_metric(metrics, "telemt_user_octets_to_client", u)),
                unique_ips_current=int(_user_metric(metrics, "telemt_user_unique_ips_current", u)),
                unique_ips_recent_window=int(
                    _user_metric(metrics, "telemt_user_unique_ips_recent_window", u)
                ),
                unique_ips_limit=int(_user_metric(metrics, "telemt_user_unique_ips_limit", u)),
            )
            for u in seen
        ),
        key=lambda x: x.user,
    )


def _tls_profiles(metrics: ParsedMetrics) -> list[TelemtTlsProfile]:
    domains: set[str] = set()
    for labels, value in metrics.get("telemt_tls_front_profile_info", []):
        if value == 1.0 and (d := labels.get("domain")):
            domains.add(d)
    return sorted(
        (
            TelemtTlsProfile(
                domain=d,
                age_seconds=int(
                    _labeled_value(metrics, "telemt_tls_front_profile_age_seconds", "domain", d)
                ),
                app_data_records=int(
                    _labeled_value(
                        metrics, "telemt_tls_front_profile_app_data_records", "domain", d
                    )
                ),
                ticket_records=int(
                    _labeled_value(metrics, "telemt_tls_front_profile_ticket_records", "domain", d)
                ),
                app_data_bytes=int(
                    _labeled_value(metrics, "telemt_tls_front_profile_app_data_bytes", "domain", d)
                ),
            )
            for d in domains
        ),
        key=lambda x: x.domain,
    )


def _ip_tracker(metrics: ParsedMetrics) -> TelemtIpTracker:
    return TelemtIpTracker(
        users_active=int(_labeled_value(metrics, "telemt_ip_tracker_users", "scope", "active")),
        users_recent=int(_labeled_value(metrics, "telemt_ip_tracker_users", "scope", "recent")),
        entries_active=int(_labeled_value(metrics, "telemt_ip_tracker_entries", "scope", "active")),
        entries_recent=int(_labeled_value(metrics, "telemt_ip_tracker_entries", "scope", "recent")),
        cap_rejects_active=int(
            _labeled_value(metrics, "telemt_ip_tracker_cap_rejects_total", "scope", "active")
        ),
        cap_rejects_recent=int(
            _labeled_value(metrics, "telemt_ip_tracker_cap_rejects_total", "scope", "recent")
        ),
    )


def _writer_pick_mode(metrics: ParsedMetrics) -> str | None:
    """Return the writer-pick mode with the most success_try picks, or None
    if no mode has activity yet. After a runtime switch both modes can have
    non-zero counts — we pick the dominant one.
    """
    best: tuple[str, float] | None = None
    for labels, value in metrics.get("telemt_me_writer_pick_total", []):
        if labels.get("result") == "success_try" and value > 0:
            mode = labels.get("mode")
            if mode and (best is None or value > best[1]):
                best = (mode, value)
    return best[0] if best else None


def build_snapshot(text: str) -> TelemtSnapshot:
    m = parse_metrics(text)
    return TelemtSnapshot(
        version=_label_value(m, "telemt_build_info", "version"),
        uptime_seconds=round(_scalar(m, "telemt_uptime_seconds"), 1),
        connections_total=int(_scalar(m, "telemt_connections_total")),
        connections_bad_total=int(_scalar(m, "telemt_connections_bad_total")),
        handshake_timeouts_total=int(_scalar(m, "telemt_handshake_timeouts_total")),
        auth_expensive_checks_total=int(_scalar(m, "telemt_auth_expensive_checks_total")),
        auth_budget_exhausted_total=int(_scalar(m, "telemt_auth_budget_exhausted_total")),
        upstream_connect_attempt_total=int(_scalar(m, "telemt_upstream_connect_attempt_total")),
        upstream_connect_success_total=int(_scalar(m, "telemt_upstream_connect_success_total")),
        upstream_connect_fail_total=int(_scalar(m, "telemt_upstream_connect_fail_total")),
        upstream_connect_duration_success=_buckets(
            m, "telemt_upstream_connect_duration_success_total"
        ),
        upstream_connect_duration_fail=_buckets(m, "telemt_upstream_connect_duration_fail_total"),
        d2c_batches_total=int(_scalar(m, "telemt_me_d2c_batches_total")),
        d2c_batch_frames_total=int(_scalar(m, "telemt_me_d2c_batch_frames_total")),
        d2c_batch_bytes_total=int(_scalar(m, "telemt_me_d2c_batch_bytes_total")),
        me_writers_active=int(_scalar(m, "telemt_me_writers_active_current")),
        me_writers_warm=int(_scalar(m, "telemt_me_writers_warm_current")),
        me_writers_target=int(_scalar(m, "telemt_me_adaptive_floor_target_writers_total")),
        me_reconnect_attempts_total=int(_scalar(m, "telemt_me_reconnect_attempts_total")),
        me_reconnect_success_total=int(_scalar(m, "telemt_me_reconnect_success_total")),
        me_handshake_reject_total=int(_scalar(m, "telemt_me_handshake_reject_total")),
        me_endpoint_quarantine_total=int(_scalar(m, "telemt_me_endpoint_quarantine_total")),
        me_endpoint_quarantine_unexpected_total=int(
            _scalar(m, "telemt_me_endpoint_quarantine_unexpected_total")
        ),
        me_shadow_rotate_total=int(_scalar(m, "telemt_me_single_endpoint_shadow_rotate_total")),
        me_writer_pick_mode=_writer_pick_mode(m),
        me_floor_mode=_label_value(m, "telemt_me_floor_mode", "mode"),
        me_fair_active_flows=int(_scalar(m, "telemt_me_fair_active_flows")),
        me_fair_queued_bytes=int(_scalar(m, "telemt_me_fair_queued_bytes")),
        me_fair_pressure_state=int(_scalar(m, "telemt_me_fair_pressure_state")),
        desync_total=int(_scalar(m, "telemt_desync_total")),
        secure_padding_invalid_total=int(_scalar(m, "telemt_secure_padding_invalid_total")),
        crc_mismatch_total=int(_scalar(m, "telemt_me_crc_mismatch_total")),
        seq_mismatch_total=int(_scalar(m, "telemt_me_seq_mismatch_total")),
        route_drop_no_conn_total=int(_scalar(m, "telemt_me_route_drop_no_conn_total")),
        route_drop_queue_full_total=int(_scalar(m, "telemt_me_route_drop_queue_full_total")),
        tls_profiles=_tls_profiles(m),
        ip_tracker=_ip_tracker(m),
        users=_users(m),
        fetched_at_unix=time.time(),
    )


_FETCH_TIMEOUT_S = 4.0


async def get_telemt_snapshot() -> TelemtSnapshot:
    if not TELEMT_METRICS_URL:
        raise TelemtUnavailable("TELEMT_METRICS_URL is not set")
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_S) as client:
            resp = await client.get(TELEMT_METRICS_URL)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        raise TelemtUnavailable(str(e)) from e
    return build_snapshot(resp.text)
