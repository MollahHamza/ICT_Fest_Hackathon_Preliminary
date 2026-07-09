# Bug Report — CoWork: Multi-Tenant Coworking Space Booking API

All file/line references point to the **original (unfixed)** code. Every fix was verified
black-box against a running server: a 106-check sequential suite covering business rules 1–15,
a 19-check concurrency suite (races, uniqueness, liveness), and a 6-check concurrent-registration
suite, plus the repository's own smoke test.

---

## Authentication & tokens

### 1. Access tokens lived 900 minutes instead of 900 seconds
- **File/line:** `app/auth.py:50`
- **Bug:** `lifetime = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)`. The config value is
  15 (minutes); multiplying by 60 while still passing it as `minutes=` yields 900 *minutes*
  (15 hours). Rule 8 requires access tokens to expire in exactly 900 seconds.
- **Fix:** `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)` → `exp - iat == 900`.

### 2. Logout never actually revoked the token
- **File/line:** `app/auth.py:86` (store) vs `app/auth.py:97` (check)
- **Bug:** `revoke_access_token` stores the token's `jti` in the revoked set, but
  `get_token_payload` checked `payload.get("sub")` (the user id) against that set. A user id never
  equals a uuid jti, so a logged-out access token kept working forever (rule 8: subsequent use
  must yield 401).
- **Fix:** the check now reads `payload.get("jti") in _revoked_tokens`.

### 3. Refresh tokens were not single-use
- **File/line:** `app/routers/auth.py:81–93` (plus support added in `app/auth.py`)
- **Bug:** `/auth/refresh` rotated tokens but never invalidated the presented refresh token, so it
  could be replayed indefinitely. Rule 8: refresh tokens are single-use; reuse → 401.
- **Fix:** added a used-jti set (`_used_refresh_jtis`) guarded by a lock in `app/auth.py`
  (`consume_refresh_token`), called from the refresh endpoint. First use succeeds and records the
  jti; any reuse → `401`. The lock makes the check-and-record atomic so two concurrent refreshes
  with the same token cannot both succeed.

### 4. Duplicate registration returned the existing user instead of 409
- **File/line:** `app/routers/auth.py:37–43`
- **Bug:** registering a username that already existed in the org returned `201` with the
  **existing** user's id/role instead of failing. Rule 15 requires `409 USERNAME_TAKEN`.
- **Fix:** raises `AppError(409, "USERNAME_TAKEN", ...)`.

### 5. Concurrent registration could 500 (hardening)
- **File/line:** `app/routers/auth.py:26–30, 45–53`
- **Bug:** org creation and user creation used plain check-then-insert. Two concurrent registers
  for the same new org name (or the same username) raced the unique constraints and one request
  crashed with an unhandled `IntegrityError` → HTTP 500, which is outside the error contract.
- **Fix:** both commits catch `IntegrityError`; the org race falls back to joining the
  now-existing org as `member`, the username race returns `409 USERNAME_TAKEN`.

---

## Datetime handling

### 6. UTC offsets were discarded instead of converted
- **File/line:** `app/timeutils.py:12–13`
- **Bug:** `dt.replace(tzinfo=None)` strips the offset but keeps the wall-clock time, so
  `2026-07-10T10:00:00+06:00` was stored as `10:00` instead of `04:00` UTC. Rule 1 requires
  conversion to UTC before storage/comparison. This corrupted conflict checks, quota windows,
  refund-notice calculations and all stored times for offset inputs.
- **Fix:** `dt.astimezone(timezone.utc).replace(tzinfo=None)`.

---

## Booking creation (`app/routers/bookings.py`)

### 7. 300-second grace window for past start times
- **File/line:** `app/routers/bookings.py:86`
- **Bug:** `if start <= now - timedelta(seconds=300)` allowed bookings starting up to 5 minutes in
  the past. Rule 2: start must be **strictly** in the future, explicitly "no grace window".
- **Fix:** `if start <= now: raise INVALID_BOOKING_WINDOW`.

### 8. No minimum-duration / end-after-start validation
- **File/line:** `app/routers/bookings.py:89–94`
- **Bug:** only `duration > 8h` was rejected. `end == start` (0 hours) passed, and even
  `end < start` with a whole-hour negative duration passed every check, creating a booking with a
  **negative price**. Rule 2: duration min 1, max 8; end strictly after start.
- **Fix:** added `end <= start → 400` and `duration < 1 → 400` (code `INVALID_BOOKING_WINDOW`).

### 9. Back-to-back bookings were rejected as conflicts
- **File/line:** `app/routers/bookings.py:50`
- **Bug:** the overlap test used inclusive comparisons
  (`b.start_time <= end and start <= b.end_time`), so a booking ending exactly when another starts
  was treated as a conflict. Rule 3 defines overlap as `existing.start < new.end AND
  new.start < existing.end` and explicitly allows back-to-back bookings.
- **Fix:** strict inequalities: `b.start_time < end and start < b.end_time`.

