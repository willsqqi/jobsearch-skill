from jobsearch_skill.cli import main


def test_version_envelope(capsys) -> None:
    assert main(["--version"]) == 0
    output = capsys.readouterr().out
    assert '"command": "version"' in output
    assert '"version": "0.1.0"' in output
