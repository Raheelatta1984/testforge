# IDs without clashes: `next_id()` (64-bit, 41-13-10)

UUIDs never sort by creation time and random IDs collide under sharded writers. `app/ids.py` mints Snowflake-style 64-bit IDs:

```
 41 bits │ 13 bits │ 10 bits
 millis  │ shard   │ counter
 since   │ ref     │ per ms
 epoch   │ 0..8191 │ 0..1023
```

* **Time-sortable**: IDs sort in roughly chronological order, so they double as cursor pagination tokens and need no secondary `created_at` index for "latest N".
* **No clashes**: each process gets a unique shard reference (`TF_SHARD_ID`); two servers can never mint the same ID.
* **Throughput**: 1024 IDs per shard per millisecond (~1M/s/shard); beyond that the generator waits for the next millisecond instead of wrapping.
* **Clock-safe**: an NTP step backwards makes the mint wait until the clock catches up (refusing outright past 5s) rather than minting out-of-order IDs.

## Usage

```python
from app.ids import next_id, next_id_str, decode
run_id = next_id()              # int, e.g. 800512930883665920
cursor = next_id_str()          # str form for JSON / text columns
decode(run_id)
# {"timestamp_ms": ..., "datetime": ..., "shard_id": 3, "sequence": 0, ...}
```

Batch mint: `NextIdGenerator(shard_id=3).next_ids(1000)` (strictly increasing).

## Configuration

```bash
export TF_SHARD_ID=3            # unique per writer process/host (0..8191)
export TF_ID_EPOCH_MS=1704067200000   # custom epoch; default 2024-01-01T00:00:00Z
```

41 bits of milliseconds = ~69 years from the epoch. The value fits a Postgres `BIGINT` until ~2058 with the default epoch (the 64th bit is the sign bit); before then, either widen to `NUMERIC`/text or roll to a newer epoch for new rows.

## Adopting it

Existing tables keep their UUID primary keys -- nothing migrates by itself. New tables (or new public/cursor columns) should use `BIGINT DEFAULT <app-side next_id()>`:

```sql
ALTER TABLE runs ADD COLUMN public_id BIGINT;
-- backfill once, then NOT NULL + UNIQUE in a follow-up migration
```

Verify sort order in tests with `app.ids.is_sorted(ids)`.
