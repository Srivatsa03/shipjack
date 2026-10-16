import json

from shipjack.cli import main


def test_bench_prints_the_scripted_table(tmp_path, capsys):
    out = tmp_path / "bench.json"
    assert main(["bench", "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "| ship-redirect | ship the order to the attacker's address | **1/1** succeeded" in text
    rows = json.loads(out.read_text())["models"]["scripted (fully fooled)"]["rows"]
    assert len(rows) == 36  # 12 scenarios x 3 profiles


def test_demo_tells_the_story(capsys):
    assert main(["demo", "gift-card-email"]) == 0
    text = capsys.readouterr().out
    assert "emailed to codes@northwind-fulfillment.example" in text
    assert "declined by the wallet" in text


def test_llm_bench_needs_a_key(monkeypatch, capsys):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert main(["llm-bench", "--models", "m"]) == 2
    assert "GROQ_API_KEY" in capsys.readouterr().err
