# Changelog

## Unreleased

**The HTTP task endpoints are closed by default.** A project that uses them
has to override `get_auth_handler()` before upgrading, or every call answers
`403`. Nothing else needs a change, apart from one `migrate` run for the admin
permissions.

### Added

- **`REDIS_SSL_CA_CERTS`**, the path of a CA certificate to verify the
  server with, for a Redis or Valkey behind TLS with a self-signed or private
  CA. It takes effect with `REDIS_SSL=True` or a `rediss://` URL; set without
  either, it is ignored with a warning in the log rather than silently.
- **Admin permissions that can be granted.** `RedisTask` moved from
  `admin.py` to `models.py`, so Django now creates its `ContentType` and
  `Permission` rows. Previously no such rows existed and the admin was, in
  practice, superuser-only. The permissions reflect what the admin can do:
  `view_redistask` for the list and detail pages, `delete_redistask` for
  deleting, and a new `run_redistask` for running and retrying. There is no
  table to add to or edit, so no `add` or `change` permission exists.
  Existing installations need one `migrate` run for the rows to appear.
- Searching the admin task list by id. The search box was rendered before but
  did nothing.

### Changed

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
- The admin actions declare the permissions they need: running and
  retrying want `run_redistask`, deleting wants `delete_redistask`. Before,
  all three were offered to anyone who could open the changelist, and
  `has_delete_permission()` returned `True` without looking at the user.
- The admin task detail page checks `view_redistask`. It was reachable by
  any staff account by URL, with the task's arguments, result and
  tracebacks.
- The admin detail page follows the admin's dark mode. Its colours were
  hard-coded for the light theme and now come from Django's admin CSS
  variables.

### Fixed

- **The admin templates and translations are in the package.** Neither the
  wheel nor the sdist contained `templates/` or `locale/`, so on any install
  that was not a source checkout, clicking a task in the admin raised
  `TemplateDoesNotExist`. The release build now compiles the translation
  catalogue too, since gettext reads `.mo` at runtime and only `.po` is in
  the repository.
- Admin pagination was off by one page: the page number was read as
  0-indexed while Django generates 1-indexed links, so each page showed the
  next page's rows and the last page was empty. `?p=abc` returned a 500 and
  now falls back to the first page.
- Django's built-in `delete_selected` action is no longer offered in the
  admin. It deleted from a queryset that is always empty, so it reported
  success while removing nothing. *Delete selected tasks* is the action that
  removes tasks from Redis.
- `get_actions()` accepts the `action_location` argument Django 6.1 passes,
  so a changelist request no longer emits a `RemovedInDjango70Warning`, and
  actions on the change form keep working. Django 6.0 is unaffected.
- Building from source needs setuptools 77 or newer, which is what the
  license metadata in `pyproject.toml` already required; the declared floor
  was too low to build the project at all.

### Documentation

- `REDIS_RESULT_TTL` is documented with its real default of 30 days, not 7,
  and `REDIS_COMPLETED_TASK_TTL`, which existed but was not listed, is
  documented.
- `run_redis_tasks --workers` is gone from the README. The command never
  defined it. Run more processes to scale.
- A *Permissions* table for the admin, and a section on opening the HTTP
  endpoints.
- `CONTRIBUTING.md`, covering the development setup, running the tests
  against Redis and Valkey, and what a pull request needs.
- This file.

## 0.1.0

Initial release: a task backend for Django 6.0's task framework on Redis
Streams, with `run_redis_tasks` and `purge_completed_redis_tasks`, a Django
admin for the tasks, HTTP endpoints for external schedulers, and TLS through
`REDIS_SSL` or a `rediss://` URL.
