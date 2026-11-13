"""Environment settings shared by the lab and the module notebooks."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
CH09_DIR = REPO_ROOT / "ch09"
ENV_FILE = REPO_ROOT / ".env"
COMPOSE_FILE = CH09_DIR / "docker-compose.langfuse.yml"


_DEFAULT_TEMPERATURE_ONLY = ("gpt-5", "o1", "o3", "o4")


def takes_temperature(deployment: str) -> bool:
    """Can this deployment be sent `temperature=0`? GPT-5-family and o-series models accept only their default
    temperature: sending 0 is a 400 (verified live on gpt-5.2-chat). Judged from the deployment NAME, which usually
    starts with the model name; when it doesn't, set LLM_TEMPERATURE=off (or on) in .env to decide explicitly."""
    forced = os.getenv("LLM_TEMPERATURE", "").strip().lower()
    if forced in ("off", "false", "0", "no"):
        return False
    if forced in ("on", "true", "1", "yes"):
        return True
    return not (deployment or "").lower().startswith(_DEFAULT_TEMPERATURE_ONLY)


GPT5_RENAMED_PARAMS = {"max_tokens": "max_completion_tokens"}  # GPT-5 / o-series: 'max_tokens' is a 400 (verified live)


def sent_params(params: dict | None, deployment: str) -> dict:
    """The model kwargs the lab's agent really sends to `deployment` for `params` (e.g. a prompt config
    {"temperature": 0, "max_tokens": 400}): unchanged for a deployment that takes temperature; for a GPT-5 / o-series
    deployment, temperature dropped and max_tokens renamed max_completion_tokens (both are 400s otherwise). This is
    what the generation's modelParameters show, so verify compares against it."""
    if takes_temperature(deployment):
        return dict(params or {})
    return {GPT5_RENAMED_PARAMS.get(k, k): v for k, v in (params or {}).items() if k != "temperature"}


def azure_endpoint_root(url: str) -> str:
    """AzureChatOpenAI wants the resource root; .env endpoints sometimes include /openai/... paths."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


@dataclass(frozen=True)
class Settings:
    azure_endpoint: str
    azure_api_key: str
    azure_api_version: str
    agent_deployment: str
    expensive_deployment: str
    langfuse_base_url: str

    @property
    def single_model(self) -> bool:
        """One deployment for everything (agent == judge/expensive, e.g. one gpt-5.4): lessons that compare a cheap
        and an expensive model switch to their single-model variant."""
        return self.agent_deployment == self.expensive_deployment

    @classmethod
    def from_env(cls, load: bool = True) -> "Settings":
        if load:
            load_dotenv(ENV_FILE)
        missing = [k for k in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY") if not os.getenv(k)]
        if missing:
            raise RuntimeError(f"Missing env vars {missing} (expected in {ENV_FILE})")
        return cls(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            azure_api_key=os.environ["AZURE_OPENAI_API_KEY"],
            azure_api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
            agent_deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini"),
            expensive_deployment=os.getenv("EVAL_JUDGE_MODEL", "gpt-5.2-chat"),
            langfuse_base_url=os.getenv("LANGFUSE_BASE_URL", "http://localhost:3000"),
        )
