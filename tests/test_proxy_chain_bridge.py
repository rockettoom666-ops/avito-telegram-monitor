from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def load_bridge_module():
    script = (
        Path(__file__).parents[1]
        / "deploy"
        / "debian"
        / "avito_http_chain.py"
    )
    spec = spec_from_file_location("avito_http_chain", script)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_proxy_authorization_header_is_optional() -> None:
    module = load_bridge_module()

    assert module.proxy_authorization_header("", "") == b""
    assert module.proxy_authorization_header("user", "pass") == (
        b"Proxy-Authorization: Basic dXNlcjpwYXNz\r\n"
    )
