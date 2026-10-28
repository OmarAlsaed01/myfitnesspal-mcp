import pytest

from myfitnesspal_mcp import __version__, cli


def test_version_flag_prints_the_installed_version(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["mfp-mcp", "--version"])

    with pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 0
    assert capsys.readouterr().out == f"mfp-mcp {__version__}\n"
