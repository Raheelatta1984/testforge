# Image storage: logical shards + machine map

Screenshots and evidence images grow without bound and must survive capacity moves without re-hashing. The design (`app/image_shards.py`):

* **Logical shards**: every image key hashes (`sha256 mod TF_IMAGE_SHARDS`, default 256) to a stable shard id. The hash never changes.
* **Machine map**: a JSON table assigns each shard *range* to a physical machine/volume/bucket. Adding capacity = moving ranges to a new machine and editing the map; only moved ranges relocate.

## Configuration

```bash
export TF_IMAGE_SHARDS=256
export TF_IMAGE_SHARD_MAP='{"0-127": "img-01:/data/img", "128-255": "img-02:/data/img"}'
# ...or keep the JSON in a file:
export TF_IMAGE_SHARD_MAP_FILE=/etc/testforge/image-map.json
export TF_IMAGE_ROOT=/app/artifacts/images   # local driver root
```

With no map configured, everything lands on machine `local` under `<TF_ARTIFACTS>/images/local/shard-NNNN/`, so a single instance works with zero setup. Map entries are `name` or `name:path`; absolute paths are honoured as mount points, relative ones hang under `TF_IMAGE_ROOT/<name>/`.

Every shard 0..N-1 must be covered exactly once; gaps and overlaps raise `ValueError` at startup (visible in `GET /api/scaling` as `images.error`).

## Usage

```python
from app.image_shards import default_store
store = default_store()
record = store.save(key=f"{run_id}:{step_index}", filename="step-3.jpg", data=jpeg_bytes)
# record -> {"shard": 41, "machine": "img-01:/data/img",
#            "path": "/data/img/shard-0041/step-3.jpg",
#            "url": "/api/images/0041/img-01/step-3.jpg", "bytes": 12345}
data = store.load(key, "step-3.jpg")
```

The shard derives from the key alone, so a file is locatable without a database lookup. Filenames are sanitised and writes are atomic (temp file + `os.replace`).

## Growing the fleet

1. Bring up `img-03`, rsync (or bucket-copy) `shard-0128..shard-0191` to it.
2. Change the map: `{"0-127": "img-01:...", "128-191": "img-03:...", "192-255": "img-02:...}`.
3. Reload the app. No re-hash, no key rewrite.

`app/images.py` re-exports the sharding API (`shard_for`, `MachineMap`, `ImageShardStore`, `default_store`) next to `compact_png`, so call sites import from either module.
