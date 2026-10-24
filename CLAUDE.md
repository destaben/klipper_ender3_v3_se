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

### Virtual printer boot check (if you touched `config/` or `sim/`)
`.github/workflows/sim-boot.yml` boots `config/` in a virtual printer and
fails if Klippy does not reach `ready`. Run the same check locally:
```bash
docker build -f sim/Dockerfile-sim -t klipper-sim . && \
docker run --rm -v "$PWD/config:/printer-config:ro" klipper-sim
```
It does not cover what the sim skips, which the run prints as `sim: not
loaded, ...` (currently two MCU temperature sensors). Heating and motion are
not simulated.

If any of these fail, fix the issue — even in a file your change didn't
touch — before pushing.
