from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def load_config_module():
    script = (
        Path(__file__).parents[1]
        / "deploy"
        / "debian"
        / "configure_avito_proxy_chain.py"
    )
    spec = spec_from_file_location("configure_avito_proxy_chain", script)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_managed_mihomo_blocks_are_removed_without_touching_vpn() -> None:
    module = load_config_module()
    configured = """mixed-port: 7890
# BEGIN MANAGED AVITO PURCHASED PROXIES
proxies:
  - name: AVITO-PURCHASED-1
# END MANAGED AVITO PURCHASED PROXIES
proxy-providers:
  vpn-subscription:
    type: http
# BEGIN MANAGED AVITO LISTENER
listeners:
  - name: avito-chain-in
# END MANAGED AVITO LISTENER
proxy-groups:
  # BEGIN MANAGED AVITO GROUP
  - name: AVITO-PURCHASED
  # END MANAGED AVITO GROUP
  - name: VPN
    type: select
rules:
  - MATCH,VPN
"""

    cleaned = module.remove_managed_blocks(configured)

    assert "MANAGED AVITO" not in cleaned
    assert "AVITO-PURCHASED" not in cleaned
    assert "vpn-subscription" in cleaned
    assert "- name: VPN" in cleaned


def test_proxy_credentials_are_decoded_without_being_dropped(tmp_path: Path) -> None:
    module = load_config_module()
    rotation = tmp_path / "rotation.py"
    rotation.write_text(
        'PROXY_URLS = ("http://test%40user:p%3Ass@192.0.2.10:1080",)\n',
        encoding="utf-8",
    )
    module.ROTATION_PATH = rotation

    assert module.load_proxy_endpoints() == [
        {
            "host": "192.0.2.10",
            "port": 1080,
            "username": "test@user",
            "password": "p:ss",
        }
    ]
