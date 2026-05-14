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


class TelemtTlsProfile(BaseModel):
    domain: str
    age_seconds: int
    app_data_records: int
    ticket_records: int
    app_data_bytes: int


class TelemtIpTracker(BaseModel):
    users_active: int
    users_recent: int
    entries_active: int
    entries_recent: int
    cap_rejects_active: int
    cap_rejects_recent: int


class TelemtSnapshot(BaseModel):
    version: str | None
    uptime_seconds: float

    connections_total: int
    connections_bad_total: int
    handshake_timeouts_total: int
    auth_expensive_checks_total: int
    auth_budget_exhausted_total: int

    upstream_connect_attempt_total: int
    upstream_connect_success_total: int
    upstream_connect_fail_total: int
    upstream_connect_duration_success: dict[str, int]
    upstream_connect_duration_fail: dict[str, int]

    d2c_batches_total: int
    d2c_batch_frames_total: int
    d2c_batch_bytes_total: int

    me_writers_active: int
    me_writers_warm: int
    me_writers_target: int
    me_reconnect_attempts_total: int
    me_reconnect_success_total: int
    me_handshake_reject_total: int
    me_endpoint_quarantine_total: int
    me_endpoint_quarantine_unexpected_total: int
    me_shadow_rotate_total: int
    me_writer_pick_mode: str | None
    me_floor_mode: str | None
    me_fair_active_flows: int
    me_fair_queued_bytes: int
    me_fair_pressure_state: int

    desync_total: int
    secure_padding_invalid_total: int
    crc_mismatch_total: int
    seq_mismatch_total: int
    route_drop_no_conn_total: int
    route_drop_queue_full_total: int

    tls_profiles: list[TelemtTlsProfile]
    ip_tracker: TelemtIpTracker

    users: list[TelemtUser]
    fetched_at_unix: float
