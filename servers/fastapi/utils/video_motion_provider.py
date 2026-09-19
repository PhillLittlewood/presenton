from enums.video_motion_provider import VideoMotionProvider
from utils.get_env import (
    get_comfyui_motion_url_env,
    get_comfyui_motion_workflow_env,
    get_disable_motion_video_env,
    get_motion_video_provider_env,
)
from utils.parsers import parse_bool_or_none


def is_motion_video_disabled() -> bool:
    return parse_bool_or_none(get_disable_motion_video_env()) or False


def get_selected_motion_video_provider() -> VideoMotionProvider | None:
    """
    Mirrors get_selected_video_narration_provider(). ComfyUI is currently the
    only option; more providers can be added here as they're supported.
    """
    provider_env = get_motion_video_provider_env()
    if provider_env:
        return VideoMotionProvider(provider_env)
    return None


def is_comfyui_motion_selected() -> bool:
    selected = get_selected_motion_video_provider()
    # Same rule as narration: configured-but-unselected counts as ComfyUI.
    if selected is None:
        return bool(get_comfyui_motion_url_env()) and bool(
            get_comfyui_motion_workflow_env()
        )
    return selected == VideoMotionProvider.COMFYUI


def is_motion_video_configured() -> bool:
    return bool(get_comfyui_motion_url_env()) and bool(
        get_comfyui_motion_workflow_env()
    )
