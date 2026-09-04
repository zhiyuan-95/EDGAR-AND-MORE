from pathlib import Path

from sec_inline_financials.config import load_sec_user_agent


def test_loads_sec_user_agent_from_config_env_without_importing_other_keys(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.env"
    config.write_text(
        'OPENAI_API_KEY=must-not-be-loaded\nSEC_USER_AGENT="Example Analyst analyst@example.com"\n',
        encoding="utf-8",
    )
    environment = {"EXISTING": "unchanged"}

    user_agent = load_sec_user_agent(working_directory=tmp_path, environment=environment)

    assert user_agent == "Example Analyst analyst@example.com"
    assert environment == {"EXISTING": "unchanged"}


def test_process_environment_takes_precedence_over_config_file(tmp_path: Path) -> None:
    (tmp_path / "config.env").write_text(
        "SEC_USER_AGENT=File Analyst file@example.com\n", encoding="utf-8"
    )

    user_agent = load_sec_user_agent(
        working_directory=tmp_path,
        environment={"SEC_USER_AGENT": "Environment Analyst env@example.com"},
    )

    assert user_agent == "Environment Analyst env@example.com"


def test_config_txt_is_supported_as_a_fallback_name(tmp_path: Path) -> None:
    (tmp_path / "config.txt").write_text(
        "SEC_USER_AGENT='Text Analyst text@example.com'\n", encoding="utf-8"
    )

    user_agent = load_sec_user_agent(working_directory=tmp_path, environment={})

    assert user_agent == "Text Analyst text@example.com"
