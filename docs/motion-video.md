# Motion clips for slide images (ComfyUI LTX or your own video)

Replace any slide image with a short motion clip **in the final video export only** -- either
generated from the image with ComfyUI LTX, or a video file you already have. The still image
stays the visual everywhere else (editor canvas, PDF, PPTX).

## Setup
Settings → **Motion Video** (or onboarding step 5), or env vars:

| Variable | Meaning |
| --- | --- |
| `COMFYUI_MOTION_URL` | ComfyUI server; falls back to `COMFYUI_URL` |
| `COMFYUI_MOTION_WORKFLOW` | LTX image-to-video workflow, API format |
| `DISABLE_MOTION_VIDEO` | Turns the feature off (export then ignores existing clips) |
| `VIDEO_MOTION_MAX_CONCURRENCY` | Simultaneous generations, default `1` (image gen, TTS and LTX share one GPU) |

Workflow node titles (same "Input Prompt" convention as image generation and narration):

| Title | Type | Required | Filled with |
| --- | --- | --- | --- |
| `Load Image` | LoadImage | yes | the slide image (LTX start frame) |
| `Input Prompt` | text/string | no | the motion prompt |
| `Width`, `Height` | int | no | pixel size chosen from the image's aspect ratio (~720p worth of pixels, multiples of 32; exactly 1280x720 for 16:9) |
| `Duration` | int | no | clip length in seconds (modal field, default 5) |

`Input Image` and `Motion Prompt` are accepted as aliases. Only nodes holding a literal number
are written; int nodes wired to another node (e.g. inside a subgraph) follow their source.
The workflow must contain a save-video node that writes an `.mp4`/`.webm` output. Any audio LTX
produces is discarded.

## Use
Select an image → **Film** button in the image toolbar. Two ways to get a clip, side by side in
the same dialog:
* **Generate with AI** -- review/edit the suggested motion prompt (seeded from the image's stored
  prompt), set a length, Generate. This is an async task (`image.generate_motion_clip`); needs
  ComfyUI set up (see Setup above).
* **Upload** -- the button at the top of the dialog. Pick a video file (`.mp4`, `.webm`, `.mov`,
  `.mkv`, `.m4v`, `.avi`; up to 300 MB) and it's used as-is, re-encoded video-only the same way a
  generated clip is. Works even without ComfyUI configured (`POST
  /api/v1/ppt/motion-video/upload`).

Either way, a badge on the image opens the current clip in a new tab, and **Loop the clip** (a
checkbox in the dialog) controls what happens when the clip is shorter than the slide's narration
-- see Export behaviour. Toggling it saves immediately, without needing to regenerate or re-upload.

## Export behaviour
* **Video:** the clip is composited over the rasterized slide at the image's position for the
  narration's duration. A clip shorter than that either fades out (0.5 s) to the static image
  (default) or repeats from the start (`motion_loop: true` on the image element) until the
  narration ends. Either way, a clip longer than the narration is truncated. Slides with no
  narration run for the clip's length. Failures fall back to the static image for that slide.
* **PDF:** clips are ignored.
* **PPTX:** the bundled export runtime can't embed video, so the .pptx keeps static images and
  the clips download as `<title>-motion-clips.zip` (`slide-NN.mp4`) beside it (browser export only).

## Limits
Clips are composited for images anywhere on the slide, including inside flex/grid/list/gallery
layouts — their position is computed with the same flow-layout rules as the real export renderer
(`servers/fastapi/services/flow_layout.py`). An image is still skipped, falling back to the
static image with a logged warning, if it is rotated or has a `clip_path`. One case is only
approximate rather than exact: a flex/grid sibling of the clipped image that is text with no
explicit width/height gets an estimated size (a character-count heuristic) rather than the
real browser's measured text size, which can shift the clip's position by a few pixels when it
shares a row/column with such text. Images themselves, and every other element type, always
have an explicit size in this app's templates, so this only comes up with unusual text-heavy
custom layouts.
