# Configuration and behavior reference

[Back to README](../README.md)

Define scenes, assign sensors, and describe normal in plain English. Run checks from a small dashboard and view scene conditions, sensor findings, individual evaluations, images, readings, and MCP tool activity.

Manual and daily scheduled checks with optional MCP actions for notifications and device control. Runs alongside Chicken Counter on port 8081.

## Run locally (WSL)

Requires uv and Python 3.12+. Camera sensors also require FFmpeg.

```bash
cp .env.example .env
cp scenes.example.yaml scenes.yaml
cp sensors.example.yaml sensors.yaml
# Add your OpenAI key and camera URL to .env.
uv sync --frozen
uv run web.py
```

Open http://localhost:8081. API documentation is at `/docs`.

The environment overrides `.env`, which overrides bundled `.env.defaults`. Defaults: `OPENAI_MODEL=gpt-4.1`, `PORT=8081`, `DATA_DIR=./data`, `SCENES_FILE=./scenes.yaml`, `SENSORS_FILE=./sensors.yaml`.

## Configuration

Define `servers` and `sensors` in `sensors.yaml` (`SENSORS_FILE`, also holds MCP actions), and `scenes` in `scenes.yaml` (`SCENES_FILE`). Sensor IDs connect the two files. Restart after editing either file. Legacy combined files, including the standalone examples, still work and do not load `SENSORS_FILE`.

`sensors.yaml`:

```yaml
servers:
  cameras:
    command: python
    args: [-m, mcp_servers.camera_server]
  yolink:
    command: python
    args: [-m, mcp_servers.yolink_server]

sensors:
  coop_camera:
    name: Coop camera
    description: Camera looking inside the coop; observes visible animals and objects.
    server: cameras
    tool: capture_camera
    arguments:
      rtsp_env: CAMERA_1_RTSP_URL
  coop_door:
    name: Coop door
    description: YoLink contact sensor reporting entrance door state; cannot observe animals.
    server: yolink
    tool: read_door
    arguments:
      device_id: YOUR_DEVICE_ID
    max_age_seconds: 120

```

`scenes.yaml`:

```yaml
scenes:
  coop:
    name: Coop
    sensors:
      coop_camera:
        instructions: Take 4 captures, 10 seconds apart. Evaluate each independently.
      coop_door:
        instructions: Read the current door state once.
    conditions:
      - Exactly three live chickens are visible inside the coop.
      - No dog is visible inside the coop.
      - The door is closed.
```

## Observation plans

At startup the LLM translates each distinct instruction into an observation count and interval. The dashboard shows the interpreted plan. Unsupported or ambiguous plans block checks and show an error. Edit YAML and restart to replan. Planning uses a paid API call per distinct instruction per startup.

Supported: 1–12 observations, intervals of 0–300 seconds between starts, independent evaluation. Numeric `evaluations` and `interval_seconds` remain available instead of `instructions`; do not mix the forms.

The host collects observations through MCP before AI assessment so model latency does not affect sampling. Intervals are approximate: slow sensor calls delay subsequent observations and calls never overlap. Sensors run sequentially. Each observation gets a separate LLM conversation with the scene conditions and sensor description; earlier results are not included.

## Adding a sensor

Implement an MCP tool, register its server, and configure the sensor's tool, arguments, and description. The host passes `sensor_id` plus configured `arguments`. Tool arguments must accept that signature. No host code changes are needed for supported observations.

This version supports **local stdio MCP servers**. `command: python` uses the application's Python environment; other commands execute directly, without a shell. Relative server arguments resolve from the application folder. Bundled adapters live in `mcp_servers/` and launch as Python modules (`args: [-m, mcp_servers.camera_server]`) so shared application imports resolve correctly. Server `env` optionally adds environment variables; use it for variable names/configuration and keep secrets in `.env`. The server inherits the app environment. `timeout_seconds` defaults to 45 (maximum 120).

A tool returns a JSON envelope as an MCP text block or `structuredContent`:

```json
{
  "sensor_id": "coop_door",
  "observed_at": "2026-10-14T19:48:00Z",
  "reading": {"state": "closed"}
}
```

Temperature example: `"reading": {"value": 72, "unit": "F"}`. Cameras return the envelope plus MCP image blocks. Supported image formats are JPEG, PNG, and WebP. A timestamp with timezone is required; it must represent when the value was observed, not merely when a cached reading was fetched. Use `max_age_seconds` per sensor (default 120) to control freshness.

