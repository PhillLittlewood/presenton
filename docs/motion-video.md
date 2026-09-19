# AI motion clips for slide images (ComfyUI LTX)

Replace any slide image with a short image-to-video clip **in the final video export only**.
The still image stays the visual everywhere else (editor canvas, PDF, PPTX).

## Setup
Settings → **Motion Video** (or onboarding step 5), or env vars:

| Variable | Meaning |
| --- | --- |
| `COMFYUI_MOTION_URL` | ComfyUI server; falls back to `COMFYUI_URL` |
| `COMFYUI_MOTION_WORKFLOW` | LTX image-to-video workflow, API format |
| `DISABLE_MOTION_VIDEO` | Turns the feature off (export then ignores existing clips) |
| `VIDEO_MOTION_MAX_CONCURRENCY` | Simultaneous generations, default `1` (image gen, TTS and LTX share one GPU) |

Workflow node titles: **`Input Image`** (required; a LoadImage-style node that receives the
slide image as the start frame) and **`Motion Prompt`** (optional text node).
The workflow must contain a save-video node that writes an `.mp4`/`.webm` output.

## Use
Select an image → **Film** button in the image toolbar → review/edit the suggested motion
prompt (seeded from the image's stored prompt) → Generate. Generation is an async task
(`image.generate_motion_clip`); a badge on the image opens the clip in a new tab.

## Export behaviour
* **Video:** the clip is composited over the rasterized slide at the image's position for the
  narration's duration. Shorter clips fade out (0.5 s) to the static image; longer ones are
  truncated. Slides with no narration run for the clip's length. Failures fall back to the
  static image for that slide.
* **PDF:** clips are ignored.
* **PPTX:** the bundled export runtime can't embed video, so the .pptx keeps static images and
  the clips download as `<title>-motion-clips.zip` (`slide-NN.mp4`) beside it (browser export only).

## Limits
Clips are only composited for images that are not rotated, have no `clip_path`, and are not
inside a flex/grid layout (the editor computes those positions at render time). Others fall
back to the static image and a warning is logged.
