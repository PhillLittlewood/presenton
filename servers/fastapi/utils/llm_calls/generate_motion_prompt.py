from typing import Optional

from llmai import get_client
from llmai.shared import JSONSchemaResponse, SystemMessage, UserMessage

from utils.llm_client_error_handler import handle_llm_client_exceptions
from utils.llm_config import get_llm_config
from utils.llm_provider import get_model
from utils.llm_utils import generate_structured_with_schema_retries

MOTION_PROMPT_SYSTEM_PROMPT = """
You write short motion prompts for an image-to-video model. The video starts
from an existing still image and animates it for a few seconds.

# Rules
- Describe only motion: subject movement, camera movement (slow push-in, gentle pan,
  parallax), and ambient movement (light, water, clouds, particles, fabric).
- Keep the motion subtle, smooth and natural; the result is used as a slide visual,
  so avoid cuts, scene changes, new subjects or text appearing.
- Preserve the look of the original image: do not restyle it.
- One or two sentences, at most 60 words, in English.
- Do not mention "video", "clip", or "animation" and do not use quotation marks.
""".strip()

MOTION_PROMPT_SCHEMA = {
    "type": "object",
    "properties": {
        "motion_prompt": {"type": "string", "minLength": 1, "maxLength": 600},
    },
    "required": ["motion_prompt"],
    "additionalProperties": False,
}


async def generate_motion_prompt(
    image_prompt: Optional[str],
    slide_context: Optional[str] = None,
) -> str:
    """
    Suggest a motion prompt for an image. Seeded from the image's stored
    generation prompt (ImageElement.prompt) rather than vision analysis.
    """
    client = get_client(config=get_llm_config())
    model = get_model()

    user_content = f"IMAGE DESCRIPTION: {(image_prompt or '').strip() or 'not available'}"
    if slide_context and slide_context.strip():
        user_content += f"\n\nSLIDE CONTEXT: {slide_context.strip()[:1000]}"

    try:
        response = await generate_structured_with_schema_retries(
            client,
            model,
            messages=[
                SystemMessage(content=MOTION_PROMPT_SYSTEM_PROMPT),
                UserMessage(content=user_content),
            ],
            response_format=JSONSchemaResponse(
                name="motion_prompt",
                json_schema=MOTION_PROMPT_SCHEMA,
                strict=False,
            ),
            json_schema=MOTION_PROMPT_SCHEMA,
            strict=False,
            validate_schema=True,
        )
    except Exception as e:
        raise handle_llm_client_exceptions(e)

    prompt = response.get("motion_prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return ""
    return " ".join(prompt.split())[:600]
