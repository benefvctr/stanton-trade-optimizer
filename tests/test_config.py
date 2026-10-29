"""config.toml handling: defaults, saves that keep comments, the .env token."""
import shutil

from sto.config import ROOT, Config, ShipProfile, read_token


def tmp_config(tmp_path) -> Config:
    path = tmp_path / "config.toml"
    shutil.copy(ROOT / "config.default.toml", path)   # not the live config
    return Config(path)


def test_default_ship_is_the_centaurus(tmp_path):
    ship = tmp_config(tmp_path).active_ship()
    assert (ship.name, ship.cargo_scu, ship.max_container_scu, ship.pad) == ("Constellation Mk V Centaurus", 240, 32, "L")


def test_defaults_match_the_spec(tmp_path):
    cfg = tmp_config(tmp_path)
    assert cfg.get("wallet", "reserve") == 150_000
    assert cfg.get("data", "price_refresh_s") == 300
    assert cfg.get("data", "systems") == ["Stanton"]
    assert cfg.get("optimizer", "overhead_min") == 8
    assert cfg.get("ocr", "delay_s") == 2 and cfg.get("ocr", "cooldown_min") == 3
    assert cfg.get("ocr", "big_change_pct") == 60


def test_all_zero_region_means_not_calibrated(tmp_path):
    cfg = tmp_config(tmp_path)
    cfg.set_ocr_region(1, (0, 0, 0, 0))
    assert cfg.ocr_region() is None


def test_saves_keep_comments(tmp_path):
    cfg = tmp_config(tmp_path)
    cfg.add_ship(ShipProfile("MISC Hull C", 4608, 32, "XL", uex_id=99))
    cfg.set_active_ship("MISC Hull C")
    cfg.set_ocr_region(2, (560, 752, 90, 22))
    text = cfg.path.read_text(encoding="utf-8")
    assert "# aUEC the optimizer never spends on cargo" in text
    assert "# Ship profiles. Add more with" in text
    again = Config(cfg.path)
    assert again.active_ship().cargo_scu == 4608
    assert again.ocr_region() == (2, (560, 752, 90, 22))
    assert set(again.ships()) == {"Constellation Mk V Centaurus", "MISC Hull C"}


def test_token_is_read_from_env_file(tmp_path):
    env = tmp_path / ".env"
    assert read_token(env) == ""
    env.write_text("# comment\nUEX_TOKEN=\n", encoding="utf-8")
    assert read_token(env) == ""
    env.write_text('UEX_TOKEN="abc123"\n', encoding="utf-8")
    assert read_token(env) == "abc123"


def test_profile_from_a_uex_vehicle(snapshot_raw):
    from sto.config import vehicle_max_box
    rows = {v["name_full"]: v for v in snapshot_raw["vehicles"]}
    hull = ShipProfile.from_vehicle(rows["MISC Hull C"])
    assert (hull.cargo_scu, hull.max_container_scu, hull.pad, hull.uex_id) == (4608, 32, "L", rows["MISC Hull C"]["id"])
    assert vehicle_max_box(rows["Drake Caterpillar"]) == 24
    assert vehicle_max_box(rows["Drake Golem Ox"]) is None
    import pytest
    with pytest.raises(ValueError):
        ShipProfile.from_vehicle(rows["Drake Golem Ox"])
    ox = ShipProfile.from_vehicle(rows["Drake Golem Ox"], max_box=32, pad="m")
    assert (ox.max_container_scu, ox.pad) == (32, "M")


def test_remove_ship_refuses_the_active_one(tmp_path):
    import pytest
    cfg = tmp_config(tmp_path)
    cfg.add_ship(ShipProfile("MISC Hull C", 4608, 32, "L"))
    with pytest.raises(ValueError):
        cfg.remove_ship(cfg.active_ship().name)
    cfg.remove_ship("MISC Hull C")
    assert "MISC Hull C" not in Config(cfg.path).ships()
    assert "# Ship profiles. Add more with" in cfg.path.read_text(encoding="utf-8")


def test_first_run_creates_config_from_the_shipped_defaults(tmp_path):
    from sto.config import DEFAULT_CONFIG_PATH
    path = tmp_path / "config.toml"
    cfg = Config(path)
    assert path.read_text(encoding="utf-8") == DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    cfg.set("wallet", "reserve", 99)
    cfg.save()
    assert Config(path).get("wallet", "reserve") == 99          # an existing config is never replaced