### 10. Double-booking race (check-then-insert) — concurrency
- **File/line:** `app/routers/bookings.py:100–118`, race widened by the planted
  `_pricing_warmup()` sleep at lines 27–29/48
- **Bug:** the conflict check and the insert were not atomic. Two concurrent requests for the same
  slot both saw "no conflict" (the 120 ms sleep between check and insert made this reliable) and
  both committed. Rule 3 must hold under concurrent requests.
- **Fix:** a module-level `threading.Lock` (`_booking_write_lock`) serializes the
  conflict-check + quota-check + insert + commit critical section (single-process deployment, so a
  process lock is sufficient). The artificial sleep — a no-op that existed only to widen the race
  window — was removed. Verified: 8 parallel requests for one slot → exactly one `201`, seven
  `409 ROOM_CONFLICT`.

### 11. Quota race (check-then-insert) — concurrency
- **File/line:** `app/routers/bookings.py:55–71` (race widened by `_quota_audit()` sleep at 32–34/69)
- **Bug:** same TOCTOU pattern for the 3-bookings-per-24h quota (rule 4): concurrent requests all
  counted the same snapshot and all passed.
- **Fix:** the quota check runs inside the same `_booking_write_lock` critical section; sleep
  removed. Verified: 6 parallel creates → exactly three `201`, three `409 QUOTA_EXCEEDED`.

### 12. Stale caches: report not invalidated on create, availability not invalidated on cancel
- **File/line:** `app/routers/bookings.py:120–122` (create) and `215–218` (cancel)
- **Bug:** creating a booking invalidated only the availability cache, never the usage-report
  cache — so `/admin/usage-report` served stale counts after new bookings (violates rule 12
  "must reflect the current state immediately"). Symmetrically, cancelling invalidated only the
  report cache, never the availability cache — so `/rooms/{id}/availability` kept showing
  cancelled bookings as busy (violates rule 13).
- **Fix:** create now also calls `cache.invalidate_report(org_id)`; cancel now also calls
  `cache.invalidate_availability(room_id, start_date)`.

---

## Booking listing & detail (`app/routers/bookings.py`)

### 13. Pagination: wrong order, wrong offset, hardcoded page size
- **File/line:** `app/routers/bookings.py:136–140`
- **Bug (three defects, rule 11):**
  1. `order_by(Booking.start_time.desc(), ...)` — spec requires **ascending** by `start_time`;
  2. `.offset(page * limit)` — page 1 skipped the first `limit` items entirely (off-by-one;
     must be `(page - 1) * limit`);
  3. `.limit(10)` — the caller's `limit` parameter was ignored.
- **Fix:** `order_by(start_time.asc(), id.asc()).offset((page - 1) * limit).limit(limit)`.
  Verified: sequential pages never skip or repeat items.

### 14. Members could read other members' bookings
- **File/line:** `app/routers/bookings.py:156–163`
- **Bug:** `GET /bookings/{id}` checked only that the booking belonged to the caller's org — any
  member could read any booking in the org. Rule 10: members may read only their own bookings;
  another member's booking id → `404 BOOKING_NOT_FOUND`. (The cancel endpoint had this ownership
  check; the read endpoint was missing it.)
- **Fix:** added the same ownership check: non-admin callers who don't own the booking get `404`.

### 15. Booking detail returned `created_at` as `start_time`
- **File/line:** `app/routers/bookings.py:166`
- **Bug:** `response["start_time"] = iso_utc(booking.created_at)` overwrote the correct
  `start_time` (set by the serializer) with the booking's creation timestamp.
- **Fix:** removed the line.

---

## Cancellation & refunds

### 16. Refund tiers wrong at both boundaries
- **File/line:** `app/routers/bookings.py:198–206`
- **Bug (rule 6):**
  1. `notice_hours = int(notice.total_seconds() // 3600)` then `if notice_hours > 48` — a
     cancellation with **exactly 48 hours** notice floors to 48, fails `> 48`, and got 50% instead
     of 100%;
  2. the final `else` branch returned **50** instead of **0**, so cancellations with less than
     24 hours notice were refunded 50%.
- **Fix:** compare the timedelta directly: `notice >= timedelta(hours=48) → 100`,
  `notice >= timedelta(hours=24) → 50`, else `0`.

