# NEON Nerdsnipe Agent

Python agent that connects to NEON via WebSocket and completes the authentication sequence.

## How it works
- Reconstructs prompts from timestamped fragments (sort by timestamp, join words).
- Responds with strict JSON objects (`enter_digits` or `speak_text`).
- Evaluates JavaScript-style arithmetic including `Math.floor(...)` and `%`.
- Fetches Wikipedia REST summary and extracts the Nth word.
- Uses resume content for crew manifest prompts and tracks prior outputs for verification.

## Run
```bash
pip install websockets requests
python neon_agent.py
```
