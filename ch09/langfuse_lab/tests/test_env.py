import importlib.metadata as md


def test_pinned_versions():
    assert md.version("langfuse").startswith("5.")
    assert md.version("openai").startswith("2.")
    assert md.version("langgraph").startswith("1.")
    assert md.version("langchain-core").split(".")[0] == "1"


def test_v5_langchain_integration_imports():
    from langfuse.langchain import CallbackHandler  # noqa: F401
    from langgraph.prebuilt import ToolNode, tools_condition  # noqa: F401
    from langchain_openai import AzureChatOpenAI  # noqa: F401
