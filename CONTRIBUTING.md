# Contributing

Thanks for your interest in django-tasks-redis.

Issues and pull requests are welcome at
<https://github.com/tokibito/django-tasks-redis>. Everything that lands in
the repository — code, comments, docstrings, commit messages, pull request
descriptions and documentation — is written in English.

## Getting set up

Python 3.12 or newer, Django 6.0 or newer, and a Redis or Valkey server.

```bash
git clone https://github.com/tokibito/django-tasks-redis.git
cd django-tasks-redis

python -m venv venv
venv/bin/pip install -e ".[dev]"
```

The tests and the demo project both expect a server on `localhost:6379`. The
quickest way to get one is a container:

```bash
docker run -d --rm --name dtr-redis -p 6379:6379 redis:7
# or
docker run -d --rm --name dtr-valkey -p 6379:6379 valkey/valkey:7.2
```

## Running the tests

```bash
venv/bin/pytest
```

Tests use pytest-django with the settings in `tests/settings.py`: an
in-memory SQLite database for the admin and auth tables, and the Redis
server on `localhost:6379`, database 0. **There is no mock.** Every test that
touches a task goes through a real server, because streams, consumer groups,
pending-entry claiming and key expiry are what the backend is made of, and a
stand-in would only prove that the stand-in works.

Test keys carry the prefix `django_tasks_test` and the `clean_redis` fixture
deletes them before and after each test, so a shared development server is
fine. Data under other prefixes is left alone.

A full run reports **no skipped tests**. If the run fails before a test
starts, the server is usually not up: `redis-cli ping` (or `valkey-cli ping`)
should answer `PONG`.

To run part of the suite:

```bash
venv/bin/pytest tests/test_backend.py
venv/bin/pytest tests/test_views.py
venv/bin/pytest -k "retry"
```

### Against Valkey

The same suite runs unchanged against Valkey; CI has a job for it. Point
`localhost:6379` at a Valkey server instead of Redis and run `pytest` as
above. A change to anything that inspects the server — `INFO`, version
checks, command availability — is worth running against both locally, since
that is exactly where the two can differ.

## Linting and formatting

```bash
venv/bin/ruff check .
venv/bin/ruff format .
```

Both have to pass; CI runs `ruff format --check`, which fails on unformatted
code rather than fixing it. CI installs a specific Ruff version (see
`.github/workflows/ci.yml`), so if a formatting failure only shows up there,
install that version locally and run it again.

CI also runs `msgfmt --check` over every `.po` file, so a broken catalogue
fails the lint job rather than the release.

## Making a change

A few things are easy to forget:

- **Translations.** User-visible strings in `models.py`, `admin.py`,
  `apps.py` and the management commands go through `gettext_lazy`, and there
  is a Japanese catalogue. Adding a string means updating it, from inside the
  app directory:

  ```bash
  cd django_tasks_redis
  PYTHONPATH=.. ../venv/bin/python -m django makemessages -l ja --settings tests.settings
  # fill in the new msgstr, then check it compiles
  PYTHONPATH=.. ../venv/bin/python -m django compilemessages --settings tests.settings
  ```

  Commit `django.po` only. The `.mo` file is not in the repository; the
  publish workflow compiles it when the package is built, and the tests do not
  need it. `makemessages` may also rewrite the `#:` source references; that is
  only bookkeeping.

- **The changelog.** Add a line to the *Unreleased* section of
  `CHANGELOG.md`, under *Added*, *Changed*, *Deprecated*, *Removed*, *Fixed*
  or *Documentation*, saying what a user of the package will notice. Leave
  `version` in `pyproject.toml` and `__version__` in
  `django_tasks_redis/__init__.py` alone; releases are cut separately, and
  the publish workflow checks that both match the release tag.

- **The README.** New settings, commands, endpoints and admin behaviour are
  documented there. Its diagrams are mermaid, rendered by GitHub. The demo
  project has its own walkthrough in `examples/README.md`; a change to a
  command or an option usually shows up in both.

- **Keys in Redis.** The key layout under `REDIS_KEY_PREFIX` is what a
  running deployment has in its server. A change to a key name, a field in a
  hash or the shape of a stream entry needs a path for tasks that were
  enqueued by the previous version and are still waiting when the workers are
  upgraded.

- **Migrations.** `RedisTask` is an unmanaged pseudo-model with no table; it
  exists so the admin can register against it and Django creates its
  permissions. Changing it does not need a migration, but check that the
  admin and the permission names still line up.

### Backwards compatibility

Projects depend on the settings in `OPTIONS`, the URLs, the management
commands and the Redis key layout, so a change to any of them needs a path
that keeps working for someone who upgrades without reading the release
notes. Where something has to go, deprecate it first: keep it working with a
`DeprecationWarning` that names the version it is removed in.

The tests are the safety net for this. When a refactor leaves the existing
tests passing unmodified, that is the evidence that behaviour did not change.

## Trying it against a real service

The demo project in `examples/` is the place to run a change end to end
against something other than the test suite: a development server, the admin,
the HTTP endpoints and a `run_redis_tasks` worker, all against the server on
`localhost:6379`. `examples/README.md` walks through it.

## Extending the backend

`RedisTaskBackend` is designed to be subclassed rather than configured for
everything. The HTTP endpoints are the example that ships: they run and
delete tasks, so they stay closed until a subclass overrides
`get_auth_handler()` and says how a request is authenticated.
`tests/backends.py` has the smallest complete version, a shared-secret header.

A project-specific behaviour — a different authentication scheme, a hook
around task execution, another key layout — usually belongs in such a
subclass in the project, configured by its dotted path in `TASKS`, and needs
nothing from a release here. Open an issue before adding a new option or
integration to this repository. Anything bundled has to keep working for as
long as the package exists, and the surface has been kept small on purpose.

## Pull requests

- Branch off `main`.
- **One purpose per pull request.** See below.
- Say what the change is for. A description that explains the problem is worth
  more than one that restates the diff.
- Keep the tests and the linters green. CI runs the suite against Python 3.12
  to 3.14 and Django 6.0 and 6.1 on Redis, and once more on Valkey.
- Add tests for a behaviour change. A bug fix wants the test that fails
  without it.

### Split by purpose

A pull request should answer one question, so that a reviewer can hold that
question in their head while reading all of it. Several purposes in one branch
means none of them gets read properly, and the risky part hides among the safe
parts.

Split it when a branch contains any of these:

- **A refactor and the feature it makes possible.** Send the refactor first,
  where the existing tests passing unmodified is the whole argument, and build
  on it afterwards. Mixed together, that evidence is gone.
- **A fix for something you noticed on the way.** Worth having, and worth
  having on its own, where it can be reviewed and reverted by itself.
- **Mechanical churn beside a real change.** Reformatting, renaming, tidying
  imports and dependency bumps drown the few lines that matter.
- **Steps that make sense in sequence.** A change to the key layout, the
  worker that reads it, the command option that exposes it and the
  documentation can each be a pull request, each mergeable and reviewable on
  its own.

This holds however the change was produced, including with the help of an AI
tool. A branch that grew in one sitting still has to arrive as the sequence of
changes a reviewer can follow; splitting it afterwards is part of the work, not
an optional tidy-up.

A change that genuinely is one purpose can still be large, and that is fine.
The test is whether the description needs the word "and".
