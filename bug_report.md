# Bug Report — Team Drill Machine

**CoWork: Multi-Tenant Coworking Space Booking API — Preliminary Round**

How we worked: we read the problem statement first, then went through every file in `app/`
comparing the code against the business rules in Section 4. Anything that looked off went on a
suspect list. We fixed the obvious stuff first, then spun the server up and wrote our own
black-box test scripts against the HTTP API — about 130 checks total, including a sequential
suite for every business rule and a concurrency suite (thread pools hammering the same
slot/booking/user) for the race conditions. Everything below was confirmed through the actual
API, not just by reading code.

Line numbers refer to the original, unfixed code.

---

## Auth & tokens

### 1. Access tokens lasted 15 hours instead of 15 minutes
`app/auth.py`, line 50

The lifetime was computed as `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)`. The config
value is already in minutes (15), so multiplying by 60 while still passing it as `minutes=` gives
900 *minutes*. The spec wants access tokens to expire in exactly 900 *seconds*, and the grader can
see this straight from the `exp - iat` claims.

**Fix:** dropped the `* 60`. Now `exp - iat == 900`.

### 2. Logout didn't actually log you out
`app/auth.py`, line 86 (store) vs line 97 (check)

This one is a two-line comedy: `revoke_access_token` puts the token's `jti` into the revoked set,
but the check in `get_token_payload` compares `payload.get("sub")` — the user id — against that
set. A user id is never equal to a uuid hex, so no token was ever considered revoked and
logged-out tokens kept working forever.

**Fix:** the check now compares `payload.get("jti")`, matching what logout stores.

### 3. Refresh tokens could be replayed forever
`app/routers/auth.py`, lines 81–93

The spec says refresh tokens are single-use: using one must invalidate it, and reusing it must
give 401. The refresh endpoint handed out new tokens but never remembered that the old one was
spent, so you could refresh with the same token as many times as you liked.

**Fix:** we track used refresh jtis in a set in `app/auth.py` (`consume_refresh_token`), guarded
by a lock so two simultaneous refreshes with the same token can't both win. First use passes and
records the jti, any later use gets 401.

### 4. Registering a taken username returned the existing user
`app/routers/auth.py`, lines 37–43

Instead of the required `409 USERNAME_TAKEN`, registering a duplicate username in an org quietly
returned 201 with the *existing* user's id and role. Besides violating the contract, that's a
nice little account-probing hole.

**Fix:** it now raises `AppError(409, "USERNAME_TAKEN", ...)`.

### 5. Concurrent registrations could crash with a 500
`app/routers/auth.py`, lines 26–30 and 45–53

Org creation and user creation were plain check-then-insert. If two requests raced to create the
same new org (or the same username), the loser blew up on the DB unique constraint with an
unhandled `IntegrityError` → HTTP 500, which is never a valid response under the error contract.

**Fix:** both commits catch `IntegrityError`. Losing the org race means the org now exists, so
the caller just joins it as a member; losing the username race returns `409 USERNAME_TAKEN`.
Verified with 8 parallel registrations: one admin, no 500s.

---

## Datetimes

### 6. Timezone offsets were thrown away instead of converted
`app/timeutils.py`, lines 12–13

`parse_input_datetime` did `dt.replace(tzinfo=None)` on offset-carrying inputs. That keeps the
wall-clock time and just deletes the offset, so `2026-07-10T10:00:00+06:00` was stored as 10:00
"UTC" when the correct answer is 04:00. Every downstream calculation — conflicts, quota windows,
refund notice, availability dates — was wrong for any client not already sending UTC.

**Fix:** `dt.astimezone(timezone.utc).replace(tzinfo=None)` — convert first, then strip.

---

## Creating bookings

### 7. A 5-minute grace window for past start times
`app/routers/bookings.py`, line 86

The check was `if start <= now - timedelta(seconds=300)`, quietly allowing bookings that start up
to 5 minutes in the past. The spec is explicit: strictly in the future, *no grace window*.

**Fix:** `if start <= now`.

### 8. Zero-hour and negative-duration bookings were accepted
`app/routers/bookings.py`, lines 89–94

Only `duration > 8h` was rejected. There was no minimum check and no end-after-start check, so
`end == start` sailed through, and — our favourite find of the night — an `end` *before* `start`
by a whole number of hours also passed every check and created a booking with a **negative
price**.

**Fix:** added `end <= start → 400` and `duration < 1 → 400`, both `INVALID_BOOKING_WINDOW`.

### 9. Back-to-back bookings were treated as conflicts
`app/routers/bookings.py`, line 50

The overlap test used `<=` on both sides (`b.start_time <= end and start <= b.end_time`). The
spec literally hands you the formula — overlap iff `existing.start < new.end AND new.start <
existing.end` — and explicitly says back-to-back is allowed. With the inclusive version, a
booking ending at 10:00 blocked another starting at 10:00.

