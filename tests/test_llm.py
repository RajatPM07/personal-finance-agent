from unittest.mock import MagicMock, patch


def test_llm_routes_pdf_extraction_to_gemini_flash():
    from skills.finance.lib.llm import llm
    with patch("skills.finance.lib.llm.litellm.completion") as mock_completion:
        mock_completion.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="ok"))])
        llm("pdf_extraction", prompt="hello")
        _, kwargs = mock_completion.call_args
        assert kwargs["model"] == "gemini/gemini-2.5-flash"
        assert "anthropic/claude-sonnet-4-6" in kwargs["fallbacks"]
        assert kwargs["metadata"]["task"] == "pdf_extraction"


def test_llm_unknown_task_raises():
    import pytest

    from skills.finance.lib.llm import llm
    with pytest.raises(KeyError):
        llm("no_such_task", prompt="x")
