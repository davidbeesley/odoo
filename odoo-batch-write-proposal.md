# Odoo ORM Batch Write Proposal

## Problem Statement

Odoo's ORM supports batch creation of records with different values per record, but does not support batch updates with different values per record. This creates a significant performance asymmetry when importing data.

| Operation | Different values per record? | Batch SQL? |
|-----------|------------------------------|------------|
| `create(vals_list)` | Yes | Yes |
| `write(vals)` | No (same vals for all) | Yes |
| `_load_records()` upsert | Creates: Yes, Updates: No | Creates only |

## Current Flow Analysis

### 1. Batch Create Flow

**Entry point:** `BaseModel.create(vals_list)` in `odoo/orm/models.py:4563`

```python
# models.py:4563-4718
def create(self, vals_list):
    # Once for batch
    self.check_access('create')

    # Once per unique field across all vals
    for field_name in field_names:
        self._check_field_access(field, 'write')

    # Batch: add defaults, filter magic fields
    new_vals_list = self._prepare_create_values(vals_list)

    # Handle _inherits parent records (batched)
    for model_name, parent_name in self._inherits.items():
        parents = self.env[model_name].create([...])

    # Batch INSERT
    records = self._create(data_list)

    # Protect computed fields during inverse
    with env.protecting(protected_fields):
        field.determine_inverse(inv_records)  # inverse methods
        inv_records.invalidate_recordset(...)  # cache cleanup

    # Per record validation
    for data in data_list:
        data['record']._validate_fields(...)

    # Once for batch
    if self._check_company_auto:
        records._check_company()

    return records
```

### 2. Single Write Flow

**Entry point:** `BaseModel.write(vals)` in `odoo/orm/models.py:4287`

```python
# models.py:4287-4471
def write(self, vals):
    # Once for recordset
    self.check_access('write')

    # Once per field in vals
    for field_name in vals:
        self._check_field_access(self._fields[field_name], 'write')

    # Filter magic fields, set defaults
    vals = {k: v for k, v in vals.items() if k not in bad_names}
    vals.setdefault('write_uid', self.env.uid)
    vals.setdefault('write_date', self.env.cr.now())

    # Classify fields
    field_values = []  # [(field, value), ...]
    determine_inverses = defaultdict(list)
    protected = set()

    # Force pending computes before write
    self._recompute_recordset(to_compute)

    with env.protecting(protected, self):
        # Track old dependencies
        self.modified(fnames_modifying_relations, before=True)

        # Per-field write - SAME value for all records
        for field, value in sorted(field_values, key=lambda x: x[0].write_sequence):
            field.write(self, value)

        # Trigger recomputation
        self.modified(vals)

        # Validation
        real_recs._validate_fields(vals, inverse_fields)

        # Inverse methods
        fields[0].determine_inverse(real_recs)

    if self._check_company_auto:
        self._check_company(list(vals))

    return True
```

### 3. Field Write Flow

**Entry point:** `Field.write(records, value)` in `odoo/orm/fields.py:1503`

```python
# fields.py:1503-1520
def write(self, records, value):
    # Cancel pending computation for this field on these records
    records.env.remove_to_compute(self, records)

    # Convert to cache format
    cache_value = self.convert_to_cache(value, records)

    # Skip records that already have this value
    records = self._filter_not_equal(records, cache_value)
    if not records:
        return

    # Update cache - SAME cache_value for ALL records
    self._update_cache(records, cache_value, dirty=True)
```

```python
# fields.py:1611-1631
def _update_cache(self, records, cache_value, dirty=False):
    field_cache = self._get_cache(env)

    # Same value applied to all record ids
    for id_ in records._ids:
        field_cache[id_] = cache_value  # <-- SAME value for all

    # Mark dirty for SQL flush
    if self.column_type and self.store and dirty:
        env._field_dirty[self].update(id_ for id_ in records._ids if id_)
```

**Problem:** `_update_cache` applies the same `cache_value` to all records.

### 4. Low-level SQL Write

**Entry point:** `BaseModel._write_multi(vals_list)` in `odoo/orm/models.py:4477`

