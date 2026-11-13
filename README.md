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
- creates `ch09/.venv-langfuse` with the exact pinned packages the course was validated with (`ch09/langfuse_lab/requirements.txt`, including the kernel-transport pins that keep VS Code from hanging on "Connecting to kernel");
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

**All 15 modules run on one deployment.** Set `AZURE_OPENAI_DEPLOYMENT` and `EVAL_JUDGE_MODEL` to the same name. The lab notices this (`Settings.single_model`) and adapts:
- **Parameters:** GPT-5 and o-series models reject `temperature=0` and `max_tokens`. The agent sends no temperature and uses `max_completion_tokens`, and module 04's verify expects exactly that.
- **Detection:** the model is recognized from the deployment name (`gpt-5…`, `o1/o3/o4…`). If your deployment name doesn't start with the model name, add `LLM_TEMPERATURE=off` to `.env`.
- **This was checked live** with gpt-5.2-chat as the only model, for the agent turns, module 03's cost spike, module 04's parameters, module 07's server-side judge, and the planted failures. About 34 Azure calls.

| Modules | On one GPT-5 deployment |
|---|---|
| 00, 01, 02, 05, 06, 11, 13 | work as written |
| 03 cost | **Single-model variant.** The planted spike is a prompt-cache-busting ~17k-token padding instead of "switch to the expensive model". The per-conversation budget scales with your model's price. v2 sends about 90k input tokens in under a minute, so a low tokens-per-minute quota may throttle it (the client retries). |
| 04 prompts | Works. The prompt config's temperature is never sent; verify accounts for that. Turn temperature off in the Playground. |
| 07, 08, 14 | Work. The judge is the agent's own model, so it can share its blind spots. Module 07's calibration step (judge vs code score) is how you find out. |
| 09, 10 | Work. GPT-5 can't run at temperature 0, so expect more "suspect" items and re-runs (a few extra calls). |
| 12 | Works. With more run-to-run variance, a cost or latency rule can fire on noise in a clean window; re-run with a new `RUN_TAG`. |

**Check the prices.** Module 03 uses list prices from `ch09/langfuse_lab/pricing.py`: gpt-5.4 at $2.50 input / $0.25 cached / $15.00 output per 1M tokens, which is **an assumption**. Check them against your Azure agreement. If your deployment name isn't in that table, module 03 stops with a clear message asking you to add a row.
