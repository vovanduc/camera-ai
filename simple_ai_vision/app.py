import base64
import json
import logging
import os
import re
import tempfile
from typing import Any

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


OPTIONS_PATH = "/data/options.json"
DEFAULT_PROMPT = (
    "B\u1ea1n \u0111ang ph\u00e2n t\u00edch \u1ea3nh camera an ninh.\n"
    "Ch\u1ec9 m\u00f4 t\u1ea3 c\u00e1c s\u1ef1 ki\u1ec7n quan tr\u1ecdng li\u00ean quan \u0111\u1ebfn an ninh.\n"
    "N\u1ebfu kh\u00f4ng c\u00f3 g\u00ec quan tr\u1ecdng h\u00e3y tr\u1ea3 l\u1eddi NORMAL."
)
DEFAULT_KEYWORDS = ["person", "human", "stranger", "fire", "smoke", "ng\u01b0\u1eddi", "ch\u00e1y"]
CAMERA_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("simple-ai-vision")

app = FastAPI(title="Simple AI Vision", docs_url=None, redoc_url=None)


def error_response(message: str, status_code: int = 400, **extra: Any) -> JSONResponse:
    payload = {"success": False, "error": message}
    payload.update(extra)
    return JSONResponse(payload, status_code=status_code)


def load_options() -> dict[str, Any]:
    defaults = {
        "go2rtc_url": "",
        "ai_api_key": "",
        "ai_base_url": "https://api.openai.com/v1",
        "ai_model": "gpt-4o-mini",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "prompt": DEFAULT_PROMPT,
        "keyword_match": DEFAULT_KEYWORDS,
        "ai_timeout": 30,
        "snapshot_timeout": 10,
        "telegram_timeout": 10,
    }

    if os.path.exists(OPTIONS_PATH):
        with open(OPTIONS_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
        defaults.update(data)

    validate_options(defaults)
    return defaults


def validate_options(options: dict[str, Any]) -> None:
    required = [
        "go2rtc_url",
        "ai_api_key",
        "ai_base_url",
        "ai_model",
        "telegram_bot_token",
        "telegram_chat_id",
    ]
    missing = [key for key in required if not str(options.get(key, "")).strip()]
    if missing:
        raise ValueError(f"Missing required option(s): {', '.join(missing)}")

    if not isinstance(options.get("keyword_match"), list):
        raise ValueError("keyword_match must be a list")

    for key in ("ai_timeout", "snapshot_timeout", "telegram_timeout"):
        try:
            options[key] = int(options[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be an integer") from exc
        if options[key] < 1:
            raise ValueError(f"{key} must be greater than 0")


def validate_camera(camera: Any) -> str:
    if not isinstance(camera, str) or not camera.strip():
        raise ValueError("camera is required")

    camera = camera.strip()
    if not CAMERA_RE.fullmatch(camera):
        raise ValueError("invalid camera name")

    return camera


def fetch_snapshot(camera: str, options: dict[str, Any]) -> str:
    logger.info("Fetching snapshot for camera=%s", camera)
    base_url = options["go2rtc_url"].rstrip("/")
    url = f"{base_url}/api/frame.jpeg"

    response = requests.get(
        url,
        params={"src": camera},
        timeout=options["snapshot_timeout"],
    )
    response.raise_for_status()

    content_type = response.headers.get("content-type", "")
    if "image" not in content_type and not response.content.startswith(b"\xff\xd8"):
        raise ValueError("snapshot response is not a JPEG image")

    tmp = tempfile.NamedTemporaryFile(
        mode="wb",
        suffix=".jpg",
        prefix=f"simple_ai_vision_{camera}_",
        dir="/tmp",
        delete=False,
    )
    with tmp:
        tmp.write(response.content)

    return tmp.name


def image_to_data_url(path: str) -> str:
    with open(path, "rb") as file:
        encoded = base64.b64encode(file.read()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def call_ai(data_url: str, options: dict[str, Any]) -> str:
    logger.info("Sending AI vision request")
    url = f"{options['ai_base_url'].rstrip('/')}/chat/completions"
    payload = {
        "model": options["ai_model"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": options["prompt"]},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "temperature": 0.2,
    }
    headers = {
        "Authorization": f"Bearer {options['ai_api_key']}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=options["ai_timeout"],
    )
    response.raise_for_status()
    data = response.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("invalid AI API response") from exc

    if isinstance(content, list):
        text_parts = [part.get("text", "") for part in content if isinstance(part, dict)]
        content = "\n".join(part for part in text_parts if part)

    return str(content).strip()


def keyword_matched(analysis: str, keywords: list[Any]) -> bool:
    logger.info("Checking keyword match")
    for keyword in keywords:
        pattern = str(keyword).strip()
        if not pattern:
            continue
        try:
            if re.search(pattern, analysis, flags=re.IGNORECASE):
                return True
        except re.error:
            if pattern.lower() in analysis.lower():
                return True
    return False


def send_telegram(camera: str, analysis: str, photo_path: str, options: dict[str, Any]) -> None:
    logger.info("Sending Telegram photo")
    url = f"https://api.telegram.org/bot{options['telegram_bot_token']}/sendPhoto"
    caption = f"Camera: {camera}\n\n{analysis}"

    with open(photo_path, "rb") as photo:
        response = requests.post(
            url,
            data={
                "chat_id": options["telegram_chat_id"],
                "caption": caption[:1024],
            },
            files={"photo": photo},
            timeout=options["telegram_timeout"],
        )
    response.raise_for_status()


def cleanup_file(path: str | None) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            logger.warning("Could not remove temp file: %s", path)


@app.get("/health")
def health() -> dict[str, bool]:
    return {"success": True}


@app.post("/analyze")
async def analyze(request: Request) -> JSONResponse:
    snapshot_path = None
    try:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return error_response("invalid JSON", 400)

        if not isinstance(body, dict):
            return error_response("invalid JSON body", 400)

        options = load_options()
        camera = validate_camera(body.get("camera"))

        snapshot_path = fetch_snapshot(camera, options)
        data_url = image_to_data_url(snapshot_path)
        analysis = call_ai(data_url, options)
        matched = keyword_matched(analysis, options["keyword_match"])

        if matched:
            send_telegram(camera, analysis, snapshot_path, options)
            logger.info("Telegram sent for camera=%s", camera)
        else:
            logger.info("No keyword match for camera=%s", camera)

        return JSONResponse(
            {
                "success": True,
                "matched": matched,
                "analysis": analysis,
            }
        )

    except ValueError as exc:
        logger.error("%s", exc)
        return error_response(str(exc), 400)
    except requests.Timeout:
        logger.error("Network timeout")
        return error_response("network timeout", 504)
    except requests.RequestException as exc:
        logger.error("Network error: %s", exc)
        return error_response("network error", 502)
    except Exception as exc:
        logger.exception("Unexpected error")
        return error_response("internal error", 500)
    finally:
        cleanup_file(snapshot_path)
