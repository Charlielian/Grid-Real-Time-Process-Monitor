from __future__ import annotations

import json

import pytest

from backend.auth.cas_client import AuthError, parse_login_page, rsa_encrypt_pkcs1
from shared.config import AppConfig, AppPaths, ConfigStore


def test_parse_login_page() -> None:
    html = '''
    <form id="fm1"><input name="execution" value="e1-x"></form>
    <script>setPublicKey("MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A")</script>
    '''
    page = parse_login_page(html)
    assert page.execution == "e1-x"
    assert page.public_key.startswith("MIIB")


def test_parse_login_page_rejects_missing_fields() -> None:
    with pytest.raises(AuthError):
        parse_login_page('<form id="fm1"></form>')


def test_app_config_validation() -> None:
    config = AppConfig()
    assert config.origin.startswith("https://")
    with pytest.raises(ValueError):
        AppConfig(base_url="http://example.test")


def test_config_store_loads_yaml_and_json_runtime_overrides(tmp_path) -> None:
    paths = AppPaths(tmp_path / "data")
    paths.yaml = tmp_path / "config.yaml"
    paths.yaml.write_text(
        "web_host: '0.0.0.0'\n"
        "web_port: 5050\n"
        "target_process_title: '自定义流程'\n"
        "target_process_key: 'custom-key'\n"
        "target_title_keywords:\n"
        "  - '广州'\n"
        "  - '江门'\n"
        "poll_interval_seconds: 120\n",
        encoding="utf-8",
    )
    paths.settings.write_text(json.dumps({"poll_interval_seconds": 300}), encoding="utf-8")

    config = ConfigStore(paths).load()

    assert config.web_host == "0.0.0.0"
    assert config.web_port == 5050
    assert config.poll_interval_seconds == 300
    assert config.target_process_title == "自定义流程"
    assert config.auto_claim_pending_tasks is False


def test_config_store_loads_auto_claim_boolean(tmp_path) -> None:
    paths = AppPaths(tmp_path / "data")
    paths.yaml = tmp_path / "config.yaml"
    paths.yaml.write_text("auto_claim_pending_tasks: true\n", encoding="utf-8")
    assert ConfigStore(paths).load().auto_claim_pending_tasks is True

    paths.yaml.write_text("auto_claim_pending_tasks: 'false'\n", encoding="utf-8")
    assert ConfigStore(paths).load().auto_claim_pending_tasks is False


def test_config_store_invalid_yaml_uses_defaults(tmp_path) -> None:
    paths = AppPaths(tmp_path / "data")
    paths.yaml = tmp_path / "config.yaml"
    paths.yaml.write_text("web_port: 70000\n", encoding="utf-8")

    config = ConfigStore(paths).load()

    assert config.web_port == 5000
    assert config.target_title_keywords == ("阳江",)


def test_app_config_rejects_invalid_deployment_values() -> None:
    with pytest.raises(ValueError):
        AppConfig(web_port=0)
    with pytest.raises(ValueError):
        AppConfig(web_port=65536)
    with pytest.raises(ValueError):
        AppConfig(target_title_keywords=("",))
    with pytest.raises(ValueError):
        AppConfig(target_process_key=" ")


def test_rsa_pkcs1_encrypts_with_real_key() -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    import base64

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    encrypted = base64.b64decode(rsa_encrypt_pkcs1("账号", public))
    assert private.decrypt(encrypted, padding.PKCS1v15()) == "账号".encode()
