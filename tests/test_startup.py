import hashlib
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from querygate.app import create_app
from querygate.config import active_settings
from querygate.setup_config import SetupConfig


def test_launcher_uses_bundled_source_with_a_cached_environment(tmp_path):
    release = tmp_path / "QueryGate extracted"
    release.mkdir()
    launcher = Path(__file__).parents[1] / "start_querygate.py"
    shutil.copyfile(launcher, release / launcher.name)
    manifest = b"[project]\nname = 'querygate'\nversion = '0.1.0'\n"
    (release / "pyproject.toml").write_bytes(manifest)
    environment = release / ".venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    (environment / ".querygate-dependencies").write_text(hashlib.sha256(manifest).hexdigest(), encoding="ascii")

    source = release / "src" / "querygate"
    source.mkdir(parents=True)
    (source / "__init__.py").touch()
    (source / "__main__.py").write_text(
        "import sys\nassert sys.argv[1:] == ['--port', '8123']\nprint('bundled QueryGate started')\n",
        encoding="utf-8",
    )
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "QUERYGATE_CONFIG"}}
    process = subprocess.run(
        [sys.executable, release / launcher.name, "--port", "8123"], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=10,
    )
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == "bundled QueryGate started"


def test_startup_reports_all_missing_credentials_by_datasource(monkeypatch):
    example = Path(__file__).parents[1] / "querygate.example.yaml"
    monkeypatch.setenv("QUERYGATE_CONFIG", str(example))
    monkeypatch.setenv("QUERYGATE_ANALYST_TOKEN", "x" * 32)
    for name in ("FINANCE_ODBC", "ERP_DSN", "ERP_USER", "ERP_PASSWORD"):
        monkeypatch.delenv(f"QUERYGATE_{name}", raising=False)

    with pytest.raises(RuntimeError) as error:
        with TestClient(create_app()):
            pytest.fail("Startup accepted an incomplete configuration")

    message = str(error.value)
    assert "datasource finance (sqlserver): QUERYGATE_FINANCE_ODBC" in message
    assert "datasource erp (oracle): QUERYGATE_ERP_DSN, QUERYGATE_ERP_USER, QUERYGATE_ERP_PASSWORD" in message


def test_setup_saves_multiple_instances_and_keeps_credentials_separate(tmp_path):
    config_path, env_path = tmp_path / "querygate.yaml", tmp_path / ".env"
    setup = SetupConfig(config_path, env_path)
    setup.save_source("finance", "sqlserver", "Finance", True, "dbo.v_orders", "", {
        "connection_string": "Server=first;UID=reader;PWD=sample\\\\${value}';",
    }, {"analyst"}, existing=False)
    original_token = setup.client_token("analyst")
    setup.save_source("ledger", "sqlserver", "Ledger", True, "dbo.v_orders", "", {
        "connection_string": "Server=second;UID=reader;PWD=other;",
    }, {"analyst"}, existing=False)
    setup.save_source("erp", "oracle", "ERP", True, "AI_PUBLIC.V_ORDERS", "AI_PUBLIC.V_ORDERS.ORDER_ID | NUMBER", {
        "dsn": "fixture.invalid:1521/demo", "user": "reader", "password": "example",
    }, {"analyst"}, existing=False)
    setup.save_source("finance", "sqlserver", "Finance", False, "dbo.v_orders", "", {}, {"analyst"}, existing=True)

    saved = SetupConfig(config_path, env_path)
    assert saved.client_token("analyst") == original_token and original_token
    assert set(saved.sources) == {"finance", "ledger", "erp"}
    assert saved.value(saved.sources["finance"].connection_string_env) == "Server=first;UID=reader;PWD=sample\\\\${value}';"
    assert saved.value(saved.sources["ledger"].connection_string_env) == "Server=second;UID=reader;PWD=other;"
    assert set(active_settings(saved.settings).datasources) == {"ledger", "erp"}
    assert active_settings(saved.settings, "erp").clients["analyst"].datasources == ["erp"]
