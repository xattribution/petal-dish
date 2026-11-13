# Working on PETAL Locate

## Setup

```bash
cd locate
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

The UI lives in `locate/ui` (`npm ci && npm run build` writes the production bundle to
`src/petal_locate/server/static/`).

## Rules for every change

1. `docs/MATH.md` fixes signs, units and conventions; `docs/ARCHITECTURE.md` fixes
   module boundaries and signatures; `docs/PROTOCOL.md` fixes the wire format. Code
   follows them. If one of them is wrong, fix the physics in code, add a test that
   proves it, and update the document in the same change.
2. `core/` stays pure: no I/O, no asyncio, no module-level state, explicit seeds.
3. Absolute time is integer nanoseconds. Float seconds are for orbit evaluation and
   for durations only.
4. Every numeric routine has a test against an independent calculation (closed form,
   brute force, or a different algorithm), not just a regression snapshot.
5. Nothing fails silently. Bad input raises `ValueError` naming the field; an
   estimate that cannot be made returns `None` with a reason.
6. Tests must be deterministic (fixed seeds) and finish quickly; mark anything over
   about 10 s with `@pytest.mark.slow`.
7. Simulated results are labelled as simulated everywhere they are shown.

## When several people (or agents) work at once

- Touch only the files you own for the task. If you need a change in someone else's
  module, write down exactly what and why instead of editing it.
- Do not run `git commit`, `git add`, `git stash` or anything that rewrites the tree;
  the integrator commits.
- Do not install packages globally. Python goes through `locate/.venv`; add a
  dependency to `pyproject.toml` only when it is needed and say so.
- Run only your own tests while others are mid-change (`pytest tests/test_<yours>.py`);
  the full suite is run at integration.
- Scratch files go under the session scratchpad, never in the repository.
