from launcher import main


def test_self_test_passes(capsys) -> None:
    assert main(["--self-test"]) == 0
    assert "autodiagnóstico correcto" in capsys.readouterr().out
