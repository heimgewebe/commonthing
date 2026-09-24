# commonThing – Python Tools

## Schnellstart

```bash
uv sync --project tools/py --locked
uv run --project tools/py --locked python -c "import pytest, yaml, sys; print(pytest.__version__, yaml.__version__, sys.version_info[:2])"
```

Repo-kanonische Aufrufe (Make, Just, CI) lauten immer:

```bash
uv run --project tools/py --locked python -m scripts.agent.validate_agent_tooling_lock
make agent-contract-check   # gleiche Semantik
make validate               # vollständiger Python-Validierungspfad über denselben Pfad
```

Der vollständige `make validate`-Pythonpfad (inkl. `scripts/ci/tests` und dem
expliziten Pytest-Lauf) nutzt ausschließlich `uv run --project tools/py --locked`.
Bare Host-`python3` ist dort unzulässig; CI-Workflows, die `make validate` fahren,
installieren keine parallelen Host-Pins für pytest/PyYAML.

## Abhängigkeiten hinzufügen

```bash
uv add <paket>
```

Gebundene Tooling-Abhängigkeiten:

- `PyYAML==6.0.2`
- `pytest==9.0.3`

Das Lockfile bindet die zugelassenen Distributionsartefakte an SHA-256-Hashes.
Änderungen werden mit der in `toolchain.versions.yml` gepinnten uv-Version
erzeugt und anschließend mit `uv sync --locked` geprüft.
