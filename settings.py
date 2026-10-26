"""Deployment configuration and scene validation."""
import os
import re
from typing import Literal
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
load_dotenv(ROOT / '.env.defaults')

# YAML 1.2-style booleans: keep keys such as `on` and `off` as strings.
class ConfigLoader(yaml.SafeLoader):
    pass

ConfigLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in entries if tag != 'tag:yaml.org,2002:bool']
    for key, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
ConfigLoader.add_implicit_resolver('tag:yaml.org,2002:bool',
                                 re.compile(r'^(?:true|false|True|False|TRUE|FALSE)$'), list('tTfF'))

class Sensor(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    description: str = ''
    server: str = 'cameras'
    tool: str = 'capture_camera'
    arguments: dict = Field(default_factory=dict)
    max_age_seconds: float = Field(default=120, gt=0, le=86400)
    rtsp_env: str | None = None  # Legacy camera adapter configuration only.

class MCPServer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command: str = 'python'
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=45, gt=0, le=120)

class Sampling(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instructions: str | None = None
    evaluations: int = Field(default=1, ge=1, le=12, strict=True)
    interval_seconds: float = Field(default=0, ge=0, le=300)

    @model_validator(mode='after')
    def instruction_mode(self):
        if self.instructions is not None:
            if not self.instructions.strip():
                raise ValueError('Capture instructions cannot be blank')
            if {'evaluations', 'interval_seconds'} & self.model_fields_set:
                raise ValueError('Use instructions or numeric sampling settings, not both')
        return self

class Action(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    server: str
    tool: str
    arguments: dict = Field(default_factory=dict)
    manual: bool = False
    arguments_by_status: dict[Literal['normal', 'needs_attention', 'unknown'], dict] = Field(default_factory=dict)
    editable_arguments: list[str] = Field(default_factory=list)
    default_instructions: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def default_instruction_mode(self):
        if self.default_instructions is not None:
            if not self.default_instructions.strip():
                raise ValueError('Default action instructions cannot be blank')
            if self.manual:
                raise ValueError('Manual actions require explicit arguments; default instructions apply to scene triggers')
        return self

class ActionStep(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action_id: str
    arguments: dict = Field(default_factory=dict)
    writing_instructions: str = Field(default='', max_length=2000)

class ActionInstructions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    instructions: str = Field(min_length=1)
    actions: list[str] = Field(min_length=1)
    _steps: list[ActionStep] | None = PrivateAttr(default=None)
    _steps_by_status: dict[str, list[ActionStep]] = PrivateAttr(default_factory=dict)
    _explicit_order: list[str] | None = PrivateAttr(default=None)

    @model_validator(mode='after')
    def valid(self):
        if not self.instructions.strip() or len(set(self.actions)) != len(self.actions):
            raise ValueError('Action instructions must be nonblank and assigned actions distinct')
        return self

class Triggers(BaseModel):
    model_config = ConfigDict(extra='forbid')
    failure: list[str] = Field(default_factory=list)
    normal: list[str] | ActionInstructions = Field(default_factory=list)
    not_normal: list[str] | ActionInstructions = Field(default_factory=list)
    needs_attention: list[str] | ActionInstructions = Field(default_factory=list)
    unknown: list[str] | ActionInstructions = Field(default_factory=list)

class Scene(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    sensors: dict[str, Sampling] = Field(default_factory=dict)
    scenes: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(min_length=1)
    on: Triggers = Field(default_factory=Triggers)

    @model_validator(mode='after')
    def source(self):
        if bool(self.sensors) == bool(self.scenes):
            raise ValueError('A scene must define either sensors or child scenes')
        if len(self.scenes) != len(set(self.scenes)):
            raise ValueError('Duplicate child scenes')
        return self

class Schedule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scene: str
    time: str = Field(pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    timezone: str = 'America/New_York'
    enabled: bool = True
    catch_up_minutes: int = Field(default=60, ge=1, le=1440, strict=True)

    @model_validator(mode='after')
    def valid_timezone(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Schedule timezone must be a valid IANA timezone') from None
        return self


class Configuration(BaseModel):
    model_config = ConfigDict(extra='forbid')
    servers: dict[str, MCPServer] = Field(default_factory=lambda: {'cameras': MCPServer(args=['-m', 'mcp_servers.camera_server'])})
    sensors: dict[str, Sensor]
    scenes: dict[str, Scene]
    actions: dict[str, Action] = Field(default_factory=dict)
    schedules: dict[str, Schedule] = Field(default_factory=dict)

    @model_validator(mode='after')
    def references(self):
        for schedule in self.schedules.values():
            if schedule.scene not in self.scenes:
                raise ValueError('Schedule references an unknown scene')
        for action in self.actions.values():
            if action.server not in self.servers:
                raise ValueError('Action references an unknown MCP server')
        for scene in self.scenes.values():
            for trigger in type(scene.on).model_fields:
                rule = getattr(scene.on, trigger)
                ids = rule.actions if isinstance(rule, ActionInstructions) else rule
                if any(a not in self.actions for a in ids):
                    raise ValueError('Scene references an unknown action')
        for sensor in self.sensors.values():
            if sensor.server not in self.servers:
                raise ValueError("Sensor references an unknown MCP server")
        for scene in self.scenes.values():
            if len(set(scene.sensors)) != len(scene.sensors):
                raise ValueError('Duplicate sensors in scene')
            if any(s not in self.sensors for s in scene.sensors):
                raise ValueError('Scene references an unknown sensor')
            if any(not c.strip() for c in scene.conditions):
                raise ValueError('Conditions must not be blank')
        visited, active = set(), set()
        def visit(scene_id):
            if scene_id in active:
                raise ValueError('Circular scene reference: ' + scene_id)
            if scene_id in visited:
                return
            if scene_id not in self.scenes:
                raise ValueError('Unknown child scene: ' + scene_id)
            active.add(scene_id)
            for child in self.scenes[scene_id].scenes:
                visit(child)
            active.remove(scene_id)
            visited.add(scene_id)
        for scene_id in self.scenes:
            visit(scene_id)
        return self

def load_config():
    scenes = yaml.load(Path(os.environ['SCENES_FILE']).read_text(), Loader=ConfigLoader)
    # Keep standalone combined configurations working (including the demo files).
    if isinstance(scenes, dict) and ('servers' in scenes or 'sensors' in scenes):
        return Configuration.model_validate(scenes)
    if not isinstance(scenes, dict) or 'scenes' not in scenes or set(scenes) - {'scenes', 'schedules'}:
        raise ValueError('Scenes file must contain scenes and optionally schedules')
    sensors = yaml.load(Path(os.environ.get('SENSORS_FILE', ROOT / 'sensors.yaml')).read_text(), Loader=ConfigLoader)
    if not isinstance(sensors, dict) or set(sensors) - {'servers', 'sensors', 'actions'}:
        raise ValueError('Sensors file may contain only servers, sensors, and actions')
    return Configuration.model_validate({**sensors, **scenes})

def data_dir():
    path = Path(os.environ['DATA_DIR']).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path
