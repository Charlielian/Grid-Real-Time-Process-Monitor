from __future__ import annotations

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


def _write_complete_config(paths, **updates) -> None:
    config = AppConfig(**updates)
    payload = {
        field.name: getattr(config, field.name)
        for field in __import__("dataclasses").fields(AppConfig)
    }
    import yaml
    paths.yaml.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")


def test_config_store_reads_only_complete_yaml(tmp_path, monkeypatch) -> None:
    paths = AppPaths(tmp_path / "data")
    paths.yaml = tmp_path / "config.yaml"
    _write_complete_config(paths, web_host="0.0.0.0", web_port=5050, target_title_keywords=("阳江",))
    (paths.root / "settings.json").write_text('{"web_port": 5999}', encoding="utf-8")
    monkeypatch.setenv("GRID_MONITOR_CONFIG", str(tmp_path / "other.yaml"))

    config = ConfigStore(paths).load()

    assert config.web_host == "0.0.0.0"
    assert config.web_port == 5050
    assert config.target_title_keywords == ("阳江",)


def test_config_store_rejects_missing_or_partial_yaml(tmp_path) -> None:
    paths = AppPaths(tmp_path / "data")
    paths.yaml = tmp_path / "config.yaml"
    with pytest.raises(FileNotFoundError):
        ConfigStore(paths).load()
    paths.yaml.write_text("web_host: '0.0.0.0'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺少字段"):
        ConfigStore(paths).load()


def test_config_store_rejects_invalid_unknown_and_empty_yaml(tmp_path) -> None:
    paths = AppPaths(tmp_path / "data")
    paths.yaml = tmp_path / "config.yaml"
    paths.yaml.write_text("unknown_option: true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="未知字段"):
        ConfigStore(paths).load()
    paths.yaml.write_text("web_port: 70000\n", encoding="utf-8")
    with pytest.raises(ValueError, match="配置字段无效"):
        ConfigStore(paths).load()
    paths.yaml.write_text("web_port: [\n", encoding="utf-8")
    with pytest.raises(ValueError, match="解析失败"):
        ConfigStore(paths).load()
    paths.yaml.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="为空"):
        ConfigStore(paths).load()


def test_config_store_legacy_keyword_is_supported(tmp_path) -> None:
    paths = AppPaths(tmp_path / "data")
    paths.yaml = tmp_path / "config.yaml"
    config = AppConfig()
    import yaml
    payload = {field.name: getattr(config, field.name) for field in __import__("dataclasses").fields(AppConfig)}
    payload.pop("target_title_keywords")
    payload["target_title_keyword"] = "广州"
    paths.yaml.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    assert ConfigStore(paths).load().target_title_keywords == ("广州",)


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
