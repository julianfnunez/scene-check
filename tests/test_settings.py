import json

import pytest

from settings import load_config


def configure(tmp_path, monkeypatch, sensors, scenes):
    for env, name, data in [('SENSORS_FILE', 'sensors.yaml', sensors),
                            ('SCENES_FILE', 'scenes.yaml', scenes)]:
        path = tmp_path / name
        path.write_text(json.dumps(data))
        monkeypatch.setenv(env, str(path))


SENSORS = {'servers': {'doors': {'command': 'python', 'args': ['-m', 'mcp_servers.yolink_server']}},
           'sensors': {'door': {'name': 'Door', 'server': 'doors', 'tool': 'read_door'}}}
SCENES = {'scenes': {'coop': {'name': 'Coop', 'sensors': {'door': {'evaluations': 1}},
                             'conditions': ['The door is closed.']}}}


def test_split_config(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch, SENSORS, SCENES)
    config = load_config()
    assert config.scenes['coop'].sensors['door'].evaluations == 1
    assert config.sensors['door'].server == 'doors'
    assert config.servers['doors'].args == ['-m', 'mcp_servers.yolink_server']


def test_combined_config_ignores_external_file(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch, None, {**SENSORS, **SCENES})
    assert load_config().sensors['door'].name == 'Door'


@pytest.mark.parametrize('sensors,scenes', [
    ({'servers': {}, 'sensors': SENSORS['sensors']}, SCENES),
    ({'servers': SENSORS['servers'], 'sensors': {}}, SCENES),
    ({**SENSORS, 'scenes': {}}, SCENES),
    (SENSORS, {**SCENES, 'unexpected': True}),
    (None, SCENES),
])
def test_invalid_split_config(tmp_path, monkeypatch, sensors, scenes):
    configure(tmp_path, monkeypatch, sensors, scenes)
    with pytest.raises(ValueError):
        load_config()


def test_missing_sensor_file(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch, SENSORS, SCENES)
    (tmp_path / 'sensors.yaml').unlink()
    with pytest.raises(FileNotFoundError):
        load_config()
