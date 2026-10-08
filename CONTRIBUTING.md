# Contributing

Use Python 3.11–3.14, Node 22 or newer, and uv.

```bash
uv sync --frozen
npm ci --ignore-scripts
uv run --no-sync python scripts/verify.py
```

The checks use fake services and build in a temporary directory. Proxy tests run
the pinned Link CLI against signed callback fixtures, including login, refresh,
approval, private token handoff, denial, and failure recovery. Compiled graph tests
also exercise the real MDA callback handler and model tool restrictions. They do not use
a real wallet or change your deployment. Keep the SDK/CLI versions pinned, and
add regression coverage for changes to approval, ownership, renewal, or recovery.
Never place an actual order as an automated test.

After changing application source, run `scripts/setup.py --slack` again before
deploying. It preserves your existing settings and deployment metadata.
For a source-only archive:

```bash
uv run --no-sync python scripts/package_source.py
```

The archive excludes private settings, local history, builds, and dependencies.
Include LICENSE and the README's source attribution when sharing it.

Keep recordings, run exports, and private notes in `.local/`. Commit source,
tests, lockfiles, and reusable docs. Check `git diff --cached` before pushing;
ignore rules do not remove files that Git already tracks.

## Report a security issue

Use GitHub's private vulnerability reporting if enabled, or contact the repository
maintainers privately. Do not post credentials, payment tokens, delivery details,
or sensitive traces in a public issue. Maintainers should enable private reporting
before making the repository public.
