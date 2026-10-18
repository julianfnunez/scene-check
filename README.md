# Scene Watch

Check whether a place is as expected using cameras and other sensors. Describe
normal conditions in plain English, run checks manually or on a daily schedule,
and send notifications or control devices based on the results.

Built-in adapters support RTSP cameras, YoLink door sensors, ntfy notifications,
and Philips Hue lights. The dashboard shows results, observations, and action outcomes.

## How it works

1. At startup, the LLM translates observation and action instructions into validated
   plans that you can review in the dashboard.
2. A check collects fresh observations through MCP tools, evaluates each observation
   independently, and combines the findings into `normal`, `needs_attention`, or `unknown`.
3. The app runs the scene's configured actions. Notification text can be written from
   the actual findings, following your scene-specific wording instructions.

Scenes can contain other scenes, such as an “All coops” check combining two coops.
Only the requested scene's actions run. Results and evidence are saved on disk.

## Configure

Start with the examples; keep your personal configuration outside Git:

```bash
cp sensors.example.yaml sensors.yaml
cp scenes.example.yaml scenes.yaml
```

| File | What to configure |
| --- | --- |
| [sensors.yaml example](sensors.example.yaml) | MCP servers, sensor tools, camera environment-variable names, door IDs, actions, and reusable action defaults. |
| [scenes.yaml example](scenes.example.yaml) | Assigned sensors or child scenes, observation instructions, normal conditions, action instructions, and schedules. |

For example, a scene's notification instructions can say “Keep it brief when
normal” or “List problems first.” Scene instructions override action defaults.
The supplied notification defaults request priority 3 for normal results and 4 for
abnormal or unknown results; inspect the compiled plan to verify the interpretation.

Daily schedules live alongside `scenes` in `scenes.yaml`:

```yaml
schedules:
  nightly_coop_check:
    scene: all_coops
    time: "21:15"
    timezone: America/New_York
    enabled: true
    catch_up_minutes: 60
```

A scheduled check waits if the app is busy, up to the catch-up deadline. Recorded
attempts are not repeated after a restart. Restart after changing configuration.

Provide credentials through environment variables. A local `.env` file is optional;
ZimaOS can supply everything through its container UI. See [.env.example](.env.example).

| Integration | Environment variables |
| --- | --- |
| OpenAI | `OPENAI_API_KEY`; optional `OPENAI_MODEL` (default `gpt-4.1`) |
| Cameras | `CAMERA_1_RTSP_URL`, `CAMERA_2_RTSP_URL`, or names assigned in `sensors.yaml` |
| YoLink | `YOLINK_UAID`, `YOLINK_SECRET_KEY` |
| ntfy | `NTFY_URL`; optional `NTFY_TOKEN` |
| Hue | `HUE_BRIDGE_URL`, `HUE_API_KEY`, `HUE_LIGHT_ID` |

Only configure integrations you use. Environment variables take precedence over
`.env` and [.env.defaults](.env.defaults). Planning, evaluations, and generated
notification messages use paid OpenAI API calls.

## Run

### Locally

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and FFmpeg for cameras.
After preparing your configuration and environment variables:

```bash
uv sync --frozen
uv run web.py
```

Open http://localhost:8081. Interactive REST documentation is available at `/docs`.
Run tests with `uv run pytest -q`; external services are mocked.

### ZimaOS / Docker

Use `ghcr.io/julianfnunez/scene-check:latest`, map port **8081 → 8081**, and set
credentials in the container's environment UI. No `.env` file is required.

| Host path (example) | Container path | Access |
| --- | --- | --- |
| `/DATA/AppData/scene-check/scenes.yaml` | `/app/scenes.yaml` | Read-only |
| `/DATA/AppData/scene-check/sensors.yaml` | `/app/sensors.yaml` | Read-only |
| `/DATA/AppData/scene-check/data` | `/data` | Read/write |

Create the host files and folder before starting the container. Its defaults already
use `/app/scenes.yaml`, `/app/sensors.yaml`, port 8081, and `DATA_DIR=/data`.
Open `http://ZIMA_IP:8081`. Restart after editing YAML; recreate the container after
changing environment variables. Pull and recreate to update the image.

Alternatively, [compose.registry.yaml](compose.registry.yaml) runs the published
image, and `docker compose up --build -d` builds from source. Both supplied Compose
files use `env_file: .env`; remove that entry and supply environment variables through
your deployment UI if you do not want an environment file.

Run one worker and one instance per data directory. Keep the dashboard on a trusted
local network: it has no login. History has no automatic cleanup. LLM observations
can be wrong; review the saved evidence when results look unexpected.

## Extend

Add a local stdio MCP server under `mcp_servers/` (or install an external server),
then register its command in `sensors.yaml`:

```yaml
servers:
  custom:
    command: python
    args: [-m, mcp_servers.custom_server]
```

- **Sensor:** register its tool, arguments, and description, then assign it to a scene.
  The tool receives `sensor_id` plus configured arguments and returns a timestamped
  JSON observation with a reading and/or MCP images.
- **Action:** register its tool and fixed arguments. Declare `editable_arguments`
  for values English instructions may change, and optionally `default_instructions`.
  Reference the action from a scene's `on` trigger.

Vendor logic belongs in the adapter; supported new tools need no core-app changes.
Include the adapter and its dependencies in the runtime image. Programs outside
`mcp_servers/` need an explicit Dockerfile copy entry.

See the [configuration and behavior reference](docs/reference.md) for observation
formats, templates, aggregation rules, scheduling details, and adapter settings.
See [PUBLISH-GITHUB.md](PUBLISH-GITHUB.md) for GitHub Actions and container publishing.
