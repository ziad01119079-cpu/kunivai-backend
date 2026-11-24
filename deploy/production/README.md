# Oracle production release

## Release policy

- Run one async worker. Serve port 8080 on the host loopback through the existing Caddy TLS configuration.
- Set `KUNIV_SCALABLE=1`, `COORDINATION_BACKEND=redis`, `AI_GLOBAL_GENERATION_RPM=30`, and `AI_USER_RATE_LIMIT_REQUESTS=10`. The first limit covers all authenticated chat requests over a rolling minute, including reconnects, and is checked together with the user limit before opening an SSE connection.
- Keep Firebase Admin, OpenAI model and vector-store credentials from production. Set explicit origins for the Firebase-hosted and custom web clients. Keep App Check off for the existing Flutter release.
- Put Redis and the API on a dedicated Docker bridge, publish no Redis port, and back Redis with a dedicated AOF volume. Use authentication and noeviction. Seed the persistent epoch key with `<epoch>:<run_id>` after confirming the primary identity.
- An absent or changed Redis primary causes readiness and new admissions to fail closed. A Redis restart requires inspecting recovery and then deliberately re-pinning the marker. If provider budget counters are lost, wait the rolling window and preserve or explicitly reset the daily budget before reopening.
- A first request from an older Flutter build may omit the message ID. The server assigns an ID and returns `X-Client-Message-ID`. A replay cursor requires its message ID. Older clients cannot reliably coalesce separate manually repeated requests because they cannot retain that ID. UUID-enabled clients get durable generation ownership.
- Firestore claims are additive. No migration or deletion of existing conversations is required. Drain the old writer before switching.

## Image and cutover

Export the exact committed revision with `git archive`. Resolve and preserve an ARM64/Python 3.11 lock using `uv pip compile` and install its wheel versions with `uv pip install --no-build --python-version 3.11 --python-platform aarch64-manylinux_2_28 --target target -r requirements-arm64.lock`. Build the target context with this Dockerfile, `--platform linux/arm64`, and `--build-arg REVISION=<git-sha>`. Include only the application and built dependencies. Verify the archive checksum, image architecture and revision before switching.

Save Docker inspect output and sensitive environment in a private server-side release directory. Pre-create the replacement, verify dependency connectivity and rollback commands, and arm a bounded rollback timer. Stop the old API, retain it under a rollback name, and start the replacement with the existing loopback binding. Verify startup before the single authenticated canary and one replay of its stable ID. Cancel rollback only after all checks pass.

For rollback, stop the candidate, preserve it under a separate name, restore the exact old container name and start it. Recheck loopback and public health. Keep the Redis volume and Firestore claims for investigation. Stop accepting candidate requests before opening traffic on the earlier image.
