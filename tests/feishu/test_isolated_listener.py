import pytest

from qianshu.feishu import handoff


def test_different_databases_share_listener_lease(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(handoff, "_MACHINE_LEASE", tmp_path / "private" / "listener", raising=False)
    with handoff.MentionLog.listening(tmp_path / "first" / "db", settings):
        with pytest.raises(BlockingIOError):
            with handoff.MentionLog.listening(tmp_path / "second" / "db", settings):
                pass
    assert not (tmp_path / "second" / "db").exists()
