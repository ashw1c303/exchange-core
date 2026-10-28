# RFC-001: Good-Til-Date (GTD) Order Expiry

| Field | Value |
| --- | --- |
| Status | Proposed |
| Authors | AIDLC Office Hours |
| Target | `exchange-core` matching + risk pipeline |
| Related guardrails | `.aidlc/guardrails.json` |
| Implementation code | **Out of scope for this RFC** (Office Hours: docs only) |

## 1. Summary

Add Good-Til-Date (GTD) time-in-force so resting limit orders automatically leave the book and release risk holds when `OrderCommand.timestamp >= expireTimestamp`, without wall-clock calls on the hot path and without breaking deterministic journal replay.

## 2. Motivation

Today `OrderType` only models immediate semantics (`IOC` / `FOK*`) and open-ended resting (`GTC`). There is no engine-native way to retire resting liquidity at a future exchange time. Clients can cancel externally, but that is not journal-deterministic relative to matching, races with fills, and cannot guarantee expiry if the publisher is silent.

GTD must:

1. Rest like GTC until expiry.
2. Expire using command time only (`OrderCommand.timestamp`), never `System.currentTimeMillis()`.
3. Unlock balances / margin through Risk R2 the same way cancel/`REDUCE` does today.
4. Survive ME snapshots and journal replay with a versioned wire change.

## 3. Office Hours Checklist (Invariants)

### 3.1 Flyweight Wire Compatibility

| Surface | Impact | Breaking? |
| --- | --- | --- |
| `OrderCommand` | Add `long expireTimestamp` (0 = unused / non-GTD) | In-memory flyweight only; ring slot layout grows by 8 bytes |
| `ApiPlaceOrder` | Add `expireTimestamp` | API binary/journal writers must emit it for `PLACE_ORDER` |
| `Order` / `DirectOrder` | Persist `expireTimestamp` after `timestamp` in `BytesIn`/`BytesOut` | **Yes** — ME snapshot order records change |
| Journal `PLACE_ORDER` | Append `expireTimestamp` after `actionAndType` byte | **Yes** — `DiskSerializationProcessor` read/write |
| `OrderType` | Add `GTD(5)` | Compatible if unknown codes still throw; old journals never emit 5 |

**Verdict:** Wire change is required. Protected journaling paths are in blast radius and need an explicit format/version bump (see §6). Do not silently reinterpret old snapshots.

### 3.2 Deterministic Time & Replay

- Expiry comparisons use **only** `cmd.timestamp` from the Disruptor event (publisher- or journal-supplied).
- Forbidden on ME/RE hot path: `System.currentTimeMillis()`, `Instant.now()`, `java.time.*`, scheduled `Executor` timers inside order books.
- Idle books: time does not advance without a command. Introduce `OrderCommandType.EXPIRE_ORDERS` (symbol-scoped or shard-wide) so operators / test harnesses / a non-hot-path timer can inject deterministic time without placing junk orders. Journal records these like any other mutating command.

### 3.3 Single-Threaded Partitioning

- Expiry state lives **inside each `IOrderBook`**, owned by the ME shard selected by existing `symbolId & shardMask` affinity. No cross-shard maps, no locks.
- Risk R1 (`preProcessCommand`): GTD `PLACE_ORDER` uses the same hold path as GTC. `EXPIRE_ORDERS` skips R1 holds (like `CANCEL_ORDER` today) and is marked valid for ME by default routing.
- Risk R2 (`handlerRiskRelease`): must release holds for **expired makers that are not `cmd.uid`**. Today exchange-mode R2 treats a leading `REDUCE`/`REJECT` as belonging solely to `cmd.uid` and then expects trades. Bulk expiry therefore **cannot** piggyback on an unrelated `PLACE_ORDER` without an R2 contract change (see §5).

### 3.4 Guardrail Blast Radius

Cross-check vs `.aidlc/guardrails.json`:

