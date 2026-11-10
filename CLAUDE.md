# CLAUDE.md

Project-specific instructions for Claude Code working in this repo.

## Before opening or updating a pull request

`.github/workflows/lint.yml` runs on every PR targeting `main` and lints the
**entire tracked tree**, not just the files the PR touches — a pre-existing
issue anywhere in the repo fails every PR's checks until someone fixes it,
even a PR that never touched the offending file. Run the same checks locally
before pushing a branch you intend to open or update a PR for, so CI doesn't
surprise you with something unrelated to your change.

Scope every check to **tracked files only** (`git ls-files`). The working
tree on the actual printer also contains vendored/gitignored third-party
tools (`chopper-resonance-tuner/`, `KlipperMaintenance/`, `moonraker-timelapse/`)
that CI never sees and that would otherwise produce noise.

Only run this before a PR, not after every individual edit.

### ShellCheck (all tracked `.sh` files)
```bash
git ls-files '*.sh' | xargs docker run --rm -v "$PWD:/mnt" -w /mnt koalaman/shellcheck:stable
```

### Hadolint (all Dockerfiles)
The CI action runs with `failure-threshold: error`, so info-level findings
(e.g. `DL3066` on a non-numeric `USER`) don't fail the job — pass the same
threshold locally or you'll chase findings CI doesn't actually care about:
```bash
docker run --rm -i hadolint/hadolint hadolint --failure-threshold error - < Dockerfile-klipper
docker run --rm -i hadolint/hadolint hadolint --failure-threshold error - < Dockerfile-moonraker
docker run --rm -i hadolint/hadolint hadolint --failure-threshold error - < Dockerfile-klipper-connect
docker run --rm -i hadolint/hadolint hadolint --failure-threshold error - < Dockerfile-mmu-fallback
docker run --rm -i hadolint/hadolint hadolint --failure-threshold error - < sim/Dockerfile-sim
```

### docker compose config validation
```bash
docker compose config --quiet
```

### yamllint (tracked YAML files, same ruleset as CI)
The ruleset below is copied from the `yamllint` job's `config_data` in
`.github/workflows/lint.yml` — if that job's config changes, update this too.
It relaxes a few yamllint defaults: no required `---` document start,
150-char line length, and `on`/`off` accepted as booleans. Reproduce it
locally, scoped to tracked files like every other check here:
```bash
docker run --rm -v "$PWD:/data" -w /data cytopia/yamllint \
  -d "extends: default
rules:
  document-start: disable
  line-length:
    max: 150
  truthy:
    allowed-values: ['true', 'false', 'on', 'off']" $(git ls-files '*.yml' '*.yaml')
```

### Virtual printer boot check (if you touched `config/`, `sim/`, `mmu-fallback/` or the sim-boot workflow)
`.github/workflows/sim-boot.yml` boots `config/` in a virtual printer and
fails if Klippy does not reach `ready`. Run the same check locally:
```bash
docker build -f sim/Dockerfile-sim -t klipper-sim . && \
docker run --rm -v "$PWD/config:/printer-config:ro" -e SIM_READY_TIMEOUT=180 klipper-sim
```
It does not cover what the sim skips, which the run prints as `sim: not
loaded, ...` (currently two MCU temperature sensors). Heating and motion are
not simulated. The run also executes `sim/smoke.py`, which checks that the
virtual-MMU guards refuse lane changes.

### Watcher unit tests (if you touched `mmu-fallback/`)
Runs `mmu-fallback/test_mmu_link.py` (also run by sim-boot in CI). Standard library only, no Docker needed:
```bash
python3 -m unittest discover -s mmu-fallback -v
```

### Print-rescue unit tests (if you touched `print-rescue/` or `config/`)
Runs `print-rescue/test_*.py` (also run by `.github/workflows/print-rescue-test.yml`). Standard library only, no Docker needed; on the Pi use `nice -n 19` while Klipper is connected:
```bash
python3 -m unittest discover -s print-rescue -v
```

### RPi metrics tests (if you touched `scripts/`)
Runs `scripts/test_rpi_metrics.py` (also run by `.github/workflows/rpi-metrics-test.yml`). Standard library only; set `RPI_PROMTOOL` to also run `promtool check metrics`:
```bash
RPI_PROMTOOL="docker run --rm -i --entrypoint promtool prom/prometheus:v2.55.1" python3 -m unittest discover -s scripts -v
```

### Klipper Connect patch tests (if you touched `patches/klipper-connect/` or `Dockerfile-klipper-connect`)
`patches/klipper-connect/test_queue_start.py` covers the local patch and runs in
`.github/workflows/klipper-connect-test.yml`. The build and run commands are in `docs/prusa-connect.md`, "Upgrading the
pinned commit", step 4.

If any of these fail, fix the issue — even in a file your change didn't
touch — before pushing.
