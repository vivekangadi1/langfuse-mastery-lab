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