```python
# models.py:4477-4557
def _write_multi(self, vals_list):
    # Requires one vals dict per record
    assert len(self) == len(vals_list)

    # Add write_uid, write_date
    if self._log_access:
        vals_list = [(log_vals | vals) for vals in vals_list]

    # Group by field set: {(field1, field2): [(id, val1, val2), ...]}
    updates = defaultdict(list)
    for record, vals in zip(self, vals_list):
        fnames, row = zip(*sorted(vals.items()))
        updates[fnames].append(record._ids + row)

    # Batch SQL in chunks of UPDATE_BATCH_SIZE (100)
    for fnames, rows in updates_list:
        self.env.execute_query(SQL(
            """UPDATE %(table)s
               SET %(assignments)s
               FROM (VALUES %(values)s) AS "__tmp"("id", %(columns)s)
               WHERE %(table)s."id" = "__tmp"."id"
            """,
            ...
        ))
```

**Key insight:** `_write_multi` already supports different values per record at SQL level.

### 5. Current _load_records (Upsert) Flow

**Entry point:** `BaseModel._load_records(data_list, update)` in `odoo/orm/models.py:5059`

```python
# models.py:5059-5164
def _load_records(self, data_list, update=False):
    # Look up existing xml_ids
    xml_ids = [data['xml_id'] for data in data_list if data.get('xml_id')]
    existing = {row: row for row in imd._lookup_xmlids(xml_ids, self)}

    # Partition into to_create and to_update
    to_create = []
    to_update = []
    for data in data_list:
        if existing.get(data['xml_id']):
            to_update.append(data)
        else:
            to_create.append(data)

    # UPDATE - NOT BATCHED (one by one!)
    for data in to_update:
        data['record']._load_records_write(data['values'])

    # CREATE - BATCHED
    if to_create:
        records = self._load_records_create([data['values'] for data in to_create])

    # Update xml_ids
    imd._update_xmlids(imd_data_list, update)

    return records
```

**Problem:** Updates loop one-by-one, creates are batched.

---

## Proposed Solution

### Step 1: Add `Field.write_multi()`

**Location:** `odoo/orm/fields.py` after line 1520

```python
def write_multi(self, record_values):
    """Write different values to different records.

    :param record_values: list of (record, value) pairs
    """
    if not record_values:
        return

    env = record_values[0][0].env
    all_ids = [record.id for record, _ in record_values]
    all_records = record_values[0][0].browse(all_ids)

    # MATCHES: records.env.remove_to_compute(self, records)
    env.remove_to_compute(self, all_records)

    field_cache = self._get_cache(env)
    dirty_ids = []

    for record, value in record_values:
        # MATCHES: cache_value = self.convert_to_cache(value, records)
        cache_value = self.convert_to_cache(value, record)

        # MATCHES: records = self._filter_not_equal(records, cache_value)
        id_ = record.id
        if id_ and field_cache.get(id_) != cache_value:
            # MATCHES: field_cache[id_] = cache_value from _update_cache
            field_cache[id_] = cache_value
            dirty_ids.append(id_)

    # MATCHES: env._field_dirty[self].update(...) from _update_cache
    if self.column_type and self.store and dirty_ids:
        env._field_dirty[self].update(dirty_ids)
```

**Mapping to existing `write()`:**

| Existing `write()` | Proposed `write_multi()` |
|--------------------|--------------------------|
| `remove_to_compute(self, records)` | `remove_to_compute(self, all_records)` - once for all |
| `convert_to_cache(value, records)` | `convert_to_cache(value, record)` - per pair |
| `_filter_not_equal(records, cache_value)` | `field_cache.get(id_) != cache_value` - per pair |
| `_update_cache(records, cache_value, dirty=True)` | `field_cache[id_] = cache_value` + batch dirty |

### Step 2: Add `BaseModel.write_multi()`

**Location:** `odoo/orm/models.py` after line 4471