The host verifies sensor identity, timestamps, and nonempty readings/images; creates evidence IDs; stores images; and supplies the observation to the evaluator. Raw unstructured text without an envelope is not supported. Missing tools, bad data, stale readings, and failed calls become unknown evaluations. Implementing the vendor API belongs entirely in the MCP adapter.

## Results

Conditions remain at scene level. Each sensor evaluates conditions it can address, marking unrelated conditions `not_applicable`. Poor visibility or missing evidence is `unknown`, not inapplicable.

Repeated findings aggregate conservatively: any failed condition wins, otherwise unknown wins. A sensor is ignored for a condition only when all its evaluations agree it is inapplicable. Across different sensors, any failed condition wins; otherwise any definite pass resolves unknown findings. If there is no definite pass or failure, the condition is unknown. Inapplicable findings are neutral. A condition no sensor can assess is unknown. Conditions requiring joint interpretation may remain unknown; this version has no separate joint-observation synthesis call.

Scene statuses: `normal`, `needs_attention`, `unknown`. Invalid model assessments are retried once against the same observation, with a reminder to preserve condition text and evidence IDs; this can add one paid model call. Persistent failures remain unknown and are logged with the sensor and evaluation number. The displayed failure distinguishes capture, validation, and evaluator request errors. Failed evaluations are retained. Images and readings go to OpenAI with `store=false`. Results and evidence persist under `DATA_DIR`; this pilot has no automatic history cleanup. Run one worker. The dashboard is for the local network and has no login.

All OpenAI requests (planning, sensor assessments, and parent assessments) allow up to three attempts for transport failures, HTTP 408/409/429, and HTTP 5xx. Retries use exponential backoff with jitter, honoring `retry-after-ms` or `Retry-After` when valid. Each request has a 90-second total budget including attempts and waits; a server delay exceeding the remaining budget stops the request. Authentication, other permanent HTTP errors, and known insufficient-quota/billing-limit errors are not retried. Invalid JSON and incomplete responses fail without transport retries. Retry logs omit request/response bodies and credentials. Transport retries can add paid requests when a response is lost after processing; the separate assessment-validation retry remains in place.

## Docker / ZimaOS

