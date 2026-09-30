import pytest

from hub.cli import COMMANDS, main

P0_COMMANDS = [
    "db init",
    "db migrate",
    "db backup",
    "db restore-check",
    "import claude-export",
    "ic pull",
    "ic docs",
    "session-open",
]
IMPLEMENTED = {"db init", "db migrate", "db backup"}
STUBS = [command for command in P0_COMMANDS if command not in IMPLEMENTED]


def test_help_lists_every_p0_subcommand(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for command in P0_COMMANDS:
        assert f"hub {command}" in out, f"{command} missing from --help"


def test_dispatch_table_matches_p0_surface():
    table = [
        name if group is None else f"{group} {name}"
        for group, subs in COMMANDS.items()
        for name in subs
    ]
    assert sorted(table) == sorted(P0_COMMANDS)


def test_no_command_prints_help_and_exits_0(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: hub")
    for command in P0_COMMANDS:
        assert f"hub {command}" in out


def test_stub_exits_2_with_message(capsys):
    assert main(["db", "restore-check"]) == 2
    captured = capsys.readouterr()
    assert "not implemented" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("command", STUBS)
def test_every_stub_exits_2(command, capsys):
    assert main(command.split()) == 2
    assert command in capsys.readouterr().err
