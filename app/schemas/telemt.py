from pydantic import BaseModel


class TelemtUser(BaseModel):
    user: str
    connections_current: int
    connections_total: int
    octets_from_client: int
    octets_to_client: int
    unique_ips_current: int
    unique_ips_recent_window: int
    unique_ips_limit: int


class TelemtSnapshot(BaseModel):
    version: str | None
    uptime_seconds: float
    connections_total: int
    connections_bad_total: int
    handshake_timeouts_total: int
    upstream_connect_attempt_total: int
    upstream_connect_success_total: int
    upstream_connect_fail_total: int
    me_writers_active: int
    me_writers_warm: int
    me_writers_target: int
    me_reconnect_attempts_total: int
    me_reconnect_success_total: int
    me_handshake_reject_total: int
    me_endpoint_quarantine_total: int
    me_endpoint_quarantine_unexpected_total: int
    desync_total: int
    secure_padding_invalid_total: int
    crc_mismatch_total: int
    seq_mismatch_total: int
    route_drop_no_conn_total: int
    route_drop_queue_full_total: int
    users: list[TelemtUser]
    fetched_at_unix: float