Repository: [julianfnunez/scene-check](https://github.com/julianfnunez/scene-check).
After the first successful GitHub publication, deploy the registry image with:

```bash
docker compose -f compose.registry.yaml pull
docker compose -f compose.registry.yaml up -d
```

Prepare `.env`, `scenes.yaml`, and `sensors.yaml` first using their example files.
The image uses placeholder configuration; your existing local YAML files are mounted
at runtime. Private packages require registry login; alternatively make the package
public. See [PUBLISH-GITHUB.md](../PUBLISH-GITHUB.md) for initial setup and updates.

To build from source:

```bash
docker compose up --build -d
```

Port 8081, persistent `/data`, and read-only `scenes.yaml` and `sensors.yaml` are configured in Compose. Include any custom server programs and their dependencies in the image. The GitHub workflow is configured to publish `ghcr.io/julianfnunez/scene-check` after tests and an image smoke test pass. See [GitHub publishing and registry deployment](../PUBLISH-GITHUB.md).

## Modules and REST

- `sensors.py`: generic MCP client, observation validation, collection and image storage
- `mcp_servers/camera_server.py`: RTSP/FFmpeg adapter
- `mcp_servers/yolink_server.py`: YoLink cloud door adapter
- `planner.py`: plain-English observation planning
- `agent.py`: independent assessments and aggregation
- `settings.py`: configuration
- `web.py`, `static/index.html`: jobs, persistence, REST and UI

Endpoints: `GET /api/scenes`, `POST /api/scenes/{id}/checks`, `GET /api/checks`, `GET /api/checks/{id}`. Checks return 202; concurrent checks return 409; invalid plans return 422.

## Tests

```bash
uv run pytest -q
```

Tests exercise real MCP subprocesses with test sensor fixtures and mocked model and YoLink responses. Live vendor and OpenAI calls require your configuration.

## YoLink door sensors

`mcp_servers/yolink_server.py` is a read-only cloud adapter for YoLink `DoorSensor` devices. It uses the existing HTTP client dependency; no additional package is required. It follows the [YoLink UAC flow](https://doc.yosmart.com/docs/overall/qsg_uac/) and [door sensor API](https://doc.yosmart.com/docs/yolinkapi/DoorSensor/).

1. Create User Access Credentials in the YoLink app under Account → Advanced Settings → Personal Access Credentials. Set `YOLINK_UAID` and `YOLINK_SECRET_KEY` in `.env`.
2. Run `uv run python -m mcp_servers.yolink_server --list-devices` to list door names and device IDs. Tokens and secrets are excluded from this output.
3. Set each door sensor's `arguments.device_id` in `sensors.yaml` to the desired device ID.
4. Restart `uv run web.py` to load the configuration.

To add another door, register it under `sensors.yaml` using the `yolink` server and `read_door` tool, then assign its sensor ID under the desired scene's `sensors` with `evaluations: 1`. Add `The coop door is closed.` as a separate scene condition and restart. The bundled scenes already include their door sensors.

The adapter discovers the device token internally, requests the latest reported state, and preserves `reportAt` as `observed_at`. `stateChangedAt` is included as metadata; an unchanged door is not necessarily stale. Reports older than `max_age_seconds` (120 in the example), missing timestamps, offline sensors, and API failures yield unknown evaluations. Set the age limit to suit your reporting cadence (up to 86400 seconds), understanding that this accepts older evidence; fetching a cloud value does not prove a new physical observation occurred. There is no MQTT listener or continuous monitoring.

OAuth access tokens are reused within an adapter client, renewed when expired, and retried once on HTTP 401. MCP reads use short-lived clients and keep credentials and device tokens out of observation evidence. For Docker, rebuild the image and merge the server and sensor definitions into the mounted `sensors.yaml` and the scenes into `scenes.yaml`; `.env` supplies the credentials through the existing Compose configuration.

## Scenes containing scenes

A parent scene uses `scenes` instead of `sensors`. For example, under the top-level `scenes` mapping:

```yaml
  all_coops:
    name: All coops
    scenes: [girls, chicky]
    conditions:
      - Scenes girls and chicky pass.
```

Checking the parent runs its children fresh, sequentially, then asks the model to assess the parent's plain-language conditions from their results. It does not reuse past dashboard checks. Nested groups are supported; a shared child is checked once per top-level check. Unknown references, duplicate children, circular references, and mixing sensors with child scenes are rejected.

For “girls and chicky pass”, both must be normal; any failure fails the condition, otherwise an unknown child leaves it unknown. This differs from combining sensors observing the same condition. Parent assessments add a paid model call per group. Child results are stored inside the parent check and shown under Child scenes; they are not separate history records. Parent checks can be manual or scheduled; observations are not simultaneous.

## MCP actions (Hue, ntfy, or your own tools)

Actions call arbitrary local stdio MCP tools after a check. Add a server and action
in `sensors.yaml`, then reference its ID in a scene's `on` mapping. No host code
changes are needed for another integration; install its adapter and dependencies
in the runtime/container. Actions use exactly the configured tool arguments and
do not receive an automatically injected `sensor_id`.

```yaml
# sensors.yaml (merge with existing servers/sensors)
servers:
  alerts:
    command: python
    args: [-m, mcp_servers.alert_server]
actions:
  red_light:
    name: Turn alert light red
    server: alerts
    tool: set_light
    arguments: {on: true, hue: 0, alert: lselect}
  notify_me:
    name: Notify me
    server: alerts
    tool: publish
    arguments:
      title: 'Scene Watch: {{ scene.name }}'
      message: "{{ result.summary }}\n{{ result.details }}"
  light_off:
    name: Turn alert light off
    server: alerts
    tool: set_light
    manual: true
    arguments: {on: false}
```

Inside a scene:

```yaml
on:
  not_normal: [red_light, notify_me]
  # normal: [success_notification]  # Define this action first, if wanted.
```

Triggers: `normal`, `needs_attention`, `unknown`, and `not_normal` (the last two
abnormal statuses). Status-specific actions run first, followed by `not_normal`
actions. Duplicate IDs in explicit lists execute once; compiled English sequences
preserve repeated steps and order. Every completed manual check triggers its
configured actions, including failed evaluations resulting in unknown. Successful
checks leave the light unchanged unless a `normal` action is configured.
Scheduling is configured separately. No cooldown, status-change deduplication, or recovery policy
is added. Interrupted checks do not start actions.

Only the explicitly requested scene's triggers run. Checking a parent does not run
its children's triggers. Checking a child directly runs that child's triggers.
The bundled `all_coops` scene sends one combined notification and requests a red
light on abnormal results. Individual coop checks have no alerts configured.

Templates support dotted dictionary fields inside `{{ ... }}`: `scene.id`,
`scene.name`, `check.id`, `check.created_at`, `result.status`, `result.summary`,
`result.conditions`, `result.children`, `result.details`, and `result.plain_english`. Details are readable
condition explanations and child-scene results. A whole-value placeholder preserves
its JSON type; embedded lists/objects become JSON text. No expressions, environment
lookups, or recursive expansion are supported. Unknown fields fail that action.
Manual actions have no scene/check context; configure literal arguments for them.
Configuration uses true/false booleans; YAML keys such as `on` remain strings.

Actions execute sequentially, each with the server's timeout covering startup,
discovery, and invocation. Each action is attempted once. Failure does not prevent
later actions and does not change the scene assessment. History and the dashboard
show each action's outcome. Tool output and arguments are not stored in outcomes.
An intent is saved before invocation; interrupted/unconfirmed work is never replayed
after restart. A timeout or lost response may follow an already-executed side effect;
check the destination before manually repeating it.

The dashboard exposes actions marked `manual: true`. REST:
`POST /api/actions/{action_id}/runs` returns a persistent job with HTTP 202;
`GET /api/checks/{id}` retrieves its outcome. Both manual actions and scene checks
share the single-worker busy guard. When actions are configured, scene-check POSTs
require `X-Check-Token` from `GET /api/scenes`; manual action POSTs always require it.
This prevents blind cross-origin form submissions; it is not a login or user
authorization system. Keep the dashboard on your trusted local network.

### Alert adapter credentials

Set these in `.env` (or container environment), then restart:

- `NTFY_URL`: full ntfy topic URL; optional `NTFY_TOKEN` bearer token.
- `HUE_BRIDGE_URL`, `HUE_API_KEY`, `HUE_LIGHT_ID`: configured Hue Bridge and light.

The adapter's `publish` tool accepts `message`, `title`, `priority` (1–5), and
`topic_env` (defaults to `NTFY_URL`). Its `set_light` tool accepts `on`, `hue`
(0–65535), `saturation` (0–254), `brightness` (1–254), and `alert`
(`none`, `select`, `lselect`). These are explicit actions, so the old
`HUE_ENABLED` and `NOTIFY_SUCCESS` switches are not used: remove the action from a
trigger to disable it, or add a `normal` trigger for success notifications.
Missing credentials appear as failed action outcomes. The adapter validates Hue
Bridge confirmations; ntfy success means the server accepted the request, not that
a phone displayed it. Real credentials/destinations are not exercised by tests.

Scene evaluation text and sensor readings cannot choose additional tools or override
the configured action plan.

### English action instructions

A trigger may contain English instructions and the action IDs it is allowed to use:

```yaml
on:
  not_normal:
    instructions: Turn Hue dark green, then send me a phone notification.
    actions: [red_light, notify_me]
```

The `red_light` action ID is the bundled Hue capability; English may change its
color because `editable_arguments` permits it. In `sensors.yaml`:

```yaml
actions:
  red_light:
    name: Turn alert light red
    server: alerts
    tool: set_light
    arguments: {on: true, hue: 0, alert: lselect}
    editable_arguments: [on, hue, saturation, brightness, alert]
```

The planner discovers MCP tool descriptions and input schemas at startup without
invoking action tools. It sends the instruction, assigned tool metadata, editable
argument defaults, and configured argument names to OpenAI with `store=false`.
Server environment variables and fixed argument values are not sent. Keep secrets
in adapter environment variables, not editable argument defaults or tool metadata.

English can select only assigned actions and override only their declared
`editable_arguments` (default: none). Other arguments, such as ntfy's destination,
remain fixed. Generated arguments are merged over YAML defaults, templates are
checked, and the result must match the discovered tool schema. Arguments are
validated again against the live schema immediately before execution. Domain
constraints should be expressed by the MCP adapter's schema and descriptions.
Hue's schema includes ranges; its description explains colors and brightness.

Each instruction compiles into 1–12 ordered steps, including repeated use of the
same action when requested. There are no planning-time side effects. Failed,
unsupported, or invalid plans block scene checks, with an error shown on the
dashboard. Successful plans are published only after every instruction validates.
The dashboard shows the original instruction and ordered argument overrides under
**Action plan**; `GET /api/scenes` exposes the same `action_plans` data. Interpretations
are model-generated, so review them after startup. The bundled abnormal alert
instruction retains red; the example above shows how to request dark green.

Planning adds a paid model request per compiled instruction/status plan at each startup, using the shared
bounded transient retries. Restart after editing instructions or adapters. Exact
arguments/order can still be specified using the original action-ID lists with no
English-planning call when those actions have no default instructions. Color words are approximate device settings, not calibrated
color matching.

Execution uses the compiled action steps. Templates using `result.plain_english` add a runtime writing call as described below. Steps run sequentially
and independently, so a failed light command still allows the notification. Instructions
requiring delays, schedules, loops, conditional branches, prior-step success, or tool
output are unsupported. Plain English is supported in scene-trigger YAML, not as an
interactive dashboard command or a manual-action definition. Scheduling is configured separately using explicit daily times.

### Plain-English result messages

To have the LLM explain the actual completed check in ordinary language, use:

```yaml
# In the notification action's arguments (the bundled notify_me now uses this):
message: "{{ result.plain_english }}"
```

English action instructions can also request this, for example:
`Turn Hue red, then send me a phone notification with the scene result in plain English.`
The startup planner maps that request to the template; it does not write a fictional
message before observations exist. The wording is generated after the check, just
before the first action that needs it, and reused by subsequent actions with the same writing instructions in that check.
Writing adds a paid model request using the shared bounded retries, with `store=false`.
Only projected scene/child-scene findings, condition explanations, and sensor assessment
results are sent to the writer; images, tool traces, action arguments, and credentials
are excluded. The writer cannot select or invoke tools or change action arguments.

The writer is instructed to name the relevant scenes, explain problems and confirmed
successes plainly, and preserve uncertainty. A failed expected-count condition alone
does not establish zero visible chickens. For example, explicit findings of an open
door and zero visible chickens can yield "Chicky's door is open and no chickens were
visible." An unclear image should instead yield "Chicky's door is open, but it was too
dark to confirm how many chickens were visible." The writer's phrasing is model-generated,
not a new measurement or a second assessment of the images.

The generated text and whether it came from the model or fallback are saved with the
check (`plain_english`, `report_source`); the dashboard shows the text under Actions.
If writing fails or returns unusable output, delivery still proceeds with a deterministic
report of conditions and their explanations. Cancellation stops delivery. Scene status
and earlier action outcomes are preserved, including when the light command fails.

### Action-level English defaults

Reusable behavior belongs in an action's `default_instructions`. The bundled
notification action now uses:

```yaml
notify_me:
  name: Send scene notification
  server: alerts
  tool: publish
  editable_arguments: [message, title, priority]
  default_instructions: >
    Write the scene result in plain English.
    Use priority 3 for normal results and priority 4 for abnormal or unknown results,
    unless the scene instructions specify otherwise.
  arguments:
    title: 'Scene Watch: {{ scene.name }}'
```

At startup, the planner combines the selected action's defaults with scene
instructions. Explicit scene instructions take precedence: for example, "send me a
notification at priority 5" overrides the usual priority. Defaults only configure
that action's editable arguments; they cannot introduce other tools or change fixed
destinations/device IDs. Unspecified arguments still use configured or tool defaults.

Status-dependent defaults compile into concrete plans for each applicable status.
A `not_normal` trigger with action defaults compiles separately for `needs_attention`
and `unknown`; the dashboard shows both plans with numeric arguments. Normal
notifications compile to priority 3; abnormal and unknown notifications compile to
priority 4. These are LLM interpretations to review in the displayed plans, rather
than rules hardcoded in the host. Execution selects the plan for the actual check
status without asking the model to choose actions or priority at runtime.

Actions with default instructions are also planned when referenced by an explicit
scene action-ID list. Lists without action defaults keep their existing direct
execution behavior. Manual actions require explicit arguments and cannot declare
`default_instructions` because they do not have a scene status or scene instructions.
Empty default instructions are rejected.

Planning adds one paid request per instruction/status plan at startup. Restart after
editing defaults or scene instructions. The plain-English message itself is still
written from completed findings at runtime through `{{ result.plain_english }}`.
Missing required arguments, non-editable overrides, or schema-invalid defaults block
checks, and no partial set of plans is published.

`arguments_by_status` remains supported for explicit configuration. Its merge order
is base arguments, per-status arguments, then compiled step overrides. The bundled
notification action no longer needs it or a numeric priority in YAML.

### Scene-specific notification wording

Writing preferences belong in the scene's notification instructions, for example:

```yaml
normal:
  instructions: >
    Send me a phone notification in plain English.
    Keep it brief: confirm all chickens are accounted for and the doors are closed,
    if the findings support that. Avoid listing each coop or repeating that nothing
    went wrong.
  actions: [notify_me]
```

The startup planner carries the writing preferences into each generated-report step's
`writing_instructions`, visible in the dashboard's Action plan. Scene wording takes
precedence over action defaults. A preference on `normal` does not affect abnormal
notifications, other scenes, or the shared notification action defaults.

At runtime, the writer receives these preferences separately from factual findings.
Preferences guide wording, not tool choice or scene status; grounding and uncertainty
rules still apply. Reports are cached per writing style within a check, so actions
with different wording instructions receive separately written messages. Each action
outcome retains its report, report source, and writing instructions. If writing fails,
the factual fallback still applies and may be longer than the requested style.
Restart after editing instructions to regenerate plans.

## Daily scheduled checks

The bundled configuration checks `all_coops` daily at **21:15 America/New_York**:

```yaml
# scenes.yaml (alongside scenes:)
schedules:
  nightly_coop_check:
    scene: all_coops
    time: "21:15"
    timezone: America/New_York
    enabled: true
    catch_up_minutes: 60
```

The scheduler runs as a FastAPI background task, polling every five seconds. It uses
exactly the same `CheckService.submit` path as REST scene checks, including plan
validation, the shared check/action busy guard, evaluations, notifications, and history.
It does not call HTTP endpoints or need the dashboard token. A scheduled check runs
fresh observations and the selected scene's existing actions; it does not reuse a
manual result. Run one app worker and one instance per data directory.

A schedule can start from its configured time until the end of its catch-up window
(exclusive). Starting the app at 21:45 catches up; at 22:15 it skips that day's check.
If another check or manual action is running, submission waits for a later scheduler
tick within the same window; a busy app beyond the deadline skips the occurrence.
Disabling a schedule prevents future submissions; it does not cancel a running check.
Plan errors block automatic submission just as they block REST checks.

Each submitted check atomically saves its schedule ID and scheduled local date before
execution. The saved check is also the durable once-per-date claim, including failed,
cancelled, or interrupted work. Restarting does not repeat that attempt. A manual
check does not claim the scheduled occurrence. Keep schedule IDs stable: renaming an
ID or deleting its saved records removes that identity's duplicate protection.
There is no automatic retry after a submitted check fails; unknown/abnormal actions
still run according to existing check behavior. No catch-up of dates outside the
configured window occurs. Windows may cross midnight; they allow 1–1440 minutes.

Times use IANA timezones independently of the host timezone, including daylight
saving. A repeated fall-back time uses its first occurrence and runs at most once
for that date. A nonexistent spring-forward time shifts forward by the DST gap
(e.g. 02:30 becomes 03:30). Catch-up windows measure elapsed minutes in UTC.
The Docker image includes timezone data; rebuild the image when updating.

The dashboard displays schedules on their scene cards and labels history as manual
or scheduled. `GET /api/scenes` includes `schedules`; check records contain `source`,
`schedule_id`, `scheduled_date`, and `scheduled_for` (UTC) for scheduled submissions.
Existing REST clients can continue using the same check endpoint. Edit YAML and
restart to change schedules. Only daily explicit-time schedules are supported.

## Diagnosing failed actions

Failed action records include `error_stage`, such as MCP initialization, tool
argument validation, or tool invocation. The host logs the stage and exception
type without printing arguments, tool error contents, or vendor responses.
The ntfy adapter logs HTTP rejection codes and distinguishes DNS, TLS, and timeout
failures. These diagnostics exclude URLs, bearer tokens, and notification text.
After updating the container, run a fresh check and inspect its container logs;
previous failure records cannot recover details that were not logged. Unconfirmed
actions are still not automatically retried.

### Check execution failures

Configure `on.failure: [notify_failure]` on a scene to request a manual check if
execution fails, including before a check can be saved. These actions require
explicit `arguments` in the sensors YAML; they do not use LLM planning or writing.
`result.summary` contains a plain-English request to check manually. See the example
`notify_failure` action. Unknown observations still use `unknown` / `not_normal`.

Failure actions are best effort, run without history writes, and are not retried.
Scheduled failures attempt one alert per schedule and date, including across
submission retries and a later execution failure for that occurrence. Each manual
check is a separate attempt. Deduplication before history can be saved is in memory
and resets on restart. An unavailable
notification service or stopped application cannot deliver an alert.
