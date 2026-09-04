import hashlib
from pathlib import Path

from sec_inline_financials.sec_transform_plugin import PluginFile, ensure_sec_transform_plugin


def test_sec_transform_plugin_is_downloaded_verified_and_then_reused(tmp_path: Path) -> None:
    contents = {"__init__.py": b"plugin", "text2num.py": b"helper"}
    manifest = tuple(
        PluginFile(name=name, sha256=hashlib.sha256(content).hexdigest())
        for name, content in contents.items()
    )
    downloads: list[str] = []

    def fetch(url: str) -> bytes:
        name = url.rsplit("/", 1)[-1]
        downloads.append(name)
        return contents[name]

    plugin_dir = ensure_sec_transform_plugin(
        tmp_path, fetch_bytes=fetch, manifest=manifest, commit="test-commit"
    )
    reused_dir = ensure_sec_transform_plugin(
        tmp_path, fetch_bytes=fetch, manifest=manifest, commit="test-commit"
    )

    assert plugin_dir == reused_dir
    assert (plugin_dir / "__init__.py").read_bytes() == b"plugin"
    assert (plugin_dir / "text2num.py").read_bytes() == b"helper"
    assert downloads == ["__init__.py", "text2num.py"]
