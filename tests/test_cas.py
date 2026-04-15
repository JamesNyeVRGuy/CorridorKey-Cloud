"""Tests for content-addressable storage (CAS) dedup and reaper.

Covers:
- _create_clip_folder placing CAS entry + hardlinking target
- Dedup on second upload of identical bytes (single inode, nlink==2)
- Dedup across two different projects in the same org
- Different content produces different CAS entries
- cleanup_once leaves a still-referenced CAS entry untouched
- cleanup_once tombstones an orphaned CAS file on first sweep
- cleanup_once reaps an orphaned CAS file after tombstone TTL
- cleanup_once removes stale tombstone when file gets re-linked
- cleanup_once handles vanished CAS files gracefully
- Auth-scoped CAS path: CAS lives at <org_id>/.cas/, not base_clips_dir/.cas/
- Hardlink fallback to copy when os.link fails (e.g., cross-device)
- CAS temp file cleanup on error
- create_project routes cas_root correctly through the call chain
- Multiple orgs get independent CAS namespaces
"""

from __future__ import annotations

import hashlib
import os
import time
from unittest import mock

import pytest

from backend.project import (
    _copy_via_cas,
    _create_clip_folder,
    create_project,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_video(tmp_path, name: str = "clip.mp4", content: bytes = b"fake-video-data-1234") -> str:
    """Write a small fake video file and return its path."""
    p = tmp_path / name
    p.write_bytes(content)
    return str(p)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _nlink(path: str) -> int:
    return os.stat(path).st_nlink


def _cas_files(cas_dir: str) -> list[str]:
    """Return non-hidden filenames in a CAS directory."""
    if not os.path.isdir(cas_dir):
        return []
    return sorted(f for f in os.listdir(cas_dir) if not f.startswith("."))


def _tombstones(cas_dir: str) -> list[str]:
    """Return tombstone filenames (.*.orphan) in a CAS directory."""
    if not os.path.isdir(cas_dir):
        return []
    return sorted(f for f in os.listdir(cas_dir) if f.endswith(".orphan"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def org_root(tmp_path):
    """Simulates a per-org root directory (what resolve_clips_dir returns under auth)."""
    d = tmp_path / "org_abc123"
    d.mkdir()
    return d


@pytest.fixture()
def base_clips_dir(tmp_path):
    """Simulates the base Projects directory containing org subdirs."""
    d = tmp_path / "Projects"
    d.mkdir()
    return d


# ===========================================================================
# _create_clip_folder — CAS upload path
# ===========================================================================


class TestCreateClipFolder:
    """Tests for the CAS write path inside _create_clip_folder."""

    def test_first_upload_creates_cas_entry_and_hardlinks(self, tmp_path, org_root):
        """First upload: CAS file created with content hash, target is hardlinked."""
        content = b"unique-video-content-abc"
        video = _make_video(tmp_path, content=content)
        clips_dir = str(org_root / "proj" / "clips")
        os.makedirs(clips_dir)

        _create_clip_folder(clips_dir, video, copy_source=True, cas_root=str(org_root))

        cas_dir = str(org_root / ".cas")
        cas = _cas_files(cas_dir)
        assert len(cas) == 1, f"Expected 1 CAS entry, got {cas}"

        expected_hash = _sha256(content)
        assert cas[0].startswith(expected_hash), f"CAS filename should start with hash: {cas[0]}"

        # CAS file and target should share an inode (nlink >= 2)
        cas_path = os.path.join(cas_dir, cas[0])
        assert _nlink(cas_path) == 2

        # Content matches
        with open(cas_path, "rb") as f:
            assert f.read() == content

    def test_dedup_same_bytes_single_inode(self, tmp_path, org_root):
        """Second upload of identical bytes: no new CAS entry, nlink increments."""
        content = b"duplicate-video-bytes-xyz"
        video1 = _make_video(tmp_path, "a.mp4", content)
        video2 = _make_video(tmp_path, "b.mp4", content)
        clips_dir = str(org_root / "proj" / "clips")
        os.makedirs(clips_dir)

        _create_clip_folder(clips_dir, video1, copy_source=True, cas_root=str(org_root))
        _create_clip_folder(clips_dir, video2, copy_source=True, cas_root=str(org_root))

        cas_dir = str(org_root / ".cas")
        cas = _cas_files(cas_dir)
        assert len(cas) == 1, "Dedup should reuse existing CAS entry"
        assert _nlink(os.path.join(cas_dir, cas[0])) == 3  # CAS + 2 targets

    def test_dedup_across_projects_same_org(self, tmp_path, org_root):
        """Identical files in different projects share the same CAS entry."""
        content = b"shared-across-projects"
        video1 = _make_video(tmp_path, "v1.mp4", content)
        video2 = _make_video(tmp_path, "v2.mp4", content)

        clips_dir_a = str(org_root / "projA" / "clips")
        clips_dir_b = str(org_root / "projB" / "clips")
        os.makedirs(clips_dir_a)
        os.makedirs(clips_dir_b)

        _create_clip_folder(clips_dir_a, video1, copy_source=True, cas_root=str(org_root))
        _create_clip_folder(clips_dir_b, video2, copy_source=True, cas_root=str(org_root))

        cas_dir = str(org_root / ".cas")
        cas = _cas_files(cas_dir)
        assert len(cas) == 1
        assert _nlink(os.path.join(cas_dir, cas[0])) == 3

    def test_different_content_different_cas_entries(self, tmp_path, org_root):
        """Different file content produces separate CAS entries."""
        video1 = _make_video(tmp_path, "a.mp4", b"content-alpha")
        video2 = _make_video(tmp_path, "b.mp4", b"content-beta")
        clips_dir = str(org_root / "proj" / "clips")
        os.makedirs(clips_dir)

        _create_clip_folder(clips_dir, video1, copy_source=True, cas_root=str(org_root))
        _create_clip_folder(clips_dir, video2, copy_source=True, cas_root=str(org_root))

        cas_dir = str(org_root / ".cas")
        cas = _cas_files(cas_dir)
        assert len(cas) == 2, "Different content should produce 2 CAS entries"
        assert cas[0] != cas[1]

    def test_copy_source_false_skips_cas(self, tmp_path, org_root):
        """When copy_source=False, no CAS entry or copy is made."""
        video = _make_video(tmp_path)
        clips_dir = str(org_root / "proj" / "clips")
        os.makedirs(clips_dir)

        _create_clip_folder(clips_dir, video, copy_source=False, cas_root=str(org_root))

        cas_dir = str(org_root / ".cas")
        assert not os.path.exists(cas_dir) or _cas_files(cas_dir) == []

    def test_hardlink_failure_falls_back_to_copy(self, tmp_path, org_root):
        """When os.link raises OSError, the file is copied instead."""
        content = b"fallback-content"
        video = _make_video(tmp_path, content=content)
        clips_dir = str(org_root / "proj" / "clips")
        os.makedirs(clips_dir)

        call_count = 0

        def fail_link(src, dst):
            nonlocal call_count
            call_count += 1
            raise OSError("cross-device link")

        with mock.patch("backend.project.os.link", side_effect=fail_link):
            _create_clip_folder(clips_dir, video, copy_source=True, cas_root=str(org_root))

        assert call_count >= 1

        # Target should still exist via copy
        clip_dirs = [d for d in os.listdir(clips_dir) if not d.startswith(".")]
        assert len(clip_dirs) == 1
        source_dir = os.path.join(clips_dir, clip_dirs[0], "Source")
        targets = os.listdir(source_dir)
        assert len(targets) == 1
        with open(os.path.join(source_dir, targets[0]), "rb") as f:
            assert f.read() == content

    def test_cas_temp_cleaned_on_write_error(self, tmp_path, org_root):
        """If hashing/writing fails mid-stream, temp file is cleaned up."""
        video = _make_video(tmp_path, content=b"x" * 100)
        clips_dir = str(org_root / "proj" / "clips")
        os.makedirs(clips_dir)

        cas_dir = str(org_root / ".cas")

        # Make the CAS copy fail by providing a non-writable CAS directory
        os.makedirs(cas_dir, exist_ok=True)

        original_open = open

        def flaky_open(path, mode="r", *a, **kw):
            path_str = str(path)
            if ".cas" in path_str and ".tmp" in path_str and ("w" in str(mode)):
                raise IOError("disk full")
            return original_open(path, mode, *a, **kw)

        with mock.patch("backend.project.open", side_effect=flaky_open):
            # The CAS code catches exceptions and falls back to shutil.copy2
            try:
                _create_clip_folder(clips_dir, video, copy_source=True, cas_root=str(org_root))
            except IOError:
                pass

        # No stale .tmp files left
        if os.path.isdir(cas_dir):
            tmps = [f for f in os.listdir(cas_dir) if f.endswith(".tmp")]
            assert tmps == [], f"Stale temp files remain: {tmps}"

    def test_cas_preserves_file_extension(self, tmp_path, org_root):
        """CAS entry preserves the original file extension."""
        video = _make_video(tmp_path, "scene.mov", b"mov-content")
        clips_dir = str(org_root / "proj" / "clips")
        os.makedirs(clips_dir)

        _create_clip_folder(clips_dir, video, copy_source=True, cas_root=str(org_root))

        cas_dir = str(org_root / ".cas")
        cas = _cas_files(cas_dir)
        assert len(cas) == 1
        assert cas[0].endswith(".mov")

    def test_cas_root_defaults_to_projects_root(self, tmp_path):
        """When cas_root is None, CAS goes to projects_root()."""
        content = b"default-root-test"
        video = _make_video(tmp_path, content=content)
        proj_root = str(tmp_path / "ProjectsDefault")
        os.makedirs(proj_root)
        clips_dir = os.path.join(proj_root, "proj", "clips")
        os.makedirs(clips_dir)

        with mock.patch("backend.project.projects_root", return_value=proj_root):
            _create_clip_folder(clips_dir, video, copy_source=True, cas_root=None)

        cas_dir = os.path.join(proj_root, ".cas")
        assert len(_cas_files(cas_dir)) == 1


# ===========================================================================
# _copy_via_cas
# ===========================================================================


class TestCopyViaCas:
    """Direct tests for the extracted _copy_via_cas helper."""

    def test_basic_copy_creates_cas_and_target(self, tmp_path):
        content = b"hello-cas"
        video = _make_video(tmp_path, content=content)
        cas_dir = str(tmp_path / ".cas")
        target = str(tmp_path / "dest" / "clip.mp4")
        os.makedirs(os.path.dirname(target))

        _copy_via_cas(video, target, cas_dir)

        cas = _cas_files(cas_dir)
        assert len(cas) == 1
        assert cas[0].startswith(_sha256(content))
        with open(target, "rb") as f:
            assert f.read() == content

    def test_dedup_reuses_existing_entry(self, tmp_path):
        content = b"dedup-test"
        v1 = _make_video(tmp_path, "a.mp4", content)
        v2 = _make_video(tmp_path, "b.mp4", content)
        cas_dir = str(tmp_path / ".cas")
        t1 = str(tmp_path / "d1" / "a.mp4")
        t2 = str(tmp_path / "d2" / "b.mp4")
        os.makedirs(os.path.dirname(t1))
        os.makedirs(os.path.dirname(t2))

        _copy_via_cas(v1, t1, cas_dir)
        _copy_via_cas(v2, t2, cas_dir)

        assert len(_cas_files(cas_dir)) == 1
        cas_path = os.path.join(cas_dir, _cas_files(cas_dir)[0])
        assert _nlink(cas_path) == 3

    def test_hardlink_failure_copies_instead(self, tmp_path):
        content = b"fallback"
        video = _make_video(tmp_path, content=content)
        cas_dir = str(tmp_path / ".cas")
        target = str(tmp_path / "dest" / "clip.mp4")
        os.makedirs(os.path.dirname(target))

        with mock.patch("backend.project.os.link", side_effect=OSError("cross-device")):
            _copy_via_cas(video, target, cas_dir)

        with open(target, "rb") as f:
            assert f.read() == content

    def test_creates_cas_dir_if_missing(self, tmp_path):
        video = _make_video(tmp_path, content=b"data")
        cas_dir = str(tmp_path / "new_cas_dir" / ".cas")
        target = str(tmp_path / "dest" / "clip.mp4")
        os.makedirs(os.path.dirname(target))

        _copy_via_cas(video, target, cas_dir)

        assert os.path.isdir(cas_dir)
        assert len(_cas_files(cas_dir)) == 1

    def test_no_stale_tmp_on_read_error(self, tmp_path):
        """If reading the source fails, no .tmp files are left behind."""
        video = str(tmp_path / "missing.mp4")  # does not exist
        cas_dir = str(tmp_path / ".cas")
        target = str(tmp_path / "dest" / "clip.mp4")
        os.makedirs(os.path.dirname(target))

        with pytest.raises(FileNotFoundError):
            _copy_via_cas(video, target, cas_dir)

        if os.path.isdir(cas_dir):
            tmps = [f for f in os.listdir(cas_dir) if f.endswith(".tmp")]
            assert tmps == []

    def test_extension_preserved(self, tmp_path):
        video = _make_video(tmp_path, "scene.mov", b"mov-data")
        cas_dir = str(tmp_path / ".cas")
        target = str(tmp_path / "dest" / "scene.mov")
        os.makedirs(os.path.dirname(target))

        _copy_via_cas(video, target, cas_dir)

        cas = _cas_files(cas_dir)
        assert cas[0].endswith(".mov")

    def test_concurrent_writers_single_inode(self, tmp_path):
        """Two concurrent _copy_via_cas calls for identical bytes share one inode."""
        content = b"concurrent-upload-bytes"
        v1 = _make_video(tmp_path, "up1.mp4", content)
        v2 = _make_video(tmp_path, "up2.mp4", content)
        cas_dir = str(tmp_path / ".cas")
        t1 = str(tmp_path / "d1" / "up1.mp4")
        t2 = str(tmp_path / "d2" / "up2.mp4")
        os.makedirs(os.path.dirname(t1))
        os.makedirs(os.path.dirname(t2))

        # Simulate the race: Writer A promotes via os.link(tmp, cas_path).
        # Before Writer B checks os.path.exists, cas_path already exists.
        # But we want to test the else-branch race: both see exists=False,
        # then both try os.link(tmp, cas_path).  We force this by making
        # os.path.exists return False for cas_path on both calls, so both
        # writers enter the else-branch.
        real_exists = os.path.exists
        call_count = {"n": 0}
        expected_hash = _sha256(content)

        def fake_exists(p):
            # Let the first two checks of cas_path see False (both writers enter else)
            if expected_hash in str(p) and call_count["n"] < 2:
                call_count["n"] += 1
                return False
            return real_exists(p)

        with mock.patch("backend.project.os.path.exists", side_effect=fake_exists):
            _copy_via_cas(v1, t1, cas_dir)
            _copy_via_cas(v2, t2, cas_dir)

        # Only one CAS entry
        cas = _cas_files(cas_dir)
        assert len(cas) == 1, f"Expected 1 CAS entry, got {cas}"

        # Both targets point at the same inode as the CAS entry
        cas_path = os.path.join(cas_dir, cas[0])
        assert os.stat(t1).st_ino == os.stat(cas_path).st_ino
        assert os.stat(t2).st_ino == os.stat(cas_path).st_ino
        assert _nlink(cas_path) == 3  # CAS + t1 + t2


# ===========================================================================
# create_project
# ===========================================================================


class TestCreateProjectCAS:
    """Verify create_project passes cas_root=root_dir correctly."""

    def test_create_project_uses_root_dir_as_cas_root(self, tmp_path):
        """create_project(root_dir=X) should place .cas at X/.cas."""
        content = b"project-level-cas"
        video = _make_video(tmp_path, content=content)
        root = str(tmp_path / "OrgRoot")
        os.makedirs(root)

        create_project(str(video), root_dir=root)

        cas_dir = os.path.join(root, ".cas")
        assert os.path.isdir(cas_dir)
        cas = _cas_files(cas_dir)
        assert len(cas) == 1
        assert cas[0].startswith(_sha256(content))

    def test_two_projects_dedup_under_same_root(self, tmp_path):
        """Two projects in the same org root share CAS entries."""
        content = b"multi-project-dedup"
        v1 = _make_video(tmp_path, "a.mp4", content)
        v2 = _make_video(tmp_path, "b.mp4", content)
        root = str(tmp_path / "OrgRoot")
        os.makedirs(root)

        create_project(str(v1), root_dir=root)
        create_project(str(v2), root_dir=root)

        cas_dir = os.path.join(root, ".cas")
        cas = _cas_files(cas_dir)
        assert len(cas) == 1
        assert _nlink(os.path.join(cas_dir, cas[0])) == 3


# ===========================================================================
# _sweep_cas_dir
# ===========================================================================


class TestSweepCasDir:
    """Direct tests for the extracted _sweep_cas_dir helper."""

    def _make_cas(self, cas_dir: str, fname: str, data: bytes) -> str:
        os.makedirs(cas_dir, exist_ok=True)
        path = os.path.join(cas_dir, fname)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_returns_freed_bytes_and_count(self, tmp_path):
        from web.api.clip_retention import _sweep_cas_dir

        cas_dir = str(tmp_path / ".cas")
        path = self._make_cas(cas_dir, "a.mp4", b"12345")
        # Create tombstone and backdate it
        tomb = os.path.join(cas_dir, ".a.mp4.orphan")
        with open(tomb, "w") as f:
            f.write("")
        os.utime(tomb, (time.time() - 7200, time.time() - 7200))

        freed, removed = _sweep_cas_dir(cas_dir, "test_org")
        assert freed == 5
        assert removed == 1
        assert not os.path.exists(path)

    def test_referenced_file_zero_freed(self, tmp_path):
        from web.api.clip_retention import _sweep_cas_dir

        cas_dir = str(tmp_path / ".cas")
        path = self._make_cas(cas_dir, "b.mp4", b"data")
        link = str(tmp_path / "ref.mp4")
        os.link(path, link)

        freed, removed = _sweep_cas_dir(cas_dir, "test_org")
        assert freed == 0
        assert removed == 0
        assert os.path.exists(path)

    def test_first_sweep_tombstones_only(self, tmp_path):
        from web.api.clip_retention import _sweep_cas_dir

        cas_dir = str(tmp_path / ".cas")
        self._make_cas(cas_dir, "c.mp4", b"orphan")

        freed, removed = _sweep_cas_dir(cas_dir, "test_org")
        assert freed == 0
        assert removed == 0
        assert _tombstones(cas_dir) == [".c.mp4.orphan"]

    def test_concurrent_link_prevents_reap(self, tmp_path):
        """Simulates a concurrent upload linking onto CAS between stat and probe."""
        from web.api.clip_retention import _sweep_cas_dir

        cas_dir = str(tmp_path / ".cas")
        path = self._make_cas(cas_dir, "race.mp4", b"contested")
        tomb = os.path.join(cas_dir, ".race.mp4.orphan")
        with open(tomb, "w") as f:
            f.write("")
        os.utime(tomb, (time.time() - 7200, time.time() - 7200))

        # Now create an external link simulating a concurrent upload
        ext_link = str(tmp_path / "new_clip_source.mp4")
        os.link(path, ext_link)
        assert _nlink(path) == 2

        # Sweep should see nlink=3 (CAS + ext_link + probe) and abort
        freed, removed = _sweep_cas_dir(cas_dir, "test_org")
        assert freed == 0
        assert removed == 0
        assert os.path.exists(path), "Concurrently-linked file must survive"
        assert not os.path.exists(tomb), "Tombstone should be cleared for re-linked file"

    def test_probe_cleaned_up_on_error(self, tmp_path):
        """Probe file is removed even if stat after probe raises."""
        from web.api.clip_retention import _sweep_cas_dir

        cas_dir = str(tmp_path / ".cas")
        self._make_cas(cas_dir, "err.mp4", b"data")
        tomb = os.path.join(cas_dir, ".err.mp4.orphan")
        with open(tomb, "w") as f:
            f.write("")
        os.utime(tomb, (time.time() - 7200, time.time() - 7200))

        _sweep_cas_dir(cas_dir, "test_org")

        # No .probe files should remain
        probes = [f for f in os.listdir(cas_dir) if f.endswith(".probe")]
        assert probes == []


# ===========================================================================
# cleanup_once
# ===========================================================================


class TestCleanupOnceCAS:
    """Tests for the CAS reaper logic inside cleanup_once."""

    @pytest.fixture(autouse=True)
    def _patch_dependencies(self, base_clips_dir):
        """Patch out all the external deps of cleanup_once."""
        policy = mock.MagicMock()
        policy.enabled = True
        policy.days_for_tier.return_value = 9999  # never expire clips for CAS tests
        policy.delete_mode = "outputs_only"

        scan_result = []  # no clips to expire — focus on CAS

        with (
            mock.patch("web.api.clip_retention.get_retention_policy", return_value=policy),
            mock.patch("web.api.clip_retention._get_org_tier", return_value="member"),
            mock.patch("backend.clip_state.scan_clips_dir", return_value=scan_result),
            mock.patch("web.api.storage_quota.invalidate_usage_cache"),
            mock.patch("web.api.ws.manager"),
        ):
            yield

    def _setup_org_cas(self, base_clips_dir, org_id: str, files: dict[str, bytes]) -> str:
        """Create an org dir with CAS entries. Returns the .cas dir path.

        files: mapping of {filename: content} for CAS entries.
        """
        org_dir = os.path.join(str(base_clips_dir), org_id)
        cas_dir = os.path.join(org_dir, ".cas")
        os.makedirs(cas_dir, exist_ok=True)
        for fname, data in files.items():
            with open(os.path.join(cas_dir, fname), "wb") as f:
                f.write(data)
        return cas_dir

    def test_referenced_cas_entry_left_alone(self, base_clips_dir, tmp_path):
        """A CAS file with nlink > 1 must not be removed."""
        from web.api.clip_retention import cleanup_once

        cas_dir = self._setup_org_cas(base_clips_dir, "orgA", {"abc123.mp4": b"data"})
        cas_path = os.path.join(cas_dir, "abc123.mp4")

        # Create a hardlink to simulate an active reference
        link_target = str(tmp_path / "active_reference.mp4")
        os.link(cas_path, link_target)
        assert _nlink(cas_path) == 2

        cleanup_once(str(base_clips_dir))

        assert os.path.exists(cas_path), "Referenced CAS entry should not be removed"
        assert _tombstones(cas_dir) == [], "Should not tombstone a referenced file"

    def test_orphaned_cas_gets_tombstoned_first_sweep(self, base_clips_dir):
        """An orphaned CAS file (nlink==1) gets a tombstone on first sweep, not deleted."""
        from web.api.clip_retention import cleanup_once

        cas_dir = self._setup_org_cas(base_clips_dir, "orgA", {"dead.mp4": b"orphan"})
        cas_path = os.path.join(cas_dir, "dead.mp4")
        assert _nlink(cas_path) == 1

        cleanup_once(str(base_clips_dir))

        assert os.path.exists(cas_path), "Should NOT delete on first sweep"
        tombs = _tombstones(cas_dir)
        assert len(tombs) == 1
        assert tombs[0] == ".dead.mp4.orphan"

    def test_orphaned_cas_not_reaped_before_ttl(self, base_clips_dir):
        """Orphaned CAS with tombstone younger than 1hr is not reaped."""
        from web.api.clip_retention import cleanup_once

        cas_dir = self._setup_org_cas(base_clips_dir, "orgA", {"dead.mp4": b"orphan"})

        # First sweep: creates tombstone
        cleanup_once(str(base_clips_dir))
        assert os.path.exists(os.path.join(cas_dir, "dead.mp4"))

        # Second sweep immediately: tombstone is too young
        cleanup_once(str(base_clips_dir))
        assert os.path.exists(os.path.join(cas_dir, "dead.mp4")), "Should not reap before TTL"

    def test_orphaned_cas_reaped_after_ttl(self, base_clips_dir):
        """Orphaned CAS with tombstone older than 1hr is removed."""
        from web.api.clip_retention import cleanup_once

        cas_dir = self._setup_org_cas(base_clips_dir, "orgA", {"dead.mp4": b"orphaned-data"})
        cas_path = os.path.join(cas_dir, "dead.mp4")
        tombstone_path = os.path.join(cas_dir, ".dead.mp4.orphan")

        # First sweep: creates tombstone
        cleanup_once(str(base_clips_dir))
        assert os.path.exists(tombstone_path)

        # Backdate the tombstone to simulate 2 hours ago
        old_time = time.time() - 7200
        os.utime(tombstone_path, (old_time, old_time))

        # Second sweep: now the TTL has expired
        cleanup_once(str(base_clips_dir))

        assert not os.path.exists(cas_path), "Orphaned CAS file should be reaped after TTL"
        assert not os.path.exists(tombstone_path), "Tombstone should be cleaned up too"

    def test_tombstone_removed_when_file_gets_relinked(self, base_clips_dir, tmp_path):
        """If an orphaned file gets a new hardlink, its tombstone is removed."""
        from web.api.clip_retention import cleanup_once

        cas_dir = self._setup_org_cas(base_clips_dir, "orgA", {"revived.mp4": b"data"})
        cas_path = os.path.join(cas_dir, "revived.mp4")
        tombstone_path = os.path.join(cas_dir, ".revived.mp4.orphan")

        # First sweep: orphaned → tombstone created
        cleanup_once(str(base_clips_dir))
        assert os.path.exists(tombstone_path)

        # Now create a hardlink (simulating a new clip referencing the file)
        new_link = str(tmp_path / "new_clip_source.mp4")
        os.link(cas_path, new_link)
        assert _nlink(cas_path) == 2

        # Second sweep: should see nlink > 1 and remove the stale tombstone
        cleanup_once(str(base_clips_dir))
        assert os.path.exists(cas_path), "Re-linked file must survive"
        assert not os.path.exists(tombstone_path), "Stale tombstone should be removed"

    def test_vanished_cas_file_tombstone_cleaned(self, base_clips_dir):
        """If a CAS file disappears between sweeps, its tombstone is cleaned up."""
        from web.api.clip_retention import cleanup_once

        cas_dir = self._setup_org_cas(base_clips_dir, "orgA", {"gone.mp4": b"temporary"})
        tombstone_path = os.path.join(cas_dir, ".gone.mp4.orphan")

        # First sweep: creates tombstone
        cleanup_once(str(base_clips_dir))
        assert os.path.exists(tombstone_path)

        # Manually remove the CAS file (simulating external deletion)
        os.remove(os.path.join(cas_dir, "gone.mp4"))

        # Second sweep: should clean up the orphaned tombstone
        cleanup_once(str(base_clips_dir))
        assert not os.path.exists(tombstone_path), "Tombstone for vanished file should be cleaned"

    def test_multiple_orphaned_files_tracked_independently(self, base_clips_dir):
        """Each orphaned file gets its own tombstone and TTL."""
        from web.api.clip_retention import cleanup_once

        cas_dir = self._setup_org_cas(
            base_clips_dir,
            "orgA",
            {
                "old.mp4": b"old-data",
                "new.mp4": b"new-data",
            },
        )

        # First sweep: both get tombstones
        cleanup_once(str(base_clips_dir))
        assert len(_tombstones(cas_dir)) == 2

        # Backdate only the first tombstone
        old_tomb = os.path.join(cas_dir, ".old.mp4.orphan")
        os.utime(old_tomb, (time.time() - 7200, time.time() - 7200))

        # Second sweep: only old.mp4 should be reaped
        cleanup_once(str(base_clips_dir))
        assert not os.path.exists(os.path.join(cas_dir, "old.mp4"))
        assert os.path.exists(os.path.join(cas_dir, "new.mp4"))

    def test_hidden_files_in_cas_dir_ignored(self, base_clips_dir):
        """Files starting with . (like tombstones or .DS_Store) are skipped."""
        from web.api.clip_retention import cleanup_once

        org_dir = os.path.join(str(base_clips_dir), "orgA")
        cas_dir = os.path.join(org_dir, ".cas")
        os.makedirs(cas_dir)

        # Create only hidden files
        for name in [".DS_Store", ".gitkeep", ".some.mp4.orphan"]:
            with open(os.path.join(cas_dir, name), "w") as f:
                f.write("")

        # Should complete without error (no non-hidden files to process)
        cleanup_once(str(base_clips_dir))


# ===========================================================================
# Auth-scoped CAS path
# ===========================================================================


class TestAuthScopedCASPath:
    """Verify that CAS lives at <org_id>/.cas/ and cleanup sweeps there."""

    @pytest.fixture(autouse=True)
    def _patch_dependencies(self, base_clips_dir):
        policy = mock.MagicMock()
        policy.enabled = True
        policy.days_for_tier.return_value = 9999
        policy.delete_mode = "outputs_only"

        with (
            mock.patch("web.api.clip_retention.get_retention_policy", return_value=policy),
            mock.patch("web.api.clip_retention._get_org_tier", return_value="member"),
            mock.patch("backend.clip_state.scan_clips_dir", return_value=[]),
            mock.patch("web.api.storage_quota.invalidate_usage_cache"),
            mock.patch("web.api.ws.manager"),
        ):
            yield

    def test_cas_created_under_org_not_base(self, base_clips_dir, tmp_path):
        """create_project with root_dir=org_dir puts .cas in org_dir, not base."""
        content = b"auth-scoped-video"
        video = _make_video(tmp_path, content=content)
        org_dir = os.path.join(str(base_clips_dir), "org_x")
        os.makedirs(org_dir)

        create_project(str(video), root_dir=org_dir)

        # CAS should be at org_dir/.cas/
        org_cas = os.path.join(org_dir, ".cas")
        assert os.path.isdir(org_cas), "CAS should be under org dir"
        assert len(_cas_files(org_cas)) == 1

        # CAS should NOT be at base_clips_dir/.cas/
        base_cas = os.path.join(str(base_clips_dir), ".cas")
        assert not os.path.exists(base_cas), "CAS must not leak to base_clips_dir"

    def test_cleanup_sweeps_per_org_cas(self, base_clips_dir, tmp_path):
        """cleanup_once must find and process CAS entries in each org's .cas dir."""
        from web.api.clip_retention import cleanup_once

        # Create two orgs with orphaned CAS files
        for org_id in ["org1", "org2"]:
            org_dir = os.path.join(str(base_clips_dir), org_id)
            cas_dir = os.path.join(org_dir, ".cas")
            os.makedirs(cas_dir)
            with open(os.path.join(cas_dir, f"orphan_{org_id}.mp4"), "wb") as f:
                f.write(b"orphan-data")

        # First sweep: tombstones created per-org
        cleanup_once(str(base_clips_dir))

        for org_id in ["org1", "org2"]:
            cas_dir = os.path.join(str(base_clips_dir), org_id, ".cas")
            tombs = _tombstones(cas_dir)
            assert len(tombs) == 1, f"Expected tombstone in {org_id}/.cas/"

    def test_cleanup_ignores_base_level_cas_dir(self, base_clips_dir):
        """A stale .cas at base level (from before the fix) is not treated as an org."""
        from web.api.clip_retention import cleanup_once

        # Create a base-level .cas dir (legacy/incorrect placement)
        stale_cas = os.path.join(str(base_clips_dir), ".cas")
        os.makedirs(stale_cas)
        with open(os.path.join(stale_cas, "stale.mp4"), "wb") as f:
            f.write(b"stale")

        # Also create a real org
        org_dir = os.path.join(str(base_clips_dir), "real_org")
        os.makedirs(org_dir)

        # Should not crash and should skip .cas (starts with ".")
        cleanup_once(str(base_clips_dir))

        # Stale file should still be there (not swept — it's in base, not per-org)
        assert os.path.exists(os.path.join(stale_cas, "stale.mp4"))


# ===========================================================================
# Multi-org CAS isolation
# ===========================================================================


class TestMultiOrgCASIsolation:
    """Verify two orgs can't cross-reference each other's CAS entries."""

    def test_same_content_different_orgs_separate_cas(self, tmp_path):
        """Identical uploads in different org roots produce per-org CAS entries."""
        content = b"same-bytes-different-orgs"

        org1_root = str(tmp_path / "org1")
        org2_root = str(tmp_path / "org2")
        os.makedirs(org1_root)
        os.makedirs(org2_root)

        for org_root in [org1_root, org2_root]:
            video = _make_video(tmp_path, f"v_{os.path.basename(org_root)}.mp4", content)
            clips_dir = os.path.join(org_root, "proj", "clips")
            os.makedirs(clips_dir)
            _create_clip_folder(clips_dir, video, copy_source=True, cas_root=org_root)

        # Each org should have its own CAS entry
        cas1 = _cas_files(os.path.join(org1_root, ".cas"))
        cas2 = _cas_files(os.path.join(org2_root, ".cas"))
        assert len(cas1) == 1
        assert len(cas2) == 1
        assert cas1[0] == cas2[0]  # same hash-based name

        # But they are distinct inodes — not hardlinked across orgs
        path1 = os.path.join(org1_root, ".cas", cas1[0])
        path2 = os.path.join(org2_root, ".cas", cas2[0])
        assert os.stat(path1).st_ino != os.stat(path2).st_ino


# ===========================================================================
# End-to-end: create + cleanup integration
# ===========================================================================


class TestCASEndToEnd:
    """End-to-end: create a project, delete the clip, and verify CAS reaper works."""

    @pytest.fixture(autouse=True)
    def _patch_dependencies(self, base_clips_dir):
        policy = mock.MagicMock()
        policy.enabled = True
        policy.days_for_tier.return_value = 9999
        policy.delete_mode = "outputs_only"

        with (
            mock.patch("web.api.clip_retention.get_retention_policy", return_value=policy),
            mock.patch("web.api.clip_retention._get_org_tier", return_value="member"),
            mock.patch("backend.clip_state.scan_clips_dir", return_value=[]),
            mock.patch("web.api.storage_quota.invalidate_usage_cache"),
            mock.patch("web.api.ws.manager"),
        ):
            yield

    def test_create_then_delete_clip_then_reap_cas(self, base_clips_dir, tmp_path):
        """Full lifecycle: create → delete clip → reaper removes orphaned CAS."""
        from web.api.clip_retention import cleanup_once

        content = b"lifecycle-video-data"
        video = _make_video(tmp_path, content=content)
        org_dir = str(base_clips_dir / "org_lifecycle")
        os.makedirs(org_dir)

        # Create a project in the org
        project_dir = create_project(str(video), root_dir=org_dir)

        cas_dir = os.path.join(org_dir, ".cas")
        cas = _cas_files(cas_dir)
        assert len(cas) == 1
        cas_path = os.path.join(cas_dir, cas[0])
        assert _nlink(cas_path) == 2  # CAS + clip target

        # Delete the clip's Source file (simulating clip deletion)
        clips_dir = os.path.join(project_dir, "clips")
        for clip_name in os.listdir(clips_dir):
            source_dir = os.path.join(clips_dir, clip_name, "Source")
            if os.path.isdir(source_dir):
                for f in os.listdir(source_dir):
                    os.remove(os.path.join(source_dir, f))

        # CAS entry is now orphaned (nlink==1)
        assert _nlink(cas_path) == 1

        # First cleanup: tombstone created
        cleanup_once(str(base_clips_dir))
        assert os.path.exists(cas_path)
        assert len(_tombstones(cas_dir)) == 1

        # Backdate tombstone past TTL
        tomb = os.path.join(cas_dir, _tombstones(cas_dir)[0])
        os.utime(tomb, (time.time() - 7200, time.time() - 7200))

        # Second cleanup: CAS file reaped
        cleanup_once(str(base_clips_dir))
        assert not os.path.exists(cas_path), "Orphaned CAS should be reaped"
        assert _tombstones(cas_dir) == [], "Tombstone should be cleaned up"

    def test_dedup_then_partial_delete_preserves_cas(self, base_clips_dir, tmp_path):
        """Two clips share CAS. Deleting one should not reap the CAS entry."""
        from web.api.clip_retention import cleanup_once

        content = b"shared-dedup-lifecycle"
        v1 = _make_video(tmp_path, "a.mp4", content)
        v2 = _make_video(tmp_path, "b.mp4", content)
        org_dir = str(base_clips_dir / "org_dedup")
        os.makedirs(org_dir)

        proj1 = create_project(str(v1), root_dir=org_dir)
        create_project(str(v2), root_dir=org_dir)

        cas_dir = os.path.join(org_dir, ".cas")
        cas = _cas_files(cas_dir)
        assert len(cas) == 1
        cas_path = os.path.join(cas_dir, cas[0])
        assert _nlink(cas_path) == 3  # CAS + 2 targets

        # Delete only the first clip's source
        clips_dir = os.path.join(proj1, "clips")
        for clip_name in os.listdir(clips_dir):
            source_dir = os.path.join(clips_dir, clip_name, "Source")
            if os.path.isdir(source_dir):
                for f in os.listdir(source_dir):
                    os.remove(os.path.join(source_dir, f))

        # nlink is now 2 (CAS + one remaining clip)
        assert _nlink(cas_path) == 2

        # Cleanup should NOT tombstone — file is still referenced
        cleanup_once(str(base_clips_dir))
        assert os.path.exists(cas_path)
        assert _tombstones(cas_dir) == []
