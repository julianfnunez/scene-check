# Scene Watch: configuration and behavior reference

[README](../README.md)

## Source files

- `sensors.yaml`: `servers`, `sensors`, `actions`.
- `scenes.yaml`: `scenes`, optional `schedules`, and optional top-level `run` with action triggers.
- A single combined configuration file containing servers and sensors also works.
- Environment variables override `.env`; `.env` overrides `.env.defaults`. Restart after YAML changes.

## Scenes and runs

Each scene requires `name`, nonempty `sensors`, and nonempty `conditions`. `active` defaults
to `true`. Scene actions and nested scenes are no longer supported.

- Single scene: `POST /api/scenes/{scene_id}/checks`. This can run an inactive scene.
- All active scenes: `POST /api/checks/active`. It selects the active IDs at admission,
  keeping the run's membership stable even if configuration changes later.
- The scheduled equivalent uses `target: active_scenes`, or `scene: id` for just one scene.
- Each scene is evaluated independently; scene checks execute sequentially, with their
  own sensor sampling/evaluation plans. Nothing is reused from earlier runs.
- Scene results are saved under one run record in `scenes` with their own conditions,
  sensor assessments, timestamps, evidence and traces. A single run produces one history record.
- Run status is deterministic: any `needs_attention` wins; otherwise `unknown` wins;
  otherwise `normal`. The LLM **writes** the notification using all scene results;
  it does not override this status.

## Example configuration

```yaml
# scenes.yaml
schedules:
  nightly:
    target: active_scenes
    time: "21:15"
    timezone: America/New_York
    catch_up_minutes: 60

run:
  on:
    failure: [notify_failure]
    not_normal:
      instructions: >
        Turn Hue red, then send a notification explaining every affected scene.
      actions: [red_light, notify_me]
    normal:
      instructions: >
        Send one concise summary of what was confirmed across the run.
      actions: [notify_me]

scenes:
  girls:
    name: Girls
    active: true
    sensors:
      coop_camera:
        instructions: Take four pictures, ten seconds apart.
      girls_door:
        evaluations: 1
    conditions:
      - Three chickens are visible inside.
      - The door is closed.
  chicky:
    name: Chicky
    active: true
    sensors:
      camera_2:
        evaluations: 4
        interval_seconds: 10
    conditions:
      - One chicken is visible inside.
```

The bundled [examples](../scenes.example.yaml) and [sensor config](../sensors.example.yaml)
include working shapes for ntfy, Hue, cameras and YoLink (credentials and device IDs must
be supplied). If `failure` is configured, it is used for host-level execution/storage
failures rather than model-generated status assessments. A normal completed run with an
`unknown` scene uses the `unknown` and/or `not_normal` triggers instead.

## Observation planning and evaluation

A sampling plan may use `instructions` *or* numeric `evaluations` and
`interval_seconds`. Values are restricted to 1–12 independent evaluations and
intervals of 0–300 seconds. The model compiles natural-language sampling instructions
at startup; unsupported plans block execution with an error on the dashboard.

The host captures observations from stdio MCP tools first. Each tool receives its
configured arguments plus `sensor_id`. An observation must have the matching sensor
ID, an offset-aware timestamp `observed_at`, and either a `reading` or image. Camera
MCP tools return supported JPEG, PNG or WebP images alongside a structured metadata
envelope. Observation age does not invalidate a reading. Missing or malformed
observations and timestamps more than five seconds in the future yield unknown.

YoLink doors use the cloud-reported state when the response identifies the
assigned device as online. A door remaining
closed for hours does not require its state to change. `stateChangedAt` records
the last transition; `reportAt` records the last report and remains the observation
timestamp. Neither is replaced with the current time. This policy trusts YoLink's
cached state and online flag; querying the cloud does not establish a fresh
physical measurement.

The LLM evaluates each observation separately against the scene conditions and sensor
description. `not_applicable` means the sensor cannot assess that *type* of condition;
visibility and ambiguous readings yield `unknown`. The host validates condition order
and evidence IDs. Invalid assessments get one validation retry. Multiple readings of
one condition are aggregated: definite `not_met` wins, then `unknown`, then `met`.
Across different sensors a definite `not_met` wins, otherwise `met`, otherwise unknown.
The current evaluator does not support a separate joint-sensor interpretation stage.

## Logs and sensor diagnostics

The console logs run and scene starts/results, sensor collection and evaluation,
and action delivery outcomes. Successful dashboard polling requests and routine
MCP request messages are suppressed; HTTP errors and check submissions remain visible.

Collection failures include the scene, sensor ID, MCP server/tool, observation
number and failing stage (startup, discovery, invocation, or validation/storage).
Safe failure details are also retained in the run's tool activity. Invalid
timestamps, missing tools, timeouts and storage failures have distinct diagnostics. Failed captures
remain unknown and are never sent to the model as evidence.

The YoLink adapter logs authentication HTTP failures, API rejection codes,
missing/offline devices, and invalid reports at the stage where they occur.
Credentials, device IDs, tool arguments, vendor response bodies and raw connection
exception text are omitted. External MCP adapters should provide equivalent safe
diagnostics on stderr; their raw tool error bodies are not copied into host logs.