| Guardrail | Assessment |
| --- | --- |
| `protected_paths` → `ExchangeCore.java` | Prefer **no** topology change. Wire `EXPIRE_ORDERS` through existing ME handler switch + `ExchangeApi` translators only. Touch `ExchangeCore` only if a new Disruptor stage is proposed (rejected in this RFC). |
| `protected_paths` → `processors/journaling/**` | **Impacted** — journal encode/decode for `PLACE_ORDER` (+ optional `EXPIRE_ORDERS`). Requires this RFC approval before code. |
| `hot_path_packages` | Order books + `MatchingEngineRouter` + `RiskEngine` will change; Zero-GC rules apply. |
| `forbidden_imports_in_hot_path` | No `HashMap`/`ArrayList`/`Instant`/locks in ME/RE expiry structures. |
| `max_files_changed_per_pr: 8` | Full feature exceeds budget → **phased PRs** (§8). |
| `max_p99_latency_ns: 1500` | Expiry must be O(k) in expired orders per command, not O(n) full-book scans on every command. |

## 4. Invariants & Memory Layout Impact

### 4.1 Proposed type & fields

```text
OrderType.GTD(5)          // place path == GTC match+rest, plus expiry metadata
OrderCommand.expireTimestamp : long   // required when orderType==GTD; 0 otherwise
Order / DirectOrder.expireTimestamp : long
OrderCommandType.EXPIRE_ORDERS (new code, e.g. 5)  // mutate=true
MatcherEventType.EXPIRE   // or reuse REDUCE with multi-party R2 semantics
```

Validation (R1 or ME, prefer R1 for fail-fast):

- `GTD` ⇒ `expireTimestamp > cmd.timestamp` else reject before book mutation.
- Non-`GTD` ⇒ `expireTimestamp` must be `0` (ignore or reject non-zero to keep journal canonical).

### 4.2 Resting layout (Zero-GC)

Per order book, maintain an intrusive expiry index owned by the ME thread:

- Prefer a **min-ordered intrusive structure** keyed by `expireTimestamp` then `orderId` (primitive open-addressed map `orderId → node`, nodes from `ObjectsPool`).
- On GTD rest: insert into index.
- On fill/cancel/reduce-to-zero/move: remove from index.
- On command with `cmd.timestamp`: pop while `peek.expireTimestamp <= cmd.timestamp`, remove from price levels, append release events.

Do **not** use `java.util.PriorityQueue`, boxed heaps, or streams on the hot path.

### 4.3 Snapshot / journal layout

**Order record (Naive `Order` and `DirectOrder`)** — append after existing `timestamp`:

```text
... uid:long | timestamp:long | expireTimestamp:long ...
```

**Journal `PLACE_ORDER`** — append after `actionAndType`:

```text
... actionAndType:byte | expireTimestamp:long
```

**Journal `EXPIRE_ORDERS`**:

```text
uid:long (0) | symbol:int | /* optional size=0 */
```

`timestamp` on the command is the exchange time watermark used for expiry.

Introduce an explicit **serialization generation** (journal header / snapshot preamble already available via snapshot id conventions) so old snapshots refuse load instead of mis-aligned `readLong` chains. Detail the bump in the journaling PR; do not invent silent dual-read without tests.

### 4.4 Event semantics for R2

Rejected approach: expire foreign uids during `PLACE_ORDER` while emitting classic single-party `REDUCE` for `cmd.uid`.

Accepted approach:

1. For each expired resting order, emit `MatcherEventType.EXPIRE` (recommended) or `REDUCE` with **`matchedOrderUid` / `matchedOrderId` set to the expired order’s identity**, `size = remaining`, `bidderHoldPrice = reserveBidPrice`, `activeOrderCompleted = true`.
2. Teach `RiskEngine.handlerRiskRelease` to walk a **prefix chain of EXPIRE/REDUCE** events and release each using the **event’s order uid**, not only `cmd.uid`.
3. After the expiry prefix, existing TRADE/REJECT handling for the triggering command proceeds unchanged.

