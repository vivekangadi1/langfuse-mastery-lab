"""Create .env from .env.example, generating every `<generate>` value. Standard library only.

    python make_env.py          # refuses to overwrite an existing .env
    python make_env.py --force  # overwrite (the running stack keeps its OLD secrets: only for a fresh stack)

Afterwards, fill in AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY (and the deployment names) by hand.
"""
import secrets
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXAMPLE, TARGET = ROOT / ".env.example", ROOT / ".env"


def generated(key: str) -> str:
    if key == "LANGFUSE_PUBLIC_KEY":
        return f"pk-lf-{uuid.uuid4()}"
    if key == "LANGFUSE_SECRET_KEY":
        return f"sk-lf-{uuid.uuid4()}"
    if key == "LANGFUSE_ENCRYPTION_KEY":
        return secrets.token_hex(32)  # Langfuse requires exactly 64 hex characters
    return secrets.token_hex(16)      # hex only: some of these end up inside URLs (e.g. DATABASE_URL)


def main() -> None:
    if TARGET.exists() and "--force" not in sys.argv:
        sys.exit(f"{TARGET} already exists; not overwriting (pass --force for a brand-new stack only).")
    lines = []
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and value.strip() == "<generate>":
            line = f"{key}={generated(key.strip())}"
        lines.append(line)
    TARGET.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {TARGET}. Now fill in AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY.")


if __name__ == "__main__":
    main()
