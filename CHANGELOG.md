# Changelog

## 0.2.0

**The HTTP task endpoints are closed by default.** A project that uses them
has to override `get_auth_handler()` before upgrading, or every call answers
`403`. Nothing else needs a change, apart from one `migrate` run for the admin
permissions.

**Tasks are now delivered at least once.** A worker that dies leaves its
message pending; another worker reclaims it after `REDIS_CLAIM_TIMEOUT` and
runs the task again, which before this release never happened. Two things
follow: `REDIS_CLAIM_TIMEOUT` (default 300 seconds) must be longer than the
longest task the workers run, or a task still running is executed a second
time, and task functions should be idempotent, since a worker can die
between finishing the work and recording the result.

### Added

- **`REDIS_SOCKET_CONNECT_TIMEOUT`**, **`REDIS_HEALTH_CHECK_INTERVAL`**,
  **`REDIS_SOCKET_TIMEOUT`** and **`REDIS_SOCKET_KEEPALIVE`**, passed
  through to redis-py from both a `REDIS_URL` and individual parameters.
  Without them a Redis restart or a network partition left a worker blocked
  on a half-open socket for as long as the kernel allowed. The connect
  timeout defaults to 5 seconds and the health check interval to 30; set an
  option to `None` to drop its default. `REDIS_SOCKET_TIMEOUT` has no default
  because it also bounds the worker's blocking read, so a value below
  `REDIS_BLOCK_TIMEOUT` makes every fetch raise.
  ([#11](https://github.com/tokibito/django-tasks-redis/pull/11))
- **`REDIS_MAX_DELIVERIES`** (default 5). A task that has been started this
  many times without finishing is given up on the next time its message is
  reclaimed: it is marked FAILED with a `TaskAbandoned` error and shows up in
  the admin instead of being retried forever. Only starts count, not
  deliveries, and a task that already finished keeps its result. `0`
  disables the cap.
  ([#9](https://github.com/tokibito/django-tasks-redis/pull/9),
  [#16](https://github.com/tokibito/django-tasks-redis/pull/16))
- **`REDIS_SCAN_BATCH_SIZE`** (default 500), the number of tasks read per
  round trip when the admin, the statistics and the purge walk the results
  index. `purge_completed_redis_tasks --batch-size` was accepted and ignored
  before; it now overrides this for one run, and applies the setting when it
  is not given.
  ([#9](https://github.com/tokibito/django-tasks-redis/pull/9),
  [#20](https://github.com/tokibito/django-tasks-redis/pull/20))
- **`REDIS_SSL_CA_CERTS`**, the path of a CA certificate to verify the
  server with, for a Redis or Valkey behind TLS with a self-signed or private
  CA. It takes effect with `REDIS_SSL=True` or a `rediss://` URL; set without
  either, it is ignored with a warning in the log rather than silently.
  ([#1](https://github.com/tokibito/django-tasks-redis/pull/1))
- **Admin permissions that can be granted.** `RedisTask` moved from
  `admin.py` to `models.py`, so Django now creates its `ContentType` and
  `Permission` rows. Previously no such rows existed and the admin was, in
  practice, superuser-only. The permissions reflect what the admin can do:
  `view_redistask` for the list and detail pages, `delete_redistask` for
  deleting, and a new `run_redistask` for running and retrying. There is no
  table to add to or edit, so no `add` or `change` permission exists.
  Existing installations need one `migrate` run for the rows to appear.
- Searching the admin task list by id. The search box was rendered before but
  did nothing. ([#7](https://github.com/tokibito/django-tasks-redis/pull/7))

### Changed

- **A continuous worker waits on the streams** for up to
  `REDIS_BLOCK_TIMEOUT` instead of sleeping between polls. The setting was
  parsed and never used. `--interval` now only applies when the wait is
  disabled with `REDIS_BLOCK_TIMEOUT: 0`; the block timeout also bounds how
  long a shutdown signal takes to be noticed.
  ([#9](https://github.com/tokibito/django-tasks-redis/pull/9))
- **Acknowledged stream entries are deleted.** The worker only ever sent
  `XACK`, so each priority stream grew by one entry per task forever.
  ([#9](https://github.com/tokibito/django-tasks-redis/pull/9))
- **`purge_completed_tasks()` and `purge_completed_redis_tasks` refuse a
  negative `days`.** A negative age put the cutoff in the future, matched
  every completed task and deleted the whole history. `--dry-run` no longer
  writes anything, not even index housekeeping.
  ([#9](https://github.com/tokibito/django-tasks-redis/pull/9))
- **A connection error is no longer mistaken for an empty queue.** The
  fetch and the stale-message sweep caught every exception where they meant
  to tolerate a stream that did not exist yet, so a Redis that had gone away
  or refused authentication looked like a worker with nothing to do. Only
  `NOGROUP` is tolerated now.
  ([#9](https://github.com/tokibito/django-tasks-redis/pull/9))
- **The HTTP task endpoints answer `403` until authenticated.**
  `get_auth_handler()` was documented as the authentication hook, but nothing
  called it, so `/tasks/run/`, `/tasks/execute/<id>/`, `/tasks/status/<id>/`
  and `/tasks/purge/` accepted every request: anyone who could reach them
  could run every queued task, read a task's arguments, return value and
  tracebacks, or delete the task history. The hook now runs before each
  view, and a backend whose `get_auth_handler()` returns `None` (the default)
  keeps its endpoints closed. Override it to open them; the README shows a
  shared-secret handler. A request naming an unknown backend gets `400`
  instead of raising, and a non-numeric `max_tasks` or `days` gets `400`
  instead of a 500.
  ([#8](https://github.com/tokibito/django-tasks-redis/pull/8))
- The admin actions declare the permissions they need: running and
  retrying want `run_redistask`, deleting wants `delete_redistask`. Before,
  all three were offered to anyone who could open the changelist, and
  `has_delete_permission()` returned `True` without looking at the user.
  ([#7](https://github.com/tokibito/django-tasks-redis/pull/7))
- The admin task detail page checks `view_redistask`. It was reachable by
  any staff account by URL, with the task's arguments, result and
  tracebacks. ([#7](https://github.com/tokibito/django-tasks-redis/pull/7))
- The admin detail page follows the admin's dark mode. Its colours were
  hard-coded for the light theme and now come from Django's admin CSS
  variables. ([#12](https://github.com/tokibito/django-tasks-redis/pull/12))

### Fixed

- **A delayed task could be queued more than once.** Promotion from the
  delayed set ran `ZRANGEBYSCORE`, `HGETALL`, `XADD`, `ZREM` with nothing
  serialising it, on every worker on every fetch, so two idle workers
  reading the same due task both queued it, and a task that re-enqueues
  itself with `run_after` fanned out exponentially. Promotion is one atomic
  script in which `ZREM` is the claim.
  ([#9](https://github.com/tokibito/django-tasks-redis/pull/9))
- **A task whose worker died was never run again.** Stale messages were
  reclaimed for a random consumer id nobody read from, and the fetch only
  ever asked for new messages, so a reclaimed message bounced between
  phantom consumers forever. Messages are reclaimed for the sweeping
  worker's own consumer, which serves its own pending messages before new
  ones, and a task the dead worker left RUNNING is handed back as READY when
  its message is reclaimed.
  ([#9](https://github.com/tokibito/django-tasks-redis/pull/9))
- Recovery looked at only the first 100 pending entries of each stream.
  ([#9](https://github.com/tokibito/django-tasks-redis/pull/9))
- A task fetched before its `run_after` was acknowledged and dropped. It
  goes back to the delayed set.
  ([#9](https://github.com/tokibito/django-tasks-redis/pull/9))
- A task delayed further out than `REDIS_RESULT_TTL` lost its data before it
  was due. The result now outlives the delay.
  ([#9](https://github.com/tokibito/django-tasks-redis/pull/9))
- `run_task_by_id()` read the status and then wrote it, so an external
  trigger delivered twice ran the task twice. The claim is atomic, and a
  retry of a FAILED task keeps its error history instead of erasing it.
  ([#9](https://github.com/tokibito/django-tasks-redis/pull/9))
- A `task_path` that can no longer be imported left the task RUNNING with
  no error and took a `--continuous` worker down. It is recorded as FAILED
  and the worker carries on.
  ([#9](https://github.com/tokibito/django-tasks-redis/pull/9))
- `deserialize_datetime()` returned whatever the stored string carried, so a
  value written under a different `USE_TZ` raised `TypeError: can't compare
  offset-naive and offset-aware datetimes` inside the worker. Naive and
  aware values are normalised to the reader's setting, preserving the
  instant. ([#11](https://github.com/tokibito/django-tasks-redis/pull/11))
- Enqueue writes the task hash, the index entry and the queue entry in one
  transaction, so a task can no longer be stored but never queued.
  ([#9](https://github.com/tokibito/django-tasks-redis/pull/9))
- The admin task list, the statistics and the purge walked the results index
  with one round trip per task. They use `SSCAN` and pipelined `HGETALL`.
  ([#9](https://github.com/tokibito/django-tasks-redis/pull/9))
- **The admin templates and translations are in the package.** Neither the
  wheel nor the sdist contained `templates/` or `locale/`, so on any install
  that was not a source checkout, clicking a task in the admin raised
  `TemplateDoesNotExist`. The release build now compiles the translation
  catalogue too, since gettext reads `.mo` at runtime and only `.po` is in
  the repository. ([#6](https://github.com/tokibito/django-tasks-redis/pull/6))
- Admin pagination was off by one page: the page number was read as
  0-indexed while Django generates 1-indexed links, so each page showed the
  next page's rows and the last page was empty. `?p=abc` returned a 500 and
  now falls back to the first page.
  ([#7](https://github.com/tokibito/django-tasks-redis/pull/7))
- Django's built-in `delete_selected` action is no longer offered in the
  admin. It deleted from a queryset that is always empty, so it reported
  success while removing nothing. *Delete selected tasks* is the action that
  removes tasks from Redis.
  ([#7](https://github.com/tokibito/django-tasks-redis/pull/7))
- `get_actions()` accepts the `action_location` argument Django 6.1 passes,
  so a changelist request no longer emits a `RemovedInDjango70Warning`, and
  actions on the change form keep working. Django 6.0 is unaffected.
  ([#10](https://github.com/tokibito/django-tasks-redis/pull/10))
- Building from source needs setuptools 77 or newer, which is what the
  license metadata in `pyproject.toml` already required; the declared floor
  was too low to build the project at all.

### Documentation

- `REDIS_RESULT_TTL` is documented with its real default of 30 days, not 7,
  and `REDIS_COMPLETED_TASK_TTL`, which existed but was not listed, is
  documented. ([#6](https://github.com/tokibito/django-tasks-redis/pull/6))
- `run_redis_tasks --workers` is gone from the README. The command never
  defined it. Run more processes to scale.
  ([#6](https://github.com/tokibito/django-tasks-redis/pull/6))
- A *Permissions* table for the admin, and a section on opening the HTTP
  endpoints. ([#8](https://github.com/tokibito/django-tasks-redis/pull/8))
- A *Delivery guarantees* section on what at-least-once means for
  `REDIS_CLAIM_TIMEOUT` and task design, and a *Connection robustness*
  section on the new connection options, including that redis-py's default
  retry policy multiplies the connect timeout.
  ([#9](https://github.com/tokibito/django-tasks-redis/pull/9),
  [#11](https://github.com/tokibito/django-tasks-redis/pull/11),
  [#16](https://github.com/tokibito/django-tasks-redis/pull/16))
- `CONTRIBUTING.md`, covering the development setup, running the tests
  against Redis and Valkey, and what a pull request needs. The test suite
  reads `REDIS_URL` from the environment to run against another server.
  ([#15](https://github.com/tokibito/django-tasks-redis/pull/15))
- This file. ([#15](https://github.com/tokibito/django-tasks-redis/pull/15))

## 0.1.0

Initial release: a task backend for Django 6.0's task framework on Redis
Streams, with `run_redis_tasks` and `purge_completed_redis_tasks`, a Django
admin for the tasks, HTTP endpoints for external schedulers, and TLS through
`REDIS_SSL` or a `rediss://` URL.