```python
def write_multi(self, vals_list):
    """Update records with different values per record.

    :param vals_list: list of dicts, one per record in self, same keys in each
    """
    if not self:
        return True
    assert len(self) == len(vals_list)

    # MATCHES: self.check_access('write')
    self.check_access('write')

    # Get field names (same for all records in batch)
    fnames = set(vals_list[0].keys()) if vals_list else set()

    # MATCHES: for field_name in vals: self._check_field_access(...)
    for field_name in fnames:
        self._check_field_access(self._fields[field_name], 'write')

    env = self.env

    # MATCHES: bad_names filtering
    bad_names = {'id', 'parent_path'}
    if self._log_access:
        if not (env.uid == SUPERUSER_ID and not self.pool.ready):
            bad_names.update(LOG_ACCESS_COLUMNS)

    # MATCHES: vals filtering and write_uid/write_date defaults
    clean_vals_list = []
    for vals in vals_list:
        clean_vals = {k: v for k, v in vals.items() if k not in bad_names}
        if self._log_access:
            clean_vals.setdefault('write_uid', env.uid)
            clean_vals.setdefault('write_date', env.cr.now())
        clean_vals_list.append(clean_vals)

    # MATCHES: classify fields
    field_values_map = {}  # {field: [(record, value), ...]}
    protected = set()
    fnames_modifying_relations = []

    for field_name in fnames:
        if field_name in bad_names:
            continue
        field = self._fields.get(field_name)
        if not field:
            continue

        field_values_map[field] = [
            (record, vals.get(field_name))
            for record, vals in zip(self, clean_vals_list)
            if field_name in vals
        ]

        if self.pool.is_modifying_relations(field):
            fnames_modifying_relations.append(field_name)
        if field.inverse or (field.compute and not field.readonly):
            if field.store or field.type not in ('one2many', 'many2many'):
                protected.update(self.pool.field_computed.get(field, [field]))

    # MATCHES: self._recompute_recordset(to_compute)
    to_compute = [f.name for f in protected if f.compute and f.name not in fnames]
    if to_compute:
        self._recompute_recordset(to_compute)

    # MATCHES: with env.protecting(protected, self):
    with env.protecting(protected, self):
        # MATCHES: self.modified(fnames_modifying_relations, before=True)
        self.modified(fnames_modifying_relations, before=True)

        real_recs = self.filtered('id')

        # MATCHES: for field, value in sorted(...): field.write(self, value)
        # BUT: calls write_multi instead of write
        for field in sorted(field_values_map.keys(), key=lambda f: f.write_sequence):
            record_values = field_values_map[field]
            if record_values:
                field.write_multi(record_values)

        # MATCHES: self.modified(vals)
        self.modified(list(fnames))

        # MATCHES: real_recs._validate_fields(vals)
        real_recs._validate_fields(list(fnames))

    # MATCHES: if self._check_company_auto: self._check_company(list(vals))
    if self._check_company_auto:
        self._check_company(list(fnames))

    return True
```

### Step 3: Update `_load_records()` to use batch write

**Location:** `odoo/orm/models.py` lines 5122-5124

```python
# EXISTING (one-by-one):
for data in to_update:
    data['record']._load_records_write(data['values'])

# PROPOSED (batched):
if to_update:
    records = self.concat(*(data['record'] for data in to_update))
    vals_list = [data['values'] for data in to_update]
    records.write_multi(vals_list)
```

---

## Flow Comparison

### Existing Write (one-by-one)

```
_load_records calls write() N times:
  - check_access() runs N times
  - modified(before) runs N times
  - field.write() runs N times per field, each triggers SQL
  - modified(after) runs N times
  - validate() runs N times
```

### Proposed Write (batched)

```
_load_records calls write_multi() once:
  - check_access() runs 1 time
  - modified(before) runs 1 time
  - field.write_multi() runs 1 time per field, SQL batched via dirty flush
  - modified(after) runs 1 time
  - validate() runs 1 time
```

---

## Implementation Checklist

- [ ] Add `Field.write_multi()` in `odoo/orm/fields.py`
- [ ] Add `BaseModel.write_multi()` in `odoo/orm/models.py`
- [ ] Update `_load_records()` to call `write_multi()` for updates
- [ ] Add tests for `write_multi()` with various field types
- [ ] Test with relational fields (Many2one, One2many, Many2many)
- [ ] Test with computed stored fields
- [ ] Test with translated fields
- [ ] Performance benchmarks: before vs after

## Files Modified

| File | Changes |
|------|---------|
| `odoo/orm/fields.py` | Add `write_multi()` method (~30 lines) |
| `odoo/orm/models.py` | Add `write_multi()` method (~80 lines) |
| `odoo/orm/models.py` | Update `_load_records()` (~3 lines) |

**Total:** ~113 lines added, 3 lines modified
