import json

import pytest

from test_moderator_role import headers, setup
from test_moderator_support import support, load_case

OWNER = headers("owner", "OWNER")


def test_owner_search_take_note_status_release_and_reads_all_audit_as_owner(support):
    client, pool = support
    from app.moderator_customers import router
    client.app.include_router(router)
    assert client.get("/api/mod/customers?search=customer", headers=OWNER).json()["items"][0]["user_id"] == "customer"
    assert client.get("/api/mod/customers/customer", headers=OWNER).status_code == 200
    path = f"/api/mod/support/cases/{load_case(client)}"
    assert client.get(path, headers=OWNER).status_code == 200
    assert client.post(path + "/take", headers=OWNER).status_code == 200
    assert client.post(path + "/notes", headers=OWNER, json={"body": "Ekip incelemesi"}).status_code == 200
    assert client.post(path + "/status", headers=OWNER, json={"status": "WAITING", "expected_version": 3}).status_code == 200
    assert client.post(path + "/release", headers=OWNER).status_code == 200
    assert all(row["actor_role"] == "OWNER" for row in pool.audits)
    assert [r["action"] for r in pool.audits] == ["customer.viewed", "support.case.viewed", "support.case.taken",
                                               "support.note.added", "support.case.status_changed", "support.case.released"]
    assert all(json.loads(r["before"]) == json.loads(r["after"]) == {} for r in pool.audits)


@pytest.mark.parametrize("target", ["owner", "moderator", "moderator2"])
def test_owner_cannot_read_or_take_staff_or_self_customer_support(support, target):
    client, pool = support
    path = f"/api/mod/support/cases/{load_case(client)}"
    pool.cases["legacy-1"]["user_id"] = target
    assert client.get(path, headers=OWNER).status_code == 404
    assert client.post(path + "/take", headers=OWNER).status_code == 404
