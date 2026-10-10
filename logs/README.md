# Harness logs

This is the TestForge test-harness log folder. Every run of `python -m tests.harness` writes a timestamped directory here and appends a row to `index.md`.

```
logs/
  index.md                  history of every committed run
  YYYYMMDD-HHMMSS/
    harness.log             combined log, one timestamped line per event
    unit.log                unittest stdout and stderr
    scenarios.log           library scenario steps and assertions
    server.log              uvicorn log for the library run
    results.json            machine-readable pass/fail with durations
    results.md              human-readable report
```

Databases, screenshots, and browser artifacts stay in a temporary directory. Only these text logs are stored in the repository.

Run the harness from the repository root:

```bash
python -m tests.harness
```

Scenario definitions live in `tests/SCENARIOS.md` and `tests/scenarios.json`.
