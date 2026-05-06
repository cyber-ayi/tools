from pathlib import Path

from rclone_migrate import config as config_mod


def test_load_minimal(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text(
        "[defaults]\n"
        "hash = 'SHA256'\n"
        "transfers = 16\n"
        "[delete]\n"
        "require_check_within = '2h'\n"
        "[[jobs]]\n"
        "name = 'a'\n"
        "src = '/tmp/s'\n"
        "dst = '/tmp/d'\n"
    )
    cfg = config_mod.load(p)
    assert cfg.defaults.hash == "SHA256"
    assert cfg.defaults.transfers == 16
    assert cfg.delete.require_check_within_s == 7200.0
    assert len(cfg.jobs) == 1
    j = cfg.jobs[0]
    assert j.name == "a" and j.src == "/tmp/s" and j.dst == "/tmp/d"
    assert j.resolved_transfers(cfg.defaults) == 16


def test_job_overrides(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text(
        "[defaults]\n"
        "transfers = 8\n"
        "[[jobs]]\n"
        "name = 'a'\n"
        "src = '/tmp/s'\n"
        "dst = '/tmp/d'\n"
        "transfers = 4\n"
        "hash = 'MD5'\n"
    )
    cfg = config_mod.load(p)
    j = cfg.jobs[0]
    assert j.resolved_transfers(cfg.defaults) == 4
    assert j.resolved_hash(cfg.defaults) == "MD5"


def test_duration_parsing(tmp_path: Path):
    for s, secs in [("30s", 30), ("5m", 300), ("2h", 7200), ("1d", 86400),
                    ("500ms", 0.5)]:
        p = tmp_path / f"c-{s}.toml"
        p.write_text(
            f"[delete]\nrequire_check_within = '{s}'\n"
            "[[jobs]]\nname='x'\nsrc='/a'\ndst='/b'\n"
        )
        cfg = config_mod.load(p)
        assert cfg.delete.require_check_within_s == secs