**Fix:** strict `<` on both comparisons.

### 10. Two people could book the same slot at the same time
`app/routers/bookings.py`, lines 100–118

The conflict check and the insert were separate steps with no synchronization, and there was even
a planted 120 ms `time.sleep` (`_pricing_warmup`) sitting between them to make the race easy to
hit. Two concurrent requests for the same slot both saw "free" and both committed.

**Fix:** a module-level lock (`_booking_write_lock`) makes the whole
check-conflict → check-quota → insert → commit sequence atomic (single-process deployment, so a
process lock is the right tool). The sleep did no real work so it went too. Verified: 8 threads
racing for one slot → exactly one 201 and seven `409 ROOM_CONFLICT`.

### 11. The quota check had the same race
`app/routers/bookings.py`, lines 55–71

Same check-then-act pattern (plus its own planted `_quota_audit` sleep): parallel requests all
counted the same snapshot, all saw "2 bookings, room for one more", and all inserted. The
3-bookings-per-24h rule fell over exactly when it mattered.

**Fix:** the quota check runs inside the same booking write lock. Verified: 6 parallel creates →
exactly three 201s and three `409 QUOTA_EXCEEDED`.

### 12. Caches went stale in both directions
`app/routers/bookings.py`, lines 120–122 (create) and 215–218 (cancel)

Creating a booking invalidated the availability cache but never the usage-report cache, so
`/admin/usage-report` kept serving pre-booking numbers. Cancelling did the mirror image: it
invalidated the report but not availability, so cancelled bookings stayed "busy" in
`/rooms/{id}/availability`. Both endpoints are required to reflect the current state immediately.

**Fix:** create also invalidates the org's report cache; cancel also invalidates the room's
availability cache for the booking's date.

---

## Listing & reading bookings

### 13. Pagination was wrong three different ways
`app/routers/bookings.py`, lines 136–140

In one query: descending order instead of the required ascending-by-`start_time`;
`.offset(page * limit)` instead of `(page - 1) * limit`, which skips the entire first page; and a
hardcoded `.limit(10)` that ignored the caller's `limit` parameter. Page 1 with the defaults
showed you items 11–20, in reverse.

**Fix:** `order_by(start_time.asc(), id.asc()).offset((page - 1) * limit).limit(limit)`.
Verified that walking sequential pages returns every item exactly once, in order.

### 14. Any member could read any booking in their org
`app/routers/bookings.py`, lines 156–163

`GET /bookings/{id}` only checked that the booking's room belonged to the caller's org. The spec
says members can read only their *own* bookings, with someone else's id behaving as
`404 BOOKING_NOT_FOUND`. Funny detail: the cancel endpoint right below it *does* have the
ownership check — it was just missing from the read path.

**Fix:** added the same check: non-admins who don't own the booking get 404.

### 15. Booking detail returned the wrong start_time
`app/routers/bookings.py`, line 166

After serializing the booking correctly, the handler did
`response["start_time"] = iso_utc(booking.created_at)` — overwriting the real start time with the
moment the row was created. No idea what this line thought it was doing.

**Fix:** deleted it.

---

## Cancelling & refunds

### 16. Refund tiers were wrong at both ends
`app/routers/bookings.py`, lines 198–206

Two separate problems in one if-chain. First, the notice was floored to whole hours and compared
with `> 48`, so cancelling with *exactly* 48 hours of notice landed in the 50% bucket instead of
the 100% one. Second, the `else` branch — the under-24-hours case that should refund nothing —
returned 50 instead of 0. Late cancellers were getting half their money back.

**Fix:** compare the timedelta directly: `>= 48h → 100`, `>= 24h → 50`, else `0`.

### 17. The refund amount in the response could differ from the ledger
`app/services/refunds.py`, lines 14–17 and `app/routers/bookings.py`, line 208

The response computed the amount with Python's `round()` (banker's rounding — halves go to the
nearest *even* cent) while the RefundLog computed it separately with floats and `int()`
(truncation). Neither matches the spec's "half-cents round up", and the spec also requires the
response amount to equal the stored amount. Concrete case: 3333 cents at 50% is 1666.5 — the spec
answer is 1667, both code paths said 1666.

**Fix:** one computation, integer-only, in `log_refund`:
`amount_cents = (price_cents * percent + 50) // 100` (round half up, no float error). The cancel
endpoint takes its response value from the returned RefundLog row, so the two can't disagree.

### 18. Cancelling twice in parallel = two refunds
`app/routers/bookings.py`, lines 195–214

The flow was: check status → 120 ms planted sleep (`_settlement_pause`) → flip status → commit.
Two concurrent cancels of the same booking both read `confirmed`, both logged a refund, both
returned 200. Bonus problem: the refund was committed *before* the status flip, so a crash in
between would refund a booking that stayed confirmed.

