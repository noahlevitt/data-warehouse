"""Customer Records runs on port 8502 everywhere (1.0.1): the file converter keeps 8501 on the same computer.

Every way of starting the app must agree, or one of them would collide with the converter: the Streamlit settings,
run_local.sh (also with PORT=…), run_local.bat, the Docker image and docker-compose.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8502


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_streamlit_settings_use_8502():
    assert tomllib.loads(read(".streamlit/config.toml"))["server"]["port"] == PORT


def test_run_local_sh_defaults_to_8502_and_keeps_the_port_override():
    assert re.findall(r'--server\.port "\$\{PORT:-(\d+)\}"', read("run_local.sh")) == [str(PORT)]


def test_run_local_bat_uses_8502():
    assert re.findall(r"--server\.port (\d+)", read("run_local.bat")) == [str(PORT)]


def test_docker_image_serves_and_checks_8502():
    text = read("Dockerfile")
    assert re.findall(r"^EXPOSE (\d+)", text, re.M) == [str(PORT)]
    assert re.findall(r"--server\.port=(\d+)", text) == [str(PORT)]
    assert re.findall(r"localhost:(\d+)/_stcore/health", text) == [str(PORT)]


def test_docker_compose_publishes_8502():
    assert re.findall(r'"(\d+):(\d+)"', read("docker-compose.yml")) == [(str(PORT), str(PORT))]


def test_every_address_given_to_people_is_8502():
    for name in ["run_local.sh", "run_local.bat", "Dockerfile", "README.md", "docs/USER_GUIDE.md"]:
        ports = re.findall(r"(?:http://|reverse_proxy )localhost:(\d+)", read(name))
        assert ports and set(ports) == {str(PORT)}, (name, ports)
