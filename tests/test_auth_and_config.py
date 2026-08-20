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
    assert config.work_order_retention_days == 90
    assert config.wal_max_size_mb == 256
    with pytest.raises(ValueError):
        AppConfig(base_url="http://example.test")
    with pytest.raises(ValueError):
        AppConfig(work_order_retention_days=-1)
    with pytest.raises(ValueError):
        AppConfig(database_cleanup_interval_seconds=10)


def test_config_store_prefers_yaml_over_local_settings(tmp_path) -> None:
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
    assert config.poll_interval_seconds == 120
    assert config.target_process_title == "自定义流程"
    assert config.auto_claim_pending_tasks is False


def test_config_store_loads_auto_claim_boolean(tmp_path) -> None:
    paths = AppPaths(tmp_path / "data")
    paths.yaml = tmp_path / "config.yaml"
    paths.yaml.write_text("auto_claim_pending_tasks: true\nweb_host: '0.0.0.0'\n", encoding="utf-8")
    config = ConfigStore(paths).load()
    assert config.auto_claim_pending_tasks is True
    assert config.web_host == "0.0.0.0"

    paths.yaml.write_text("auto_claim_pending_tasks: 'false'\nweb_host: '0.0.0.0'\n", encoding="utf-8")
    config = ConfigStore(paths).load()
    assert config.auto_claim_pending_tasks is False
    assert config.web_host == "0.0.0.0"


def test_config_store_invalid_field_only_falls_back_and_logs(tmp_path, caplog) -> None:
    paths = AppPaths(tmp_path / "data")
    paths.yaml = tmp_path / "config.yaml"
    paths.yaml.write_text(
        "web_port: 70000\n"
        "web_host: '0.0.0.0'\n"
        "target_process_key: 'custom-key'\n",
        encoding="utf-8",
    )

    config = ConfigStore(paths).load()

    assert config.web_port == 5000
    assert config.web_host == "0.0.0.0"
    assert config.target_process_key == "custom-key"
    assert "source=config.yaml" in caplog.text
    assert "field=web_port" in caplog.text
    assert "fallback=default" in caplog.text


def test_config_store_ignores_unknown_fields_without_losing_valid_values(tmp_path, caplog) -> None:
    paths = AppPaths(tmp_path / "data")
    paths.yaml = tmp_path / "config.yaml"
    paths.yaml.write_text("unknown_option: true\nweb_host: '0.0.0.0'\n", encoding="utf-8")

    config = ConfigStore(paths).load()

    assert config.web_host == "0.0.0.0"
    assert "field=unknown_option" in caplog.text


def test_config_store_invalid_local_setting_does_not_replace_yaml(tmp_path, caplog) -> None:
    paths = AppPaths(tmp_path / "data")
    paths.yaml = tmp_path / "config.yaml"
    paths.yaml.write_text("poll_interval_seconds: 120\nweb_host: '0.0.0.0'\n", encoding="utf-8")
    paths.settings.write_text(json.dumps({"poll_interval_seconds": 99999}), encoding="utf-8")

    config = ConfigStore(paths).load()

    assert config.poll_interval_seconds == 120
    assert config.web_host == "0.0.0.0"
    assert "source=settings.json" in caplog.text
    assert "field=poll_interval_seconds" in caplog.text


def test_config_store_legacy_keyword_is_supported(tmp_path) -> None:
    paths = AppPaths(tmp_path / "data")
    paths.yaml = tmp_path / "config.yaml"
    paths.yaml.write_text("target_title_keyword: '广州'\n", encoding="utf-8")

    assert ConfigStore(paths).load().target_title_keywords == ("广州",)


def test_config_store_invalid_yaml_still_uses_defaults(tmp_path, caplog) -> None:
    paths = AppPaths(tmp_path / "data")
    paths.yaml = tmp_path / "config.yaml"
    paths.yaml.write_text("web_port: [\n", encoding="utf-8")

    config = ConfigStore(paths).load()

    assert config.web_port == 5000
    assert "配置源解析失败" in caplog.text


def test_config_store_invalid_json_does_not_affect_yaml(tmp_path, caplog) -> None:
    paths = AppPaths(tmp_path / "data")
    paths.yaml = tmp_path / "config.yaml"
    paths.yaml.write_text("web_host: '0.0.0.0'\n", encoding="utf-8")
    paths.settings.write_text("{", encoding="utf-8")

    config = ConfigStore(paths).load()

    assert config.web_host == "0.0.0.0"
    assert "source=settings.json" in caplog.text


def test_app_config_rejects_invalid_deployment_values() -> None:
    with pytest.raises(ValueError):
        AppConfig(web_port=0)
    with pytest.raises(ValueError):
        AppConfig(web_port=65536)
    with pytest.raises(ValueError):
        AppConfig(target_title_keywords=("",))
    with pytest.raises(ValueError):
        AppConfig(target_process_key=" ")


def test_app_config_rejects_invalid_maintenance_types() -> None:
    with pytest.raises(ValueError):
        AppConfig(database_max_size_mb="large")
    with pytest.raises(ValueError):
        AppConfig(wal_max_size_mb=True)
    with pytest.raises(ValueError):
        AppConfig(database_cleanup_batch_size=1.5)


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
