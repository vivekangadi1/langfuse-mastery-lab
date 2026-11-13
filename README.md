# Langfuse mastery lab

A hands-on course in production Langfuse:
- **What you instrument:** a real LangGraph customer-support agent on Azure OpenAI, traced into a **self-hosted** Langfuse (Docker).
- **What you learn:** observability, scores, LLM judges, human review, datasets and experiments, a CI gate, analytics, alerts and production ops.
- **How it's structured:** 15 modules, `ch09/langfuse_mastery/00_setup.ipynb` … `14_capstone.ipynb`. The course guide is [`ch09/langfuse_mastery/README.md`](ch09/langfuse_mastery/README.md).

## What you need
- **Docker Desktop**, running, with about 4 GB of RAM for the stack.
- **Python 3.13**, plus [`uv`](https://docs.astral.sh/uv/) (optional, faster).
- **Git** and **VS Code** with the Jupyter extension.
- **An Azure OpenAI deployment.** One is enough.

## Setup (once per machine)
```powershell
git clone https://github.com/vivekangadi1/langfuse-mastery-lab.git
cd langfuse-mastery-lab
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

`setup.ps1` does three things:
- creates `ch09/.venv-langfuse` with the pinned packages (`requirements-langfuse.txt`);
- registers the Jupyter kernel **Python (ch09 Langfuse v5)**;
- writes `.env` with freshly generated Langfuse secrets (`make_env.py`).

Then:
1. **Edit `.env`:** set `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` and the deployment names.
   - **With one model** (e.g. `gpt-5.4`), set **both** `AZURE_OPENAI_DEPLOYMENT` and `EVAL_JUDGE_MODEL` to that deployment name.
   - Use the `AZURE_OPENAI_API_VERSION` your Azure deployment page shows.
2. **Start Langfuse:**
   ```powershell
   docker compose --env-file .env -f ch09/docker-compose.langfuse.yml up -d
   ```
   The first start pulls the images, which takes a few minutes. It's ready when http://localhost:3000/api/public/health says `OK`.
3. **Log in** at http://localhost:3000 with `LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD` from `.env`. The organization, the project `ch09-eval-project` and its API keys are created automatically on first boot.
4. **Open** `ch09/langfuse_mastery/00_setup.ipynb` and pick the kernel **Select Kernel → Jupyter Kernel… → Python (ch09 Langfuse v5)**.

## Everyday use
| | |
|---|---|
| Stop the stack (keeps all data) | `docker compose --env-file .env -f ch09/docker-compose.langfuse.yml stop` |
| Start it again | `... up -d` (same command as setup) |
| Lab unit tests | `cd ch09; .venv-langfuse\Scripts\python.exe -m pytest langfuse_lab/tests -q` |
| Ports used | 3000 (UI/API), 9090 (MinIO S3). Local-only: 3030, 5433, 6380, 8123, 9091, 19020 |

**Never run `docker compose ... down -v`.** It deletes every trace, score and prompt.

## Troubleshooting
- **`docker` can't connect:** Docker Desktop isn't running. Start it and wait for "Engine running".
- **The UI isn't up several minutes after an unclean shutdown:** Postgres is replaying its log. Give it about 4 minutes.
- **The notebook can't connect to the kernel:** in VS Code, run **Developer: Reload Window**, then pick **Jupyter Kernel… → Python (ch09 Langfuse v5)**. Don't use "Python Environments": VS Code doesn't always discover a venv inside `ch09/`.
- **Native Langfuse alerts/webhooks (modules 12, 14) show ⚠️:** the server's SSRF guard blocks webhooks to your own machine. To allow it, add `LANGFUSE_WEBHOOK_WHITELISTED_HOST: host.docker.internal` to the `&langfuse-worker-env` block of the compose file and run `up -d` again.

## Running on one GPT-5-family model (e.g. gpt-5.4)
The course was built and validated with two deployments: `gpt-4.1-mini` as the agent and `gpt-5.2-chat` as the judge.

**What works with one model:**
- **The agent runs on a single GPT-5-family deployment.** GPT-5 and o-series models reject `temperature=0` and `max_tokens`, so the lab automatically sends no temperature and uses `max_completion_tokens` instead.
- **This was checked live** with gpt-5.2-chat as the only model.
- **How the model is detected:** from the deployment name (`gpt-5…`, `o1/o3/o4…`). If your deployment name doesn't start with the model name, add `LLM_TEMPERATURE=off` to `.env`.
- **Modules 00–02 and 06** need nothing else.

**Still being checked:** later modules that deliberately compare two models, e.g. module 03's "expensive model" cost spike, and the server-side judge in module 07 are being reviewed for single-model use.
