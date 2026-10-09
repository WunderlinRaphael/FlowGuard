import pytest
import yaml

from flowguard.common.stations import by_number, load_stations, targets


@pytest.fixture
def stations():
    return load_stations()


def write_config(tmp_path, entries):
    path = tmp_path / "stations.yaml"
    path.write_text(yaml.safe_dump({"stations": entries}), encoding="utf-8")
    return path


def entry(number, role="target", parameters=("Q", "W"), **slots):
    return {
        "number": number,
        "name": f"Station {number}",
        "river": "River",
        "role": role,
        "parameters": list(parameters),
        **slots,
    }


def test_config_has_three_targets(stations):
    assert {s.number for s in targets(stations)} == {"2087", "2491", "2056"}


def test_every_target_has_w(stations):
    for s in targets(stations):
        assert "W" in s.parameters, s.number


def test_every_input_has_q(stations):
    inputs = [s for s in stations if s.role == "input"]
    assert inputs
    for s in inputs:
        assert "Q" in s.parameters, s.number


def test_seedorf_upstream_slots(stations):
    seedorf = by_number(stations)["2056"]
    assert seedorf.up_main == "2087"
    assert seedorf.up_trib == "2491"
    assert seedorf.div_q == ("2492", "2649")


def test_observe_only_is_not_used_upstream(stations):
    observe_only = {s.number for s in stations if s.role == "observe_only"}
    assert observe_only == {"2299"}
    for s in stations:
        assert not observe_only & set(s.upstream()), s.number


def test_unknown_role_is_rejected(tmp_path):
    path = write_config(tmp_path, [entry("1", role="forecast")])
    with pytest.raises(ValueError, match="unknown role"):
        load_stations(path)


def test_unknown_upstream_is_rejected(tmp_path):
    path = write_config(tmp_path, [entry("1", up_main="999")])
    with pytest.raises(ValueError, match="not in the list"):
        load_stations(path)


def test_target_without_w_is_rejected(tmp_path):
    path = write_config(tmp_path, [entry("1", parameters=["Q"])])
    with pytest.raises(ValueError, match="needs the water level W"):
        load_stations(path)


def test_input_without_q_is_rejected(tmp_path):
    path = write_config(tmp_path, [entry("1", role="input", parameters=["W"])])
    with pytest.raises(ValueError, match="needs the discharge Q"):
        load_stations(path)
