from pathlib import Path

import pytest

from vdb.config import Camera, load_settings


@pytest.mark.parametrize("name", ["local.example.yaml", "cloud.example.yaml", "smoke.yaml"])
def test_shipped_configs_load(name):
    assert load_settings(Path("configs") / name).sites


def test_camera_url_from_env(monkeypatch):
    cam = Camera(id="c", room="r", source="env:CAM_URL")
    monkeypatch.setenv("CAM_URL", "rtsp://u:p@cam/1")
    assert cam.stream_url() == "rtsp://u:p@cam/1"


def test_missing_camera_env_names_variable_not_value(monkeypatch):
    monkeypatch.delenv("CAM_URL", raising=False)
    with pytest.raises(RuntimeError, match="CAM_URL is not set"):
        Camera(id="c", room="r", source="env:CAM_URL").stream_url()
