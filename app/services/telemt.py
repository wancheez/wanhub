"""Fetch + parse the telemt proxy Prometheus endpoint.

Returns a curated TelemtSnapshot — a small subset of the ~500-line Prometheus
dump that's actually interesting to look at on a dashboard. Raw dump is not
exposed; if we ever need it, the operator runs curl directly.
"""

import re
import time

import httpx

from app.core.config import TELEMT_METRICS_URL
from app.schemas.telemt import TelemtSnapshot, TelemtUser


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


def _user_metric(metrics: ParsedMetrics, name: str, user: str) -> float:
    for labels, value in metrics.get(name, []):
        if labels.get("user") == user:
            return value
    return 0.0


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


def build_snapshot(text: str) -> TelemtSnapshot:
    m = parse_metrics(text)
    return TelemtSnapshot(
        version=_label_value(m, "telemt_build_info", "version"),
        uptime_seconds=round(_scalar(m, "telemt_uptime_seconds"), 1),
        connections_total=int(_scalar(m, "telemt_connections_total")),
        connections_bad_total=int(_scalar(m, "telemt_connections_bad_total")),
        handshake_timeouts_total=int(_scalar(m, "telemt_handshake_timeouts_total")),
        upstream_connect_attempt_total=int(_scalar(m, "telemt_upstream_connect_attempt_total")),
        upstream_connect_success_total=int(_scalar(m, "telemt_upstream_connect_success_total")),
        upstream_connect_fail_total=int(_scalar(m, "telemt_upstream_connect_fail_total")),
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
        desync_total=int(_scalar(m, "telemt_desync_total")),
        secure_padding_invalid_total=int(_scalar(m, "telemt_secure_padding_invalid_total")),
        crc_mismatch_total=int(_scalar(m, "telemt_me_crc_mismatch_total")),
        seq_mismatch_total=int(_scalar(m, "telemt_me_seq_mismatch_total")),
        route_drop_no_conn_total=int(_scalar(m, "telemt_me_route_drop_no_conn_total")),
        route_drop_queue_full_total=int(_scalar(m, "telemt_me_route_drop_queue_full_total")),
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
