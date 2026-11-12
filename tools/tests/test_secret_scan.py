from pathlib import Path

from tools import secret_scan

# Assembled at runtime so this test file never contains a key-shaped literal.
FAKE_GOOGLE_KEY = "AI" + "za" + "B" * 35


def test_detects_google_api_key_shape():
    hits = secret_scan.scan_text(f"line one\nkey = {FAKE_GOOGLE_KEY}\n")
    assert (2, "google-api-key") in hits


def test_detects_assigned_gemini_key():
    hits = secret_scan.scan_text("GEMINI_API_KEY" + "=" + "x" * 30)
    assert [name for _, name in hits] == ["assigned-gemini-key"]


def test_placeholders_are_clean():
    assert secret_scan.scan_text("GEMINI_API_KEY=\nWEBHOOK_TOKEN=__GENERATE__\n") == []


def test_main_reports_without_printing_the_secret(tmp_path: Path, capsys):
    leaky = tmp_path / "leak.txt"
    leaky.write_text(FAKE_GOOGLE_KEY, encoding="utf-8")
    clean = tmp_path / "clean.txt"
    clean.write_text("nothing here", encoding="utf-8")

    assert secret_scan.main([str(clean)]) == 0
    assert secret_scan.main([str(leaky)]) == 1
    err = capsys.readouterr().err
    assert "google-api-key" in err
    assert FAKE_GOOGLE_KEY not in err


def test_exact_env_values_are_detected_whatever_their_format(tmp_path: Path, capsys):
    env = tmp_path / ".env"
    env.write_text(
        "LOG_LEVEL=INFO\nGEMINI_API_KEY=new.format-key_0123456789abcdef\nWEBHOOK_TOKEN=short\n"
        "POSTGRES_PASSWORD=correct-horse-battery-staple\n",
        encoding="utf-8",
    )
    secrets = secret_scan.env_secrets(env)
    assert set(secrets) == {"GEMINI_API_KEY", "POSTGRES_PASSWORD"}

    hits = secret_scan.scan_text("x = 'new.format-key_0123456789abcdef'\nfine\n", secrets)
    assert hits == [(1, "value-of-GEMINI_API_KEY")]
    assert secret_scan.scan_text("nothing secret here", secrets) == []
    assert secret_scan.env_secrets(tmp_path / "missing.env") == {}