`CANCEL_ORDER` path stays as today (single `REDUCE`, taker = `cmd.uid`).

## 5. Recommended Design

### 5.1 Place path

GTD places through the same match-then-rest path as GTC (`newOrderPlaceGtc` shared or thin wrapper). After partial/full rest, if remaining size > 0, store `expireTimestamp` and index it.

### 5.2 When expiry runs

Run expiry at the start of symbol-local ME processing for:

- `PLACE_ORDER`, `MOVE_ORDER`, `CANCEL_ORDER`, `REDUCE_ORDER`, `ORDER_BOOK_REQUEST`
- `EXPIRE_ORDERS`

Watermark = `cmd.timestamp`. This keeps expiry ordered relative to matching in the same command batch.

### 5.3 Idle-book progress

External (off hot path) component publishes `EXPIRE_ORDERS` with monotonic timestamps. On replay, the journal re-injects the same commands → identical book state. No ME thread sleeps or schedules.

### 5.4 Alternatives considered

| Alternative | Why rejected / deferred |
| --- | --- |
| Wall-clock timer inside ME | Breaks deterministic replay |
| Client-only cancels | Not atomic vs fills; silent failure if client dies |
| Lazy expire only on next trade, no `EXPIRE_ORDERS` | Idle GTD rests forever |
| Encode expiry in `userCookie` | Wrong width/semantics; cookie is client opaque |
| New Disruptor stage / change `ExchangeCore` topology | Violates protected-path preference; unnecessary |
| Full-book scan each command | Misses p99 SLA on deep books |

## 6. Mermaid Sequence Diagram

```mermaid
sequenceDiagram
    autonumber
    participant Pub as Publisher / Journal Replay
    participant J as Journaler (optional)
    participant R1 as RiskEngine R1
    participant ME as MatchingEngineRouter / OrderBook
    participant R2 as RiskEngine R2
    participant RH as ResultsHandler

    Note over Pub,RH: A) Place GTD
    Pub->>J: PLACE_ORDER GTD expireTimestamp=Texp
    Pub->>R1: PLACE_ORDER (timestamp=T0)
    R1->>R1: Hold funds/margin like GTC<br/>validate Texp > T0
    R1->>ME: resultCode=VALID_FOR_MATCHING_ENGINE
    ME->>ME: Expire resting where Texp <= T0
    ME->>ME: Match as GTC; rest residual; index Texp
    ME->>R2: TRADE* + EXPIRE* event chain
    R2->>R2: Release EXPIRE by event uid;<br/>apply trades for cmd.uid / makers
    R2->>RH: Command + events

    Note over Pub,RH: B) Time watermark / idle expiry
    Pub->>J: EXPIRE_ORDERS symbol=S timestamp=T1
    Pub->>R1: EXPIRE_ORDERS (no hold)
    R1->>ME: pass-through
    ME->>ME: Pop expiry index while Texp <= T1<br/>remove orders, build EXPIRE chain
    ME->>R2: EXPIRE* (multi-uid)
    R2->>R2: Unlock each expired uid
    R2->>RH: Success + expire events
```

Pipeline alignment with production topology:

`Publisher → (Grouping) → Journal ∥ RiskR1 → MatchingEngine → RiskR2 → ResultsHandler`

## 7. Guardrail Compliance Table

