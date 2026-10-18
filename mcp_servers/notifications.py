"""ntfy messages and Hue light commands."""
import json, os, urllib.error, urllib.request, urllib.parse

def notify(result):
    url = os.environ.get("NTFY_URL","").strip()
    if not url:
        return
    ok = result["status"] == "observed_expected"
    if ok and os.environ.get("NOTIFY_SUCCESS","false").lower() != "true":
        return
    message = notification_text(result)
    headers = {"Title":"Chicken check: expected count observed" if ok else "Chicken check: please inspect",
               "Priority":"3" if ok else "4"}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["NTFY_TOKEN"]
    try:
        with urllib.request.urlopen(urllib.request.Request(url,message.encode(),headers),timeout=20):
            pass
    except (urllib.error.URLError,TimeoutError):
        raise RuntimeError("Notification delivery failed") from None


def hue_alert(result, force=False):
    if os.environ.get("HUE_ENABLED", "false").lower() != "true":
        return "disabled"
    if not force and result["status"] == "observed_expected":
        return "unchanged"
    hue_set_state({"on": True, "hue": 0, "sat": 254, "bri": 254, "alert": "lselect"})
    return "red_requested"


def hue_off():
    hue_set_state({"on": False, "alert": "none"})
    return "off_requested"


def hue_set_state(payload):
    base = os.environ.get("HUE_BRIDGE_URL", "").rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    key = os.environ.get("HUE_API_KEY", "")
    light = os.environ.get("HUE_LIGHT_ID", "")
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.path
            or parsed.query or parsed.fragment or not key or not light.isdigit()):
        raise RuntimeError("Hue configuration incomplete or invalid")
    url = (base + "/api/" + urllib.parse.quote(key, safe="")
           + "/lights/" + light + "/state")
    req = urllib.request.Request(url, json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"}, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            answer = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        raise RuntimeError("Hue Bridge request failed") from None
    # Hue v1 can report API errors even with HTTP 200.
    if not isinstance(answer, list) or not answer:
        raise RuntimeError("Unexpected Hue Bridge response")
    successes = {}
    for item in answer:
        if not isinstance(item, dict) or "error" in item or not isinstance(item.get("success"), dict):
            raise RuntimeError("Hue Bridge rejected the light update; check key, light ID and color support")
        successes.update(item["success"])
    if any(successes.get(f"/lights/{light}/state/{name}") != value
           for name, value in payload.items()):
        raise RuntimeError("Hue Bridge did not confirm all light settings")


def notification_text(result):
    lines = []
    for coop in result["coops"]:
        counts = "[" + ",".join(str(f["count"]) for f in coop["frames"]) + "]"
        label = f"[{coop['name']}]"
        expected = coop["expected"]
        if coop["status"] == "observed_expected":
            message = f"All {expected} accounted for."
        elif coop["status"] == "error":
            message = "Could not complete check. " + coop.get("error", "Please inspect the camera.")
        else:
            message = f"Please check: expected {expected}; counts do not match or the view is unclear."
        lines.append(f"{label}: {message} {counts}")
    return "\n".join(lines)
