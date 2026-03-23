"""Tests for per-org file isolation (CRKY-10)."""

import os
from unittest.mock import MagicMock

import pytest

from web.api.auth import UserContext
from web.api.org_isolation import resolve_clips_dir, set_base_clips_dir, validate_clip_access


@pytest.fixture
def setup_isolation(tmp_path):
    """Initialize storage and base clips dir for isolation tests."""
    import web.api.orgs as orgs_mod
    from web.api import database as db_mod
    from web.api import persist

    persist.init(str(tmp_path))
    db_mod._backend = None
    set_base_clips_dir(str(tmp_path))
    orgs_mod._org_store = None

    yield tmp_path

    db_mod._backend = None
    orgs_mod._org_store = None


def _make_request(user: UserContext | None = None) -> MagicMock:
    request = MagicMock()
    request.state.user = user
    request.headers = {}  # Real dict so .get() works correctly
    return request


class TestResolveClipsDir:
    def test_auth_disabled_returns_base(self, setup_isolation, monkeypatch):
        import web.api.org_isolation as mod

        monkeypatch.setattr(mod, "AUTH_ENABLED", False)
        request = _make_request()
        result = resolve_clips_dir(request)
        assert result == str(setup_isolation)

    def test_no_user_returns_base(self, setup_isolation, monkeypatch):
        import web.api.org_isolation as mod

        monkeypatch.setattr(mod, "AUTH_ENABLED", True)
        request = _make_request(user=None)
        result = resolve_clips_dir(request)
        assert result == str(setup_isolation)

    def test_user_gets_org_scoped_dir(self, setup_isolation, monkeypatch):
        import web.api.org_isolation as mod

        monkeypatch.setattr(mod, "AUTH_ENABLED", True)

        from web.api.orgs import get_org_store

        store = get_org_store()
        org = store.create_org("Studio", "user-1")

        user = UserContext(user_id="user-1", email="a@b.com", tier="member")
        request = _make_request(user=user)
        result = resolve_clips_dir(request)
        assert result == os.path.join(str(setup_isolation), org.org_id)
        assert os.path.isdir(result)

    def test_user_with_no_orgs_gets_personal(self, setup_isolation, monkeypatch):
        import web.api.org_isolation as mod

        monkeypatch.setattr(mod, "AUTH_ENABLED", True)

        user = UserContext(user_id="new-user", email="new@test.com", tier="member")
        request = _make_request(user=user)
        result = resolve_clips_dir(request)
        # Should have created a personal org and returned its dir
        assert str(setup_isolation) in result
        assert result != str(setup_isolation)

    def test_explicit_org_id_validated(self, setup_isolation, monkeypatch):
        import web.api.org_isolation as mod

        monkeypatch.setattr(mod, "AUTH_ENABLED", True)

        from web.api.orgs import get_org_store

        store = get_org_store()
        org = store.create_org("Other Studio", "user-2")

        # user-1 is NOT a member of this org
        user = UserContext(user_id="user-1", email="a@b.com", tier="member")
        request = _make_request(user=user)
        with pytest.raises(Exception) as exc_info:
            resolve_clips_dir(request, org_id=org.org_id)
        assert exc_info.value.status_code == 403

    def test_admin_can_access_any_org(self, setup_isolation, monkeypatch):
        import web.api.org_isolation as mod

        monkeypatch.setattr(mod, "AUTH_ENABLED", True)

        from web.api.orgs import get_org_store

        store = get_org_store()
        org = store.create_org("Other Studio", "user-2")

        user = UserContext(user_id="admin-1", email="admin@b.com", tier="platform_admin")
        request = _make_request(user=user)
        result = resolve_clips_dir(request, org_id=org.org_id)
        assert org.org_id in result


class TestValidateClipAccess:
    def test_auth_disabled_allows_all(self, setup_isolation, monkeypatch):
        import web.api.org_isolation as mod

        monkeypatch.setattr(mod, "AUTH_ENABLED", False)
        request = _make_request()
        assert validate_clip_access(request, "/any/path")

    def test_no_user_allows(self, setup_isolation, monkeypatch):
        import web.api.org_isolation as mod

        monkeypatch.setattr(mod, "AUTH_ENABLED", True)
        request = _make_request(user=None)
        assert validate_clip_access(request, "/any/path")

    def test_admin_allows_all(self, setup_isolation, monkeypatch):
        import web.api.org_isolation as mod

        monkeypatch.setattr(mod, "AUTH_ENABLED", True)
        user = UserContext(user_id="admin", email="a@b.com", tier="platform_admin")
        request = _make_request(user=user)
        assert validate_clip_access(request, "/any/path")

    def test_user_can_access_own_org(self, setup_isolation, monkeypatch):
        import web.api.org_isolation as mod

        monkeypatch.setattr(mod, "AUTH_ENABLED", True)

        from web.api.orgs import get_org_store

        store = get_org_store()
        org = store.create_org("My Studio", "user-1")

        clip_path = os.path.join(str(setup_isolation), org.org_id, "clips", "my_clip")
        os.makedirs(clip_path, exist_ok=True)

        user = UserContext(user_id="user-1", email="a@b.com", tier="member")
        request = _make_request(user=user)
        assert validate_clip_access(request, clip_path)

    def test_user_cannot_access_other_org(self, setup_isolation, monkeypatch):
        import web.api.org_isolation as mod

        monkeypatch.setattr(mod, "AUTH_ENABLED", True)

        from web.api.orgs import get_org_store

        store = get_org_store()
        org = store.create_org("Other Studio", "user-2")

        clip_path = os.path.join(str(setup_isolation), org.org_id, "clips", "secret_clip")

        user = UserContext(user_id="user-1", email="a@b.com", tier="member")
        request = _make_request(user=user)
        with pytest.raises(Exception) as exc_info:
            validate_clip_access(request, clip_path)
        assert exc_info.value.status_code == 403
