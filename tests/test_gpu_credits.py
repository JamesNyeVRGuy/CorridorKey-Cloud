"""Tests for GPU credit tracking (CRKY-6, CRKY-185)."""

import pytest

from web.api.gpu_credits import (
    OrgCredits,
    add_consumed,
    add_contributed,
    get_org_credits,
    grant_monthly_credits,
    run_monthly_grant_cycle,
)


@pytest.fixture
def credits_store(tmp_path):
    """Initialize storage for credit tests."""
    from web.api import database as db_mod
    from web.api import persist

    persist.init(str(tmp_path))
    db_mod._backend = None
    yield
    db_mod._backend = None


class TestOrgCredits:
    def test_balance_surplus(self):
        c = OrgCredits(org_id="o1", contributed_seconds=100, consumed_seconds=60)
        assert c.balance == 40

    def test_balance_deficit(self):
        c = OrgCredits(org_id="o1", contributed_seconds=50, consumed_seconds=80)
        assert c.balance == -30

    def test_ratio_normal(self):
        c = OrgCredits(org_id="o1", contributed_seconds=100, consumed_seconds=50)
        assert c.ratio == 0.5

    def test_ratio_zero_contributed(self):
        c = OrgCredits(org_id="o1", contributed_seconds=0, consumed_seconds=50)
        assert c.ratio == float("inf")

    def test_ratio_zero_both(self):
        c = OrgCredits(org_id="o1", contributed_seconds=0, consumed_seconds=0)
        assert c.ratio == 0.0

    def test_to_dict(self):
        c = OrgCredits(org_id="o1", contributed_seconds=3600, consumed_seconds=1800)
        d = c.to_dict()
        assert d["contributed_hours"] == 1.0
        assert d["consumed_hours"] == 0.5
        assert d["balance_seconds"] == 1800.0


class TestCreditTracking:
    def test_add_contributed(self, credits_store):
        add_contributed("org-1", 100)
        credits = get_org_credits("org-1")
        assert credits.contributed_seconds == 100

    def test_add_consumed(self, credits_store):
        add_consumed("org-1", 50)
        credits = get_org_credits("org-1")
        assert credits.consumed_seconds == 50

    def test_accumulates(self, credits_store):
        add_contributed("org-1", 100)
        add_contributed("org-1", 200)
        credits = get_org_credits("org-1")
        assert credits.contributed_seconds == 300

    def test_separate_orgs(self, credits_store):
        add_contributed("org-1", 100)
        add_contributed("org-2", 200)
        assert get_org_credits("org-1").contributed_seconds == 100
        assert get_org_credits("org-2").contributed_seconds == 200

    def test_empty_org(self, credits_store):
        credits = get_org_credits("nonexistent")
        assert credits.contributed_seconds == 0
        assert credits.consumed_seconds == 0

    def test_ignores_zero(self, credits_store):
        add_contributed("org-1", 0)
        add_consumed("org-1", 0)
        credits = get_org_credits("org-1")
        assert credits.contributed_seconds == 0

    def test_ignores_negative(self, credits_store):
        add_contributed("org-1", -50)
        credits = get_org_credits("org-1")
        assert credits.contributed_seconds == 0

    def test_ignores_empty_org_id(self, credits_store):
        add_contributed("", 100)
        add_consumed("", 100)
        # Should not create an entry for empty org_id
        credits = get_org_credits("")
        assert credits.contributed_seconds == 0


class TestMonthlyGrant:
    """CRKY-185 — recurring monthly credit grants."""

    def test_first_grant_wins(self, credits_store):
        assert grant_monthly_credits("org-a", "2026-04", 3600) is True
        assert get_org_credits("org-a").contributed_seconds == 3600

    def test_second_grant_same_period_is_noop(self, credits_store):
        assert grant_monthly_credits("org-a", "2026-04", 3600) is True
        # Second call for the same period must NOT bump the balance.
        assert grant_monthly_credits("org-a", "2026-04", 3600) is False
        assert get_org_credits("org-a").contributed_seconds == 3600

    def test_different_periods_both_apply(self, credits_store):
        assert grant_monthly_credits("org-a", "2026-04", 3600) is True
        assert grant_monthly_credits("org-a", "2026-05", 3600) is True
        assert get_org_credits("org-a").contributed_seconds == 7200

    def test_zero_amount_is_noop(self, credits_store):
        assert grant_monthly_credits("org-a", "2026-04", 0) is False
        assert get_org_credits("org-a").contributed_seconds == 0

    def test_empty_org_id_is_noop(self, credits_store):
        assert grant_monthly_credits("", "2026-04", 3600) is False

    def test_cycle_disabled_when_amount_zero(self, credits_store):
        result = run_monthly_grant_cycle(seconds=0, period="2026-04")
        assert result["disabled"] is True
        assert result["granted"] == 0

    def test_cycle_skips_pending_personal_orgs(self, credits_store, monkeypatch):
        """Personal orgs owned by pending users must not receive monthly grants."""
        from web.api.gpu_credits import run_monthly_grant_cycle

        class FakeOrg:
            def __init__(self, org_id, owner_id, personal):
                self.org_id = org_id
                self.owner_id = owner_id
                self.personal = personal

        class FakeUser:
            def __init__(self, tier):
                self.tier = tier

        orgs = [
            FakeOrg("org-approved", "user-1", personal=True),
            FakeOrg("org-pending", "user-2", personal=True),
            FakeOrg("org-team", "user-2", personal=False),
        ]
        tiers = {"user-1": "member", "user-2": "pending"}

        class FakeOrgStore:
            def list_orgs(self):
                return orgs

        class FakeUserStore:
            def get_user(self, uid):
                t = tiers.get(uid)
                return FakeUser(t) if t else None

        monkeypatch.setattr("web.api.gpu_credits.get_org_store", lambda: FakeOrgStore(), raising=False)
        monkeypatch.setattr("web.api.orgs.get_org_store", lambda: FakeOrgStore())
        monkeypatch.setattr("web.api.users.get_user_store", lambda: FakeUserStore())

        result = run_monthly_grant_cycle(seconds=3600, period="2026-04")
        assert result["granted"] == 2  # approved personal + team org
        assert result["skipped"] == 1  # pending personal
        assert get_org_credits("org-approved").contributed_seconds == 3600
        assert get_org_credits("org-pending").contributed_seconds == 0
        assert get_org_credits("org-team").contributed_seconds == 3600

    def test_cycle_is_idempotent_across_reruns(self, credits_store, monkeypatch):
        from web.api.gpu_credits import run_monthly_grant_cycle

        class FakeOrg:
            def __init__(self, org_id):
                self.org_id = org_id
                self.owner_id = "user"
                self.personal = False

        class FakeOrgStore:
            def list_orgs(self):
                return [FakeOrg("org-x"), FakeOrg("org-y")]

        class FakeUserStore:
            def get_user(self, uid):
                return None

        monkeypatch.setattr("web.api.orgs.get_org_store", lambda: FakeOrgStore())
        monkeypatch.setattr("web.api.users.get_user_store", lambda: FakeUserStore())

        first = run_monthly_grant_cycle(seconds=1800, period="2026-04")
        assert first["granted"] == 2
        second = run_monthly_grant_cycle(seconds=1800, period="2026-04")
        assert second["granted"] == 0
        assert second["skipped"] == 2
        # Balances didn't double.
        assert get_org_credits("org-x").contributed_seconds == 1800
        assert get_org_credits("org-y").contributed_seconds == 1800
