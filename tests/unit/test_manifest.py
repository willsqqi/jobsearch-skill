import json
from pathlib import Path


def test_plugin_manifest_contract() -> None:
    manifest = json.loads(Path(".codex-plugin/plugin.json").read_text())
    assert manifest["name"] == "jobsearch-skill"
    assert manifest["version"].split("+", 1)[0] == "0.1.0"
    assert manifest["skills"] == "./skills/"
    assert manifest["interface"]["displayName"] == "Jobsearch"
    assert len(manifest["interface"]["defaultPrompt"]) == 3