| Path / Rule | Touched? | Notes |
| --- | --- | --- |
| `src/main/java/exchange/core2/core/ExchangeCore.java` | **Avoid** | No new Disruptor handlers in v1 |
| `src/main/java/exchange/core2/core/processors/journaling/**` | **Yes (protected)** | `PLACE_ORDER` (+ `EXPIRE_ORDERS`) encode/decode; format bump + tests |
| `orderbook/OrderBookDirectImpl.java` | Yes | GTD place, `DirectOrder.expireTimestamp`, expiry index, expire sweep |
| `orderbook/OrderBookNaiveImpl.java` | Yes | Parity with Direct |
| `orderbook/OrderBookEventsHelper.java` | Yes | `sendExpireEvent` |
| `orderbook/IOrderBook.java` | Maybe | Shared expire hook / command dispatch |
| `processors/MatchingEngineRouter.java` | Yes | Route `EXPIRE_ORDERS` into symbol books |
| `processors/RiskEngine.java` | Yes | GTD validation in R1; multi-uid EXPIRE release in R2 |
| `common/OrderType.java` | Yes | `GTD(5)` |
| `common/cmd/OrderCommand.java` | Yes | `expireTimestamp` |
| `common/cmd/OrderCommandType.java` | Yes | `EXPIRE_ORDERS` |
| `common/Order.java` / `IOrder.java` | Yes | Field + marshalling |
| `common/MatcherEventType.java` | Yes | `EXPIRE` |
| `common/api/ApiPlaceOrder.java` / `ExchangeApi.java` | Yes | API + translators |
| `java.util.HashMap` / locks / `Instant` on hot path | **Forbidden** | Use Eclipse primitive maps + object pool |
| `max_files_changed_per_pr: 8` | Constraint | Split per §8 |
| `max_p99_latency_ns: 1500` | Constraint | Amortize expiry; bound work per command if needed |

### Tentative file list (implementation follow-ups; not part of this RFC PR)

Phase-sized so each PR ≤ 8 files (tests may need a waiver or companion PR):

1. **Wire + API types** — `OrderType`, `OrderCommand`, `OrderCommandType`, `IOrder`, `Order`, `ApiPlaceOrder`, `MatcherEventType`, (+ journal types deferred).
2. **ME expiry** — `OrderBookDirectImpl`, `OrderBookNaiveImpl`, `OrderBookEventsHelper`, `IOrderBook`, `MatchingEngineRouter`.
3. **Risk R1/R2** — `RiskEngine` (+ focused tests).
4. **Journaling (protected)** — `DiskSerializationProcessor` (+ replay tests) under this RFC.
5. **API surface** — `ExchangeApi`, `SimpleEventsProcessor` / `IEventsHandler` if expire callbacks are exposed.

## 8. Rollout Plan

1. Land this RFC under `docs/rfcs/` (no `.java` changes).
2. Implement types + ME expiry behind journal version gate.
3. Land Risk R2 multi-party EXPIRE before enabling bulk sweep in production configs.
4. Land journaling format bump with replay fixtures (empty expire for legacy migration path only if dual-read is explicitly tested; otherwise require fresh snapshots).
5. Perf: hdr histogram / existing bench — place GTD, expire burst of N orders, confirm p99 budget.

## 9. Testing Requirements (for implementation PRs)

- Deterministic replay: place GTD → trades → `EXPIRE_ORDERS` → identical state hash after journal replay.
- Expiry vs fill race: command at `T` that both matches and expires uses single watermark ordering (expire first, then match — document and test).
- Multi-uid expiry releases correct currencies for exchange and margin symbols.
- Reject `expireTimestamp <= cmd.timestamp` on place.
- Snapshot round-trip includes `expireTimestamp`.
- Zero-GC smoke: no hot-path allocations in expire pop loop (existing alloc instrumentation / careful review).

## 10. Open Questions

1. Should `MOVE_ORDER` allow updating `expireTimestamp`, or is expiry immutable after place?
2. Exact `OrderCommandType` byte for `EXPIRE_ORDERS` (avoid collisions with `ORDER_BOOK_REQUEST=6`).
3. Dual-read vs hard snapshot cutover for marshalling.
4. Whether results API gains a first-class `OrderExpired` callback vs reusing reduce/cancel notifications.

## 11. Decision

**Proceed** with GTD as `OrderType.GTD` + `expireTimestamp` on command/order records, command-watermark expiry inside the symbol order book, `EXPIRE_ORDERS` for idle progress, and Risk R2 multi-uid `EXPIRE` handling. Journal/snapshot layout changes are approved in principle by this RFC; `ExchangeCore` topology stays unchanged.