## Run actions and notifications

MCP actions are defined in `sensors.yaml`, but their triggers are configured in the
**top-level `run.on`**. Available triggers: `normal`, `needs_attention`, `unknown`,
`not_normal` (both abnormal states), and `failure` (emergency explicit action list).
One configured run-level policy applies to both manual and scheduled checks. One run
executes at most **one `publish` notification action**; duplicate publish actions
are rejected before performing any actions. Other actions, such as turning a Hue
light red, run sequentially and independently and do not depend on earlier success.

The `ActionInstructions` planner compiles free-form run action instructions at startup
into ordered MCP steps. Explicit action IDs are also supported. Only assigned actions
and their declared `editable_arguments` may be changed. All other arguments remain
fixed. Plans validate against the discovered tool input schemas before use.

Templates in actions use `{{ dotted.paths }}`; available paths are:

- `scene.id`, `scene.name`: compatibility aliases for the run's selected scene ID or
  `active_scenes`, and run display name.
- `run.id`, `run.name`, `run.scene_ids`, `run.status`: run identity and outcome.
- `check.id`, `check.created_at`: historical check aliases.
- `result.status`, `result.summary`, `result.conditions`, `result.scenes`,
  `result.details`, `result.plain_english`.

To have the model write the final notification **once per run** from all completed
scene results, configure the `publish` action's message as:

```yaml
message: '{{ result.plain_english }}'
```

The runtime forwards only the projected scene findings, conditions and summarized
sensor evaluations to the writer, not images, raw traces or secrets. The writer
cannot access tools. It must preserve uncertainties instead of inventing failed
counts or opposite conditions. The resulting message is cached for the run and
reused by any action that needs it; on writing failure, a deterministic factual
fallback is used. Generated text and its source are saved in the run history.

An action's `default_instructions` can specify its editable defaults. Run-specific
instructions override those defaults, but do not permit changing fixed arguments.
The planner may compile separate normal/abnormal plans. The dashboard exposes the
compiled run action plans for review. Actions persist an `uncertain` intent before
external execution; interrupted/unconfirmed actions are not retried blindly.

The `failure` trigger is designed for a separate, simple manual-check alert when a
run cannot be persisted or completed. It does not invoke the LLM writer. Failures
before publishing starts can send this alert. Once publishing has been attempted,
later action or storage failures never send a second notification, even if delivery
is unconfirmed. Unexpected scene exceptions and timeouts use the failure policy
after the remaining scenes finish; completed uncertain findings use `unknown` or
`not_normal` instead.

## Daily scheduling

A schedule has **exactly one** of `scene: id` or `target: active_scenes`, plus
`time: "HH:MM"`, a valid IANA `timezone`, optional `enabled` and
`catch_up_minutes` (1–1440, default 60). Polling runs every five seconds and shares
the same one-worker busy guard with manual runs and manual actions.

Scheduled occurrences are reserved on disk before execution, keyed by schedule ID
and local date. An interrupted or already attempted occurrence is not replayed after
restart. Catch-up windows permit a busy run to delay submission until the window
ends. DST fall-back uses the first occurrence; spring-forward gaps shift forward.

## API and dashboard

- `GET /` — dashboard, including **Check All Active Scenes** and per-scene buttons.
- `GET /api/scenes` — configs, active state, run plans, schedule state, busy state, and session token.
- `POST /api/checks/active` — one run for all active scenes.
- `POST /api/scenes/{scene_id}/checks` — one run for the chosen scene.
- `GET /api/checks` — last 50 records.
- `GET /api/checks/{id}` — one saved run or manual-action record.
- `POST /api/actions/{action_id}/runs` — explicit manual action, if marked `manual: true`.
- `GET /frames/{filename}` — saved frame (validated filename).

Check POSTs require `X-Check-Token` when any actions exist; manual-action POSTs
always require it. This protects against blind cross-origin form posts; it is **not**
a login system. Keep the dashboard on a trusted private network.

## Deployment, credentials, and reliability

Python 3.12+, uv, FFmpeg for camera capture. Local start:

```bash
uv sync --frozen
uv run web.py
uv run pytest -q
```

Environment settings: `OPENAI_API_KEY`, `OPENAI_MODEL`, `SCENES_FILE`, `SENSORS_FILE`,
`DATA_DIR`, `PORT`; adapter settings include camera RTSP URLs, YoLink credentials,
ntfy credentials and Hue bridge/light details. See `.env.example`.

LLM requests use `store=false`, bounded retry/backoff on transport/server errors,
and no credentials in application response bodies. Failed evaluations are recorded
as unknown. Results and image evidence persist beneath `DATA_DIR`. There is no
automatic retention; operate one app worker per data directory.

Docker deployment examples: [compose.yaml](../compose.yaml),
[compose.registry.yaml](../compose.registry.yaml). Publishing instructions:
[PUBLISH-GITHUB.md](../PUBLISH-GITHUB.md).
