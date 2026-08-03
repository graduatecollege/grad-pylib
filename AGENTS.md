# AGENTS

This is a shared FastAPI utilities project for the Graduate College.

## Instructions

**Modern Python 3.14 is used**. Futures imports are not needed. Use built-in type hints when possible.

**If you need to run python** or **install dependencies**, use `uv`.

**Tests are co-located with the code using a `test_` prefix**.

**FastAPI concurrency**: Important! Endpoints and dependencies must be synchronous, FastAPI automatically 
handles concurrency. Middleware must be async.

## Commands

```bash
uv sync                                                    # Install dependencies
uv run pytest                                                 # Run all tests
```