**Fix:** the cancel critical section runs under the same booking write lock, re-reads the fresh
status from the DB (`db.expire`), rejects if already cancelled, then flips the status and writes
the RefundLog in a single commit. Verified: 8 parallel cancels → one 200, seven
`409 ALREADY_CANCELLED`, exactly one RefundLog, and its amount equals the response.

---

## The services

### 19. A textbook deadlock in notifications
`app/services/notifications.py`, lines 24–35

`notify_created` takes the email lock, then the audit lock. `notify_cancelled` took them in the
*opposite* order. One create and one cancel landing together could each grab their first lock and
wait on the other forever — and since every subsequent booking or cancel also needs those locks,
the whole service wedges. That's the liveness rule (16) gone. The sleeps inside the critical
sections made the bad interleaving very easy to hit.

**Fix:** both functions acquire email → audit. Same order everywhere = no deadlock, by
construction. We kept the sleeps here since they stand in for real I/O (the fake SMTP/audit
writes). Verified with a 12-thread create+cancel storm: everything completes, `/health` stays up.

### 20. Duplicate reference codes under load
`app/services/reference.py`, lines 17–21

`next_reference_code` read the counter, slept 120 ms (planted `_format_pause`), then wrote back
`current + 1`. Any two concurrent creates read the same counter value and got the same
"unique" code.

**Fix:** the read-and-increment is atomic under a lock; the sleep served no purpose and was
removed. Verified: 10 parallel creates → 10 distinct codes.

### 21. The rate limiter forgot requests under load
`app/services/ratelimit.py`, lines 18–26

Each request copied the user's bucket, slept 100 ms (planted `_settle_pause`), appended its own
timestamp, and wrote the copy back. Concurrent requests all copied the same snapshot and
clobbered each other's appends — so a burst of 30 sailed past the 20/minute limit.

**Fix:** trim + append + count happen atomically under a lock. Verified: 30 parallel booking
requests from one user → exactly 20 processed and 10 × `429 RATE_LIMITED`.

### 22. Room stats drifted away from reality
`app/services/stats.py`, lines 15–26

Same lost-update pattern: read the counters, sleep 100 ms (planted `_aggregate_pause`), write
back. Under concurrent bookings the increments overwrote each other and `/rooms/{id}/stats`
stopped matching the actual booking table — exactly what rule 14 says must never happen,
"including after bursts of concurrent activity".

**Fix:** the read-modify-write is atomic under a lock. Verified: after our parallel create/cancel
storms, stats match the bookings to the cent.

### 23. The CSV export leaked other orgs' data
`app/services/export.py`, lines 48–52

The nastiest one in the pile. With `include_all=true&room_id=<id>`, the export took a special
path through `fetch_bookings_raw` — which filters by room id only, with **no org check**. An
admin of org A could pass a room id belonging to org B and download org B's entire booking
history, reference codes and all. Complete multi-tenancy breach.

**Fix:** that branch now goes through `_fetch_scoped`, which always applies the caller's org
filter (with the room filter on top). Verified: asking for another org's room returns just the
CSV header.

---

## About all those `time.sleep()` calls

The codebase was sprinkled with little helpers — `_pricing_warmup`, `_quota_audit`,
`_settlement_pause`, `_format_pause`, `_settle_pause`, `_aggregate_pause` — that did nothing but
sleep. They're not real work; they exist to stretch the race windows of bugs 10, 11, 18, 20, 21
and 22 so the races fire reliably. The actual defects were the unsynchronized check-then-act
sequences, which we fixed with proper locking/atomic sections, and we removed the artificial
delays along with them. The only sleeps we kept are in `notifications.py`, where they simulate
actual email/audit I/O — the bug there was purely the inconsistent lock order.

---

## Extra hardening (beyond the planted bugs)

### Room stats survive a process restart
`app/services/stats.py` and `app/routers/rooms.py` (stats read path)

Not one of the deliberately-broken lines, but a real deviation from rule 14 ("room stats always
equal the values derivable from the bookings themselves"). Room stats were kept only in an
in-process dict, updated incrementally on each create/cancel. After a process restart the dict
starts empty while the database still holds every booking, so `GET /rooms/{id}/stats` reported
`0 / 0` for every room and disagreed with `GET /admin/usage-report` (which reads the DB). We
confirmed this live: create 3 bookings → stats show them → `docker compose restart` → stats show
zero.

**Fix:** `stats.get` now derives the count and revenue directly from the bookings table when a DB
session is available (the stats endpoint always has one), so the answer is correct no matter how
long the process has been running. In steady-state operation this returns exactly what the
in-memory counters returned — the incremental counters are updated after each booking is committed,
so committed-confirmed-bookings already equal the counter — so there is no observable change during
normal use, only correctness after a restart. We verified it holds for restart-then-read,
restart-then-create-then-read, and restart-then-cancel-then-read (a DB-authoritative read is
correct in all three; a cache-with-fallback approach would still be wrong for the create-first
ordering).
