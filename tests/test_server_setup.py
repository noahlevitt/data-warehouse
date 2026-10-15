"""The shared server (1.5.0): the Docker setup always has logins, five imports run at once, no known password.

docker-compose.yml is the setup for the team's server; .env.example is what every install copies.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def env_example() -> dict[str, str]:
    """The settings .env.example sets (KEY=value lines, comments left out)."""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return dict(re.findall(r"^([A-Z_]+)=(.*)$", text, re.M))


def compose_app() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["app"]


def test_the_docker_setup_always_has_logins():
    app = compose_app()
    assert app["environment"]["AUTH_MODE"] == "password"  # environment wins over env_file (.env)
    assert app["env_file"] == ".env"


def test_the_app_is_published_on_the_server_itself_only():
    """Reached through Caddy (HTTPS): Docker's published ports bypass the firewall, so not on every address."""
    assert compose_app()["ports"] == ["127.0.0.1:8502:8502"]


def test_no_admin_password_is_shipped():
    """A password written in .env.example would be known to everyone who has the code."""
    env = env_example()
    assert env["ADMIN_USERNAME"] == "admin" and env["ADMIN_PASSWORD"] == ""


def test_five_imports_at_once():
    assert env_example()["IMPORT_WORKERS"] == "5"
