from yggdrasil import config, gitops


def test_merge_mode_comes_from_explained_config(monkeypatch):
    monkeypatch.setattr(config, "explain_all", lambda project=None, package=None: {"merge_mode": ("local", "pid:p1")})
    assert gitops.merge_mode("p1") == "local"
    monkeypatch.setattr(config, "explain_all", lambda project=None, package=None: {})
    assert gitops.merge_mode("p1") == "prepare"
    monkeypatch.setattr(config, "explain_all", lambda project=None, package=None: {"merge_mode": ("bogus", "core")})
    assert gitops.merge_mode("p1") == "prepare"