### 17. Refund amount: wrong rounding, and response could disagree with the RefundLog
- **File/line:** `app/services/refunds.py:14–17` and `app/routers/bookings.py:208`
- **Bug:** the ledger computed the amount via floats and `int(...)` (truncation), while the
  response computed it independently with `round(...)` (banker's rounding, half-to-even). Neither
  implements the spec's "half-cents round up", and the two could disagree — e.g. 3333¢ at 50% is
  1666.5¢: spec says 1667, both implementations produced 1666. Rule 6 also requires the cancel
  response amount to equal the stored RefundLog amount.
- **Fix:** `log_refund` computes `amount_cents = (price_cents * percent + 50) // 100` (pure
  integer round-half-up), and the router takes `refund_amount_cents` from the returned RefundLog
  entry, so the two are the same value by construction.

### 18. Concurrent cancels double-refunded — concurrency
- **File/line:** `app/routers/bookings.py:195–214` (race widened by `_settlement_pause()` sleep at
  37–39/212)
- **Bug:** status check → 120 ms sleep → status flip → commit. Two parallel cancels of the same
  booking both saw `confirmed`, both wrote a RefundLog and both returned `200`. Also, the refund
  was committed *before* the status flip, so a crash in between could refund without cancelling.
  Rule 6: exactly one RefundLog, concurrent-safe; second cancel → `409 ALREADY_CANCELLED`.
- **Fix:** the cancel critical section runs under the same `_booking_write_lock`: it re-reads the
  booking's fresh status (`db.expire`), rejects if already cancelled, then flips the status and
  writes the RefundLog in **one** commit (atomic). Sleep removed. Verified: 8 parallel cancels →
  one `200`, seven `409 ALREADY_CANCELLED`, exactly one RefundLog whose amount equals the response.

---

## Services

### 19. Deadlock between create/cancel notifications — concurrency/liveness
- **File/line:** `app/services/notifications.py:24–35`
- **Bug:** classic ABBA deadlock. `notify_created` acquired `_email_lock` → `_audit_lock`;
  `notify_cancelled` acquired `_audit_lock` → `_email_lock`. One concurrent create + cancel could
  each grab their first lock and wait forever on the other — and since every subsequent
  booking/cancel also needs those locks, the whole service wedged (violates rule 16, liveness).
  The `time.sleep` calls inside made the interleaving easy to hit.
- **Fix:** both functions acquire the locks in the same order (email → audit). Consistent lock
  ordering makes deadlock impossible. Verified with a 12-thread create+cancel storm: all requests
  complete, `/health` stays responsive.

### 20. Duplicate reference codes under concurrency
- **File/line:** `app/services/reference.py:17–21`
- **Bug:** `next_reference_code` read the counter, slept 120 ms (planted `_format_pause`), then
  wrote back `current + 1`. Concurrent creators read the same value and were issued the **same**
  reference code, violating rule 7 (unique, including under concurrent creation).
- **Fix:** the read-and-increment is atomic under a `threading.Lock`; the artificial sleep was
  removed. Verified: 10 parallel creates → 10 distinct codes.

### 21. Rate limiter lost entries under concurrency
- **File/line:** `app/services/ratelimit.py:18–26`
- **Bug:** each request copied the user's bucket, slept 100 ms (planted `_settle_pause`), appended
  its own timestamp and wrote the copy back. Parallel requests each rebuilt the bucket from the
  same snapshot and overwrote each other's appends, so far more than 20 requests/minute got
  through (rule 5 must hold under concurrent requests).
- **Fix:** trim + append + count run atomically under a `threading.Lock`; sleep removed. Verified:
  30 parallel `POST /bookings` from one user → exactly 20 processed, 10 × `429 RATE_LIMITED`.

### 22. Room stats lost updates under concurrency
- **File/line:** `app/services/stats.py:15–26`
- **Bug:** `record_create`/`record_cancel` read the current counters, slept 100 ms (planted
  `_aggregate_pause`), then wrote back the incremented dict. Concurrent bookings overwrote each
  other's updates, so `/rooms/{id}/stats` drifted away from the actual bookings (rule 14 requires
  consistency, "including after bursts of concurrent activity").
- **Fix:** both updates (and the read) are atomic under a `threading.Lock`; sleep removed.
  Verified: stats match the booking table exactly after parallel create/cancel bursts.

### 23. CSV export leaked other organizations' bookings
- **File/line:** `app/services/export.py:48–52` (`generate_export` calling `fetch_bookings_raw`)
- **Bug:** with `include_all=true&room_id=<id>` the export used `fetch_bookings_raw`, which
  filters **only by room id with no org check**. An admin of org A could pass a room id belonging
  to org B and download org B's entire booking history — a multi-tenancy breach (rule 9:
  cross-org resource IDs must behave as non-existent).
- **Fix:** that branch now goes through `_fetch_scoped`, which always applies the
  `Room.org_id == org_id` filter (room filter applied on top). Verified: cross-org room id
  exports only the CSV header.

---

## Note on the removed `time.sleep` calls

The codebase contained several no-op "warmup / audit / settle / format / aggregate" helpers that
only called `time.sleep(...)`. They performed no work; their purpose was to widen the race windows
of bugs 10, 11, 18, 20, 21 and 22 so the races are observable. The actual defects were the
non-atomic check-then-act sequences, which are now protected by proper synchronization; the
artificial delays were removed together with those fixes. The sleeps in
`app/services/notifications.py` were kept, since they simulate real I/O (email/audit-log) — there
the defect was purely the inconsistent lock order.
