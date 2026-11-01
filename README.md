# Scene Watch

Check whether a place is as expected using cameras and other MCP sensors. Describe normal
conditions in plain English, run one scene or all active scenes, and receive one notification
per run. Built-in MCP adapters support RTSP cameras, YoLink door sensors, ntfy notifications,
and Philips Hue lights.

## How it works

1. At startup, the LLM compiles observation sampling instructions and run-level action instructions into validated plans.
2. A **run** selects one scene or snapshots the currently active scene IDs. Each scene collects and evaluates its own sensor observations independently.
3. The application combines scene statuses: a failed scene means `needs_attention`; otherwise any `unknown` means `unknown`; otherwise `normal`.
4. If the run has a configured notification action, the LLM reads the completed **scene results**, writes one grounded message, and the application sends **at most one `publish` notification** for the run. Optional device actions remain sequential and independent.

Parent scenes, child scene references, and per-scene action triggers have been removed. Scene definitions only configure `name`, `active`, `sensors`, and `conditions`. An inactive scene can still be checked manually.

## Configure

```bash
cp sensors.example.yaml sensors.yaml
cp scenes.example.yaml scenes.yaml
```

Keep private configuration out of Git; both `.env` and your copied YAML files are ignored.
Use `sensors.yaml` for MCP servers, sensors, actions, and reusable action defaults.
Use `scenes.yaml` for scenes, schedules, and a single top-level `run.on` policy:

```yaml
schedules:
  nightly:
    target: active_scenes
    time: "21:15"
    timezone: America/New_York

run:
  on:
    failure: [notify_failure]
    not_normal:
      instructions: >
        Turn Hue red and send one phone notification summarizing all scene results.
        Mention problems first, then what was confirmed.
      actions: [red_light, notify_me]
    normal:
      instructions: >
        Send one concise notification confirming the run results.
      actions: [notify_me]

scenes:
  girls:
    name: Girls
    active: true
    sensors:
      coop_camera:
        instructions: Take 4 captures, 10 seconds apart.
    conditions:
      - Three chickens are visible in the coop.
  chicky:
    name: Chicky
    active: true
    sensors:
      camera_2:
        evaluations: 1
    conditions:
      - One chicken is visible in the coop.
```

You may schedule a particular scene with `scene: girls` instead of `target: active_scenes`.
Restart the application after changing YAML.

`GET /api/scenes` provides the current scene list, active flags, run action plans and schedule.
`POST /api/checks/active` checks all active scenes, while
`POST /api/scenes/{scene_id}/checks` checks just that scene. Both use the same worker,
run-level actions and persisted history. A run has `scenes: [...]` containing the independent
results, `status`, `summary`, `plain_english` (if generated) and `actions`. The dashboard
has a **Check All Active Scenes** button and individual scene buttons.

### Environment

| Integration | Environment variables |
| --- | --- |
| OpenAI | `OPENAI_API_KEY`; optional `OPENAI_MODEL` (default `gpt-4.1`) |
| Cameras | `CAMERA_1_RTSP_URL`, `CAMERA_2_RTSP_URL`, or sensor-assigned names |
| YoLink | `YOLINK_UAID`, `YOLINK_SECRET_KEY` |
| ntfy | `NTFY_URL`; optional `NTFY_TOKEN` |
| Hue | `HUE_BRIDGE_URL`, `HUE_API_KEY`, `HUE_LIGHT_ID` |

Only configure integrations you actually use. Environment values override `.env` and `.env.defaults`.
Planning, evaluation, and LLM-written notifications incur OpenAI API usage.

## Run

### Locally

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and FFmpeg for cameras.

```bash
uv sync --frozen
uv run web.py
```

Open http://localhost:8081. API docs: `/docs`. Test with `uv run pytest -q`.

### ZimaOS / Docker

Use `ghcr.io/julianfnunez/scene-check:latest`, map port **8081 → 8081**, and set
credentials in the container environment UI. A `.env` file is not necessary.

| Host path (example) | Container path | Access |
| --- | --- | --- |
| `/DATA/AppData/scene-check/scenes.yaml` | `/app/scenes.yaml` | Read-only |
| `/DATA/AppData/scene-check/sensors.yaml` | `/app/sensors.yaml` | Read-only |
| `/DATA/AppData/scene-check/data` | `/data` | Read/write |

Create the files and directory before starting. Restart after changing YAML. To update
an image, pull and recreate the container. The included Compose files can also be used;
if you supply all secrets through the container UI, remove their `env_file: .env` entries.

Only run one worker/instance per data directory. The dashboard has no login and should
stay on a trusted network. History has no automatic cleanup. LLM observations may be
wrong; inspect the retained evidence when a result looks suspicious.

## Extend

Register custom stdio MCP servers in `sensors.yaml`. A sensor tool receives `sensor_id`
and configured arguments and returns timestamped structured readings and/or MCP images.
An action tool uses fixed arguments and can expose `editable_arguments` and
`default_instructions`. **Reference actions under `run.on`, never under a scene.**

For complete configuration behavior and the run-level template context, see the
[reference](docs/reference.md). For CI/image publishing, see [PUBLISH-GITHUB.md](PUBLISH-GITHUB.md).
