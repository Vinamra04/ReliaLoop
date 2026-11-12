from itertools import count
from pathlib import Path

from tools import init_env

EXAMPLE = """# comment
PROJECT_NAME=relialoop
WEBHOOK_TOKEN=__GENERATE__
CHAOS_TOKEN=__GENERATE__
GEMINI_API_KEY=
"""


def _factory():
    counter = count(1)
    return lambda: f"tok{next(counter)}"


def test_fresh_env_generates_every_marked_token():
    text, generated, added = init_env.merge_env(EXAMPLE, None, _factory())
    values = init_env.parse_env(text)
    assert values["WEBHOOK_TOKEN"] == "tok1"
    assert values["CHAOS_TOKEN"] == "tok2"
    assert values["PROJECT_NAME"] == "relialoop"
    assert generated == ("WEBHOOK_TOKEN", "CHAOS_TOKEN")
    assert added == ()
    assert init_env.GENERATE_MARKER not in text


def test_existing_values_are_never_overwritten():
    existing = "WEBHOOK_TOKEN=keepme\nGEMINI_API_KEY=user-pasted\nPROJECT_NAME=custom\n"
    text, generated, added = init_env.merge_env(EXAMPLE, existing, _factory())
    values = init_env.parse_env(text)
    assert values["WEBHOOK_TOKEN"] == "keepme"
    assert values["GEMINI_API_KEY"] == "user-pasted"
    assert values["PROJECT_NAME"] == "custom"
    assert values["CHAOS_TOKEN"] == "tok1"
    assert generated == ("CHAOS_TOKEN",)
    assert added == ("CHAOS_TOKEN",)


def test_local_additions_survive():
    text, _, _ = init_env.merge_env(EXAMPLE, "MY_EXTRA=1\n", _factory())
    assert init_env.parse_env(text)["MY_EXTRA"] == "1"


def test_run_init_is_idempotent(tmp_path: Path):
    example = tmp_path / ".env.example"
    example.write_text(EXAMPLE, encoding="utf-8")
    env_file = tmp_path / ".env"
    token_file = tmp_path / ".secrets" / "webhook_token"
    runtime = (tmp_path / "postmortems",)

    first = init_env.run_init(example, env_file, token_file, runtime)
    first_text = env_file.read_text(encoding="utf-8")
    second = init_env.run_init(example, env_file, token_file, runtime)

    assert first.env_created is True
    assert first.webhook_file_written is True
    assert second.env_created is False
    assert second.generated_keys == ()
    assert second.webhook_file_written is False
    assert env_file.read_text(encoding="utf-8") == first_text
    assert token_file.read_text(encoding="utf-8") == init_env.parse_env(first_text)["WEBHOOK_TOKEN"]
    assert runtime[0].is_dir()
