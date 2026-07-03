# DMRlink3 Access Control Lists (ACLs)

DMRlink3 shares its ACL engine with HBlink3, but because of what DMRlink3 *is* —
a Motorola MOTOTRBO IPSC bridge/monitor — it applies ACLs in a narrower, more
focused way. In DMRlink3 the ACL system is primarily about **peer registration**:
deciding which repeaters/peers are allowed to join an IPSC network where
DMRlink3 is the master. The bridge application adds one global **subscriber**
filter on top. This document explains the grammar, the exact matching rules
(verified against `config.py`, `dmrlink.py`, and `bridge.py`), worked examples,
and honest performance guidance.

ACLs here are cheap and safe. Read the [Performance](#performance) section — for
an IPSC master, a registration ACL is close to free and is your first line of
defense.

---

## 1. The two-part grammar

Every ACL is a single string of the form:

```
ACTION:entry,entry,entry,...
```

* **ACTION** is either `PERMIT` or `DENY`.
* **entries** are comma-separated, and each entry is one of:
  * a **single ID** — e.g. `12345`
  * a **range** — two IDs joined by a hyphen, low first — e.g. `1000-2000`
  * the literal keyword **`ALL`** — matches every possible ID.

Examples of valid ACLs:

```
PERMIT:ALL
DENY:1
PERMIT:311111,311222,312000-312099
DENY:3120101,3120124
```

An **empty** value is treated as `PERMIT:ALL` (permit everything).

> There is exactly **one** ACTION per ACL. You cannot mix `PERMIT` and `DENY`
> entries in the same list.

---

## 2. The single most important rule: what happens to IDs you *didn't* list

This is the part that trips everyone up, so read it twice.

An ACL does two things at once. It defines an ACTION for the IDs you **list**,
and it *implies the opposite ACTION* for every ID you **don't** list.

| You write | A **listed** ID is… | An **unlisted** ID is… | Mental model |
|-----------|--------------------|------------------------|--------------|
| `PERMIT:…` | permitted | **denied** | **whitelist** — only these get in |
| `DENY:…`   | denied    | **permitted** | **blacklist** — everyone except these |

So for a registration ACL:

* `PERMIT:311111,311222` means *"only peers 311111 and 311222 may register,
  reject every other login."*
* `DENY:311999` means *"reject peer 311999, accept every other peer."*

This is verified in `acl_check()` — a match returns the ACTION, and a non-match
returns `not action`:

```python
def acl_check(_id, _acl):
    id = int_id(_id)
    action, singles, starts, ends = _acl
    if id in singles:
        return action           # listed  -> the ACTION
    i = bisect.bisect_right(starts, id) - 1
    if i >= 0 and id <= ends[i]:
        return action           # listed  -> the ACTION
    return not action           # unlisted -> the OPPOSITE
```

**Rule of thumb:** use `PERMIT:` when you run a closed IPSC network and want only
a known list of repeaters to join. Use `DENY:` when you want an otherwise-open
network minus a few blocked IDs.

---

## 3. The two ACLs DMRlink3 actually uses

Unlike HBlink3, DMRlink3 does **not** apply per-timeslot talkgroup ACLs or
per-system subscriber ACLs. Its `process_acls()` wires up exactly two:

| Key | Where | Gates | Value domain | Enforced when |
|-----|-------|-------|--------------|---------------|
| `REG_ACL` | per-system (`[SAMPLE-PEER]` etc.) | **Peer registration** (an IPSC peer requesting to join) | peer IDs, `1 … 4294967295` | **Only when `MASTER_PEER: True`** |
| `SUB_ACL` | `[GLOBAL]` only | **Subscriber** source ID of a group/private call passing through the **bridge** | subscriber IDs, `1 … 16776415` | When the bridge (`bridge.py`) is running |

### 3a. `REG_ACL` — the heart of DMRlink3 ACLs

A registration ACL only makes sense on a system where **DMRlink3 is the IPSC
master** (`MASTER_PEER: True`). When a peer sends a registration request,
`master_reg_req()` checks it:

```python
def master_reg_req(self, _data, _peerid, _host, _port):
    if not acl_check(_peerid, self._local['REG_ACL']):
        logger.warning('(%s) Peer Registration ***REJECTED BY ACL***: ...')
        return
    ...
```

If DMRlink3 is a **peer** on someone else's master (`MASTER_PEER: False`), the
`REG_ACL` is ignored — a peer does not decide who else joins the master's
network. The sample config says so directly:

```ini
# REG_ACL is ignored when MASTER_PEER: False — peers don't control registration
REG_ACL: PERMIT:ALL
```

Registration IDs are peer IDs and are validated against the full 32-bit range
(`1 … 4294967295`, `const.PEER_MAX`).

### 3b. `SUB_ACL` — global subscriber gate on the bridge

The bridge (`bridge.py`, `group_voice()` and its private-call counterpart) checks
the **global** `SUB_ACL` against the source subscriber before propagating a call:

```python
def group_voice(self, _src_sub, _dst_group, _ts, _end, _peerid, _data):
    if not acl_check(_src_sub, self._CONFIG['GLOBAL']['SUB_ACL']):
        logger.warning('(%s) Group Voice ***REJECTED BY ACL*** From: ...')
        return
    ...
```

There is only **one** `SUB_ACL`, in `[GLOBAL]`. It is checked against subscriber
IDs (`1 … 16776415`, `const.ID_MAX`). Set it to `PERMIT:ALL` if you don't want
subscriber filtering.

`USE_ACL` in `[GLOBAL]` is the master enable switch for subscriber filtering.

---

## 4. Worked examples

**Closed IPSC network (recommended for a private master).** Only these repeaters
may register; everything else is rejected:

```ini
[MY-IPSC-MASTER]
MASTER_PEER: True
REG_ACL: PERMIT:311111,311222,311333
```

**Open network minus a few bad actors.** Accept any peer except two known-bad
IDs:

```ini
[MY-IPSC-MASTER]
MASTER_PEER: True
REG_ACL: DENY:311998,311999
```

**Registering a block of site repeaters by range.** Allow an allocated peer-ID
block plus one out-of-block gateway:

```ini
[MY-IPSC-MASTER]
MASTER_PEER: True
REG_ACL: PERMIT:311000-311099,312500
```

**Global subscriber blacklist on the bridge.** Let all subscribers through
except a few, while leaving registration wide open:

```ini
[GLOBAL]
USE_ACL: True
SUB_ACL: DENY:1,2606234,3141592

[MY-IPSC-MASTER]
MASTER_PEER: True
REG_ACL: PERMIT:ALL
```

**Members-only subscriber policy on the bridge.** Only your membership block may
be bridged:

```ini
[GLOBAL]
USE_ACL: True
SUB_ACL: PERMIT:3120000-3120999
```

**Large but efficient range list.** Adjacent and contiguous ranges are merged at
load time, so this…

```ini
REG_ACL: PERMIT:311000-311099,311100-311199,311200-311299,312500
```

…collapses internally to a single span `311000–311299` plus the single ID
`312500`. Write ranges however is clearest to *you*; the engine tidies them up.

---

## 5. How matching actually works (for the curious)

At startup, `acl_build()` turns each ACL string into a compact structure:

```
(action, frozenset_of_single_ids, sorted_range_starts, sorted_range_ends)
```

* **Single IDs** go into a `frozenset` → membership test is O(1).
* **Ranges** are sorted and merged into disjoint spans by `merge_ranges()`
  (adjacent ranges, e.g. `1-5` and `6-10`, are fused into `1-10`), then stored as
  two parallel tuples so `acl_check()` finds any match with **one binary search**
  (`bisect`), i.e. O(log n).

So a check is one hash-set lookup plus one binary search over an already-minimized
range list.

---

## Performance

**Short version: on an IPSC master, use a `REG_ACL`. It costs almost nothing.**

* Parsing/merging happens **once, at startup** — never on the hot path.
* Registration is a **rare event** (a peer registers, then periodically
  re-registers on the ping interval). A `REG_ACL` check runs a handful of times
  per peer per interval — utterly negligible, and it protects your network from
  unwanted peers.
* The global `SUB_ACL` runs once per call on the bridge: one set lookup plus one
  binary search. For *n* merged ranges the search is about **log₂(n)**
  comparisons — 1,000 ranges is ~10 comparisons. You won't measure it.
* Adjacent/overlapping ranges are **merged**, so a large-looking config often
  reduces to a few spans in memory.

Practical guidance:

* Prefer ranges over enumerating many consecutive IDs — less typing, smaller
  memory, same speed.
* A registration ACL is the single highest-value, lowest-cost ACL you can set on
  a master. There is no performance reason to leave it at `PERMIT:ALL` if you
  actually want to restrict who joins.
* If you truly want no subscriber filtering, `USE_ACL: False` (or
  `SUB_ACL: PERMIT:ALL`) skips it.

---

## Warnings

* **`PERMIT` is a whitelist — it rejects every peer/ID you didn't list.** Writing
  `REG_ACL: PERMIT:311111` intending "also allow 311111" will lock out every
  other repeater on the network. Re-read [Section 2](#2-the-single-most-important-rule-what-happens-to-ids-you-didnt-list).
* **`REG_ACL` only applies when `MASTER_PEER: True`.** On a peer system it is
  ignored — you cannot filter registrations on a network you don't master.
* **Out-of-range entries stop DMRlink3 at startup.** A subscriber entry above
  16776415, or a peer entry above 4294967295, triggers
  `ACL CREATION ERROR, VALUE OUT OF RANGE` and the program exits. Recheck your
  numbers if DMRlink3 won't start after an ACL edit.
* **Write ranges low-to-high** (`311000-311099`, not `311099-311000`). A reversed
  range will silently never match.
* **There is only one `SUB_ACL`, and it lives in `[GLOBAL]`.** DMRlink3 does not
  read per-system subscriber or talkgroup ACLs — don't expect a `SUB_ACL` under a
  peer stanza to do anything.
* **`USE_ACL` gates the subscriber filter, not registration.** `REG_ACL` on a
  master is governed by `MASTER_PEER`, not by `USE_ACL`.
