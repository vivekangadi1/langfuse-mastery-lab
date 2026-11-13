"""LangGraph support agent. Contains NO Langfuse code: notebooks pass instrumented pieces in.

Graph: START -> guardrail -> agent <-> tools -> END   (guardrail -> END when blocked)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Callable, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from .config import Settings, azure_endpoint_root, takes_temperature
from .failures import FailureConfig
from .guardrail import GuardrailResult, check_input
from .prompts import system_prompt as _default_prompt
from .tools import ToolContext, make_tools

REFUSAL = "I can't help with that request."
MAX_STEPS = 12


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    guardrail_flags: list
    blocked: bool


def make_llm(settings: Settings, *, expensive: bool = False) -> BaseChatModel:
    """The agent deployment runs at temperature 0. The expensive deployment (gpt-5.2-chat) only accepts its default
    temperature: sending 0 is a 400 (verified live), so it gets none. Neither does an agent deployment from the
    GPT-5 / o-series family (e.g. a single gpt-5.4 deployment used for everything): see `config.takes_temperature`."""
    from langchain_openai import AzureChatOpenAI

    deployment = settings.expensive_deployment if expensive else settings.agent_deployment
    sampling = {"temperature": 0} if not expensive and takes_temperature(deployment) else {}
    return AzureChatOpenAI(
        azure_deployment=deployment,
        azure_endpoint=azure_endpoint_root(settings.azure_endpoint),
        api_key=settings.azure_api_key,
        api_version=settings.azure_api_version,
        timeout=120,
        max_retries=2,
        **sampling,
    )


RENAME_FOR_GPT5 = {"max_tokens": "max_completion_tokens"}  # GPT-5 / o-series: 'max_tokens' is a 400 (verified live)


def turn_params(params: dict | None, drop: frozenset = frozenset(), rename: dict | None = None) -> dict | None:
    """The per-turn model kwargs actually bound: `params` without the keys the deployment rejects (e.g. a prompt
    config's temperature on a GPT-5-family deployment), with keys it spells differently renamed (max_tokens ->
    max_completion_tokens). None when nothing is left."""
    rename = rename or {}
    kept = {rename.get(k, k): v for k, v in (params or {}).items() if k not in drop}
    return kept or None


def build_graph(llm: BaseChatModel, tools, *, prompt: str | Callable[[], str],
                guardrail_fn: Callable[[str], GuardrailResult], llm_params: Callable[[], dict] | None = None,
                drop_params: frozenset = frozenset(), rename_params: dict | None = None):
    """`prompt` is the system prompt, or a zero-arg callable read on every agent step (so a caller can swap it per
    turn). `llm_params`, if given, returns extra model kwargs (e.g. temperature, max_tokens) bound per agent step;
    keys in `drop_params` are never bound (the deployment would reject them)."""
    bound = llm.bind_tools(tools)
    get_prompt = prompt if callable(prompt) else (lambda: prompt)

    def guardrail(state: AgentState):
        last_user = next(m for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
        result = guardrail_fn(last_user.content)
        update = {"guardrail_flags": list(result.flags), "blocked": not result.allowed}
        if not result.allowed:
            update["messages"] = [AIMessage(content=REFUSAL)]
        return update

    def agent(state: AgentState):
        params = turn_params(llm_params() if llm_params else None, drop_params, rename_params)
        model = bound.bind(**params) if params else bound
        return {"messages": [model.invoke([SystemMessage(content=get_prompt()), *state["messages"]])]}

    graph = StateGraph(AgentState)
    graph.add_node("guardrail", guardrail)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "guardrail")
    graph.add_conditional_edges("guardrail", lambda s: END if s["blocked"] else "agent", ["agent", END])
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")
    return graph.compile()


def _is_error_result(tool_message: ToolMessage) -> bool:
    """Argument-validation failures: ToolNode sets status="error". Business errors: a JSON dict with "error"."""
    if getattr(tool_message, "status", None) == "error":
        return True
    content = tool_message.content
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return False
    return isinstance(content, dict) and "error" in content


def summarize_run(messages: list[BaseMessage]) -> dict:
    """Same field names the ch09 evaluators already read. Pass only the messages of the turn to summarize."""
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    tool_calls = []
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                tool_message = results.get(call["id"])
                output = tool_message.content if tool_message else None
                failed = tool_message is not None and _is_error_result(tool_message)
                status = "error" if failed else "success"
                tool_calls.append({"name": call["name"], "input": call["args"], "output": output, "status": status})
    handoffs = [c for c in tool_calls if c["name"] == "create_handoff" and c["status"] == "success"]
    final = next((m.content for m in reversed(messages) if isinstance(m, AIMessage) and not m.tool_calls), "")
    return {
        "steps_completed": [c["name"] for c in tool_calls],
        "tool_calls": tool_calls,
        "handoff_target": handoffs[-1]["input"]["target"] if handoffs else "none",
        "retry_count": sum(1 for c in tool_calls if c["status"] == "error"),
        "final_answer": final,
    }


@dataclass
class AgentRun:
    messages: list
    summary: dict
    guardrail_flags: list
    blocked: bool


class SupportAgent:
    def __init__(self, settings: Settings | None = None, *, failures: FailureConfig | None = None,
                 llm: BaseChatModel | None = None, guardrail_fn=check_input, search_fn=None,
                 system_prompt: str | None = None):
        """`system_prompt` replaces the built-in prompt (`prompts.system_prompt(failures)`) for every turn."""
        self.failures = failures or FailureConfig()
        self.drop_params: frozenset = frozenset()
        self.rename_params: dict = {}
        if llm is None:
            settings = settings or Settings.from_env()
            expensive = self.failures.cost_spike
            llm = make_llm(settings, expensive=expensive)
            deployment = settings.expensive_deployment if expensive else settings.agent_deployment
            if expensive or not takes_temperature(deployment):
                self.drop_params = frozenset({"temperature"})  # make_llm sent none; a prompt config must not either
                self.rename_params = dict(RENAME_FOR_GPT5)
        self.ctx = ToolContext(failures=self.failures, search_fn=search_fn)
        self.system_prompt = system_prompt if system_prompt is not None else _default_prompt(self.failures)
        self._turn_prompt: str | None = None
        self._turn_params: dict | None = None
        self.graph = build_graph(llm, make_tools(self.ctx), prompt=lambda: self._turn_prompt or self.system_prompt,
                                 guardrail_fn=guardrail_fn, llm_params=lambda: self._turn_params,
                                 drop_params=self.drop_params, rename_params=self.rename_params)

    def invoke(self, user_message: str, *, history: list | None = None, config: dict | None = None,
               system_prompt: str | None = None, llm_params: dict | None = None) -> AgentRun:
        """Run one turn. `config` is passed to LangGraph, e.g. {"callbacks": [langfuse_handler]}.

        `system_prompt` overrides the agent's system prompt for this turn only (e.g. one served by prompt
        management); `llm_params` adds model kwargs for this turn's LLM calls (e.g. {"temperature": 0, "max_tokens": 400}).

        `summary` is per-turn: it describes only the messages produced after `history`.
        `messages` is the full list, so it can be passed back as the next turn's `history`.
        Raises GraphRecursionError when the run exceeds MAX_STEPS. Exceptions raised inside
        injected functions (llm, guardrail_fn, search_fn) propagate; only tool argument-validation
        errors become error ToolMessages. Use one SupportAgent per concurrent conversation.
        """
        self.ctx.handoffs.clear()
        n = len(history or [])
        self._turn_prompt, self._turn_params = system_prompt, dict(llm_params) if llm_params else None
        try:
            state = self.graph.invoke(
                {"messages": [*(history or []), HumanMessage(content=user_message)], "guardrail_flags": [], "blocked": False},
                config={"recursion_limit": MAX_STEPS, **(config or {})},
            )
        finally:
            self._turn_prompt, self._turn_params = None, None
        return AgentRun(
            messages=state["messages"],
            summary=summarize_run(state["messages"][n:]),
            guardrail_flags=state["guardrail_flags"],
            blocked=state["blocked"],
        )
