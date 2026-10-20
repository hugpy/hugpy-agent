# Project Overview

## Purpose
The project is a portable agent runtime that utilizes the [hugpy] self-hosted LLM fleet as its inference brain. It includes features such as a gateway + tool-call adapter, an assess→act→observe loop with crash-safe SQLite journaling, workspace-jailed tools, markdown memory, and a CLI. The project is designed to be lightweight, requiring Python ≥ 3.10 and no external dependencies.

## Key Features
- **Gateway + Tool-call Adapter**: Facilitates interaction with the LLM fleet.
- **Assess→Act→Observe Loop**: Ensures a structured approach to task execution with crash-safe journaling.
- **Workspace-Jailed Tools**: Provides a secure environment for tool execution.
- **Markdown Memory**: Stores facts in markdown format for easy retrieval and indexing.
- **CLI Interface**: Offers command-line tools for running tasks, chatting, and managing the agent.

## Evaluation Results
The project includes evaluation results comparing two models: `ponpoke/flux2-klein-9b-uncensored-text-encoder` and `Qwen/Qwen3-Coder-Next-GGUF`. The results show that:
- **Reliability**: Qwen3-Coder-Next passed all 4 tasks, while flux2-klein failed one task due to a tool-call discipline issue.
- **Latency**: flux2-klein is significantly faster (4–5×) in wall time compared to Qwen3-Coder-Next.
- **Steps/Tokens**: Both models are comparable in terms of steps and tokens used, with Qwen3-Coder-Next using slightly fewer steps on average.

## Conclusion
The project provides a robust framework for running agents with the LLM fleet. The evaluation results highlight a trade-off between speed and reliability, with flux2-klein being faster but less reliable, while Qwen3-Coder-Next is more reliable but slower. The choice of model depends on the specific requirements of the task at hand.

## Additional Information
- **Installation**: The project can be installed using `python3 -m venv .venv && . .venv/bin/activate` followed by `pip install -e .`
- **Configuration**: Precedence is given to environment variables over `.env` files and `agent.toml` configurations.
- **Usage**: The project includes commands for running tasks, chatting, and managing the agent.

## Tools Available
The project provides a variety of tools for interacting with the LLM fleet, including:
- Local tools: `fs_read`, `fs_write`, `fs_glob`, `shell`, `http_fetch`
- Fleet text ML tools: `summarize`, `keywords`, `embed`, `similarity`
- Fleet file ML tools: `transcribe`, `classify`, `detect`, `segment`, `depth`, `vision`
- Fleet generation tools: `generate_image`, `generate_scene`
- Meta tools: `models_list`, `remember`, `final_answer`