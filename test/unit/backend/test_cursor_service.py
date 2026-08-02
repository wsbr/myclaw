from datetime import datetime

import pytest
from fastapi import HTTPException

from app.services.cursor_service import decode_cursor, encode_cursor


pytestmark = pytest.mark.unit


def test_cursor_is_signed_and_bound_to_principal_resource_and_filters():
    filters = {"status": "active", "owner": None}
    token = encode_cursor(
        principal="account:a1", resource="agents", filters=filters,
        values=[datetime(2026, 8, 2, 1, 2, 3), "agent-1"],
    )
    assert decode_cursor(
        token, principal="account:a1", resource="agents", filters=filters,
    ) == ["2026-08-02T01:02:03", "agent-1"]

    for principal, resource, changed_filters in (
        ("account:a2", "agents", filters),
        ("account:a1", "skills", filters),
        ("account:a1", "agents", {"status": "disabled", "owner": None}),
    ):
        with pytest.raises(HTTPException) as captured:
            decode_cursor(
                token, principal=principal, resource=resource, filters=changed_filters,
            )
        assert captured.value.status_code == 422

    payload, signature = token.split(".", 1)
    tampered = ("A" if payload[0] != "A" else "B") + payload[1:] + "." + signature
    with pytest.raises(HTTPException):
        decode_cursor(
            tampered, principal="account:a1", resource="agents", filters=filters,
        )

    noncanonical_signature = (
        payload + "." + signature[:-1] + ("A" if signature[-1] != "A" else "B")
    )
    with pytest.raises(HTTPException):
        decode_cursor(
            noncanonical_signature,
            principal="account:a1",
            resource="agents",
            filters=filters,
        )
