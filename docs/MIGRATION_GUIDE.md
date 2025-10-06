# Migration Guide: Legacy Context Calls to Snapshot Workflows

## Overview

`GameEngine.progress_stage` and `GameEngine.finalize_game` now expose
snapshot-based execution paths. The legacy signatures that accept
`context` and `game` objects are still available for backwards
compatibility, but they emit `DeprecationWarning` messages and will be
removed in a future major release.

Migrating to the new entry points keeps the critical section small,
moves all Telegram I/O outside the stage lock, and dramatically reduces
lock contention when several hands finish in parallel.

## What Changed?

### Deprecated Signatures

```python
# Legacy usage (deprecated)
await game_engine.progress_stage(
    context=context,
    chat_id=chat_id,
    game=game,
)

await game_engine.finalize_game(
    context=context,
    game=game,
    chat_id=chat_id,
)
```

### Preferred Snapshot Signatures

```python
# Snapshot usage (preferred)
await game_engine.progress_stage(chat_id=chat_id)

await game_engine.finalize_game(chat_id=chat_id)
```

## Why Switch?

- **Shorter lock holds** – winner evaluation, messaging, and statistics
  happen after the lock is released.
- **Type safety** – dependencies are accessed through dedicated
  protocols instead of dynamic attribute lookups.
- **Cleaner integrations** – there is a single code path to exercise in
  tests and during production incidents.

## Migration Checklist

1. **Locate legacy calls**

   ```bash
   rg "progress_stage(.*context" -g"*.py"
   rg "finalize_game(.*context" -g"*.py"
   ```

2. **Remove `context` and `game` arguments**

   ```python
   # Before
   await game_engine.progress_stage(context=context, chat_id=chat_id, game=game)

   # After
   await game_engine.progress_stage(chat_id=chat_id)
   ```

3. **Trim unused variables** – eliminate `context` or `game`
   parameters that were only forwarded to the engine.

4. **Run the compatibility tests**

   ```bash
   pytest tests/test_game_engine_backward_compat.py -v
   ```

## Deprecation Timeline

| Version | Status            | Action Required                         |
|---------|-------------------|-----------------------------------------|
| 1.x     | Deprecated        | Switch to snapshot signatures           |
| 2.0     | Removal planned   | Legacy keyword arguments will error out |

## Additional Notes

- The new path always requires `chat_id`.
- `finalize_game` now returns `None`; callers should await it only for
  side effects.
- Legacy usage will continue to work during the deprecation window but
  logs a warning to simplify auditing.

## Statistics Schema Management

### Strategy Overview

- The ORM models defined in `pokerapp/database_schema.py` are the source
  of truth for the statistics subsystem schema.
- New deployments may rely on `Base.metadata.create_all()` to provision
  the schema automatically during application bootstrap.
- Existing deployments that previously executed the SQL files under
  `migrations/` should continue to manage upgrades through those
  migrations.

### Deployment Guidance

- **Fresh installs** – allow the application to create the schema via
  the ORM metadata. The raw SQL migrations do not need to be executed.
- **Upgrades** – run the SQL migration scripts manually before starting
  the new application version. The bootstrap process will detect the
  pre-existing tables and skip auto-creation.
- **Mixed environments** – avoid executing both the SQL migrations and
  the ORM bootstrap on the same database without coordination. If the
  migrations created part of the schema but the ORM bootstrap still
  detects missing tables, treat the situation as a failed deployment and
  investigate before proceeding.
