# Presenton Fork — Handover Notes

Fork: `github.com/PhillLittlewood/presenton`, forked from `github.com/presenton/presenton`.
Written for a fresh Claude Code session with direct file access. Parts of this were written from
chat sessions (no direct file access) — **verify current state against the repo** (`git log`,
`git status`) before relying on anything here.

## 0. Branches and how to get either version

| Branch / tag | What it is |
| --- | --- |
| `main` | **Current version.** Everything below, including AI motion clips and editable speaker notes. |
| `pre-embedded-video` | The fork exactly as it was *before* the motion-clip work (narrated-video export only). Keep it as the fallback. |
| tag `pre-embedded-video-v1` | Immutable pointer to the same commit as `pre-embedded-video`. |
| tag `backup-before-upstream-merge` | From before the 169-commit upstream merge (older still). |

```
git clone https://github.com/PhillLittlewood/presenton.git                         # new (main)
git clone -b pre-embedded-video https://github.com/PhillLittlewood/presenton.git presenton-old
git switch pre-embedded-video   # in an existing clone, then: docker compose build <service>
```

Switching versions needs an image rebuild (`docker compose build production`), because both the
Next.js frontend and the FastAPI backend changed. No database migration is involved either way.
Data written by the new version is tolerated by the old one, with one loss: the old backend's
image model does not know `motion_video`, so saving a slide with it drops the clip reference (the
clip files stay on disk under `motion/`).

**Upstream:** an earlier PR to `presenton/presenton` was opened; their `CONTRIBUTING.md` says
contributions outside `electron/` may not be accepted, and everything here lives in `servers/`.
If that PR was opened from this fork's `main` and is still open, pushing to `main` adds the new
commits to it — check the PR before pushing.

## 1. Feature A — slide notes to narrated video (already existed, unchanged in behaviour)

Export a presentation's speaker notes as narration synced to the slides, as a downloadable MP4,
using a user-supplied ComfyUI TTS workflow. Mirrors the ComfyUI *image* generation integration
(submit → poll `/history` → download; same Settings UI shape; same env-var naming).

Pipeline (`servers/fastapi/services/video_export_service.py`, singleton `VIDEO_EXPORT_SERVICE`):
1. Export the deck to PDF through the existing export pipeline (`utils/export_utils.py` →
   `services/export_task_service.py`, the Puppeteer sidecar) so visuals match the editor.
2. Rasterize each PDF page to PNG with PyMuPDF (`import fitz`).
3. Narrate each slide via ComfyUI TTS (`services/narration_service.py`), bounded by
   `VIDEO_NARRATION_MAX_CONCURRENCY` (default 2). Slides with no note, or with narration disabled,
   get a short silent clip instead of failing.
4. Build per-slide segments with ffmpeg and concat them into the final MP4.

Key files: `services/narration_service.py` (text node must be titled **"Input Prompt"**),
`api/v1/ppt/endpoints/video.py` (sync + `/async` export endpoints, polled through the generic
`GET /api/v1/async-tasks/status/{id}`), `utils/video_narration_provider.py`,
`enums/video_narration_provider.py`, `utils/asset_directory_utils.py` (`get_videos_directory()`
owner-scoped via `_owned_directory()`; `get_videos_root_directory()` unscoped).

Frontend: `settings/VideoNarrationProvider.tsx` (toggle → provider tile grid → "ComfyUI setup"),
onboarding step 4, "Video" option in the editor's Export dropdown (`PresentationHeader.tsx`,
`handleExportVideo()` polls every 3 s). Video export uses its own Next.js proxy routes
(`app/api/export-presentation-video/{route,status/route,file/route}.ts`) because PDF/PPTX export
goes through a different client-side runtime (`runBundledPresentationExport`). The file route
enforces the same per-owner check as `app/api/export-presentation/file/route.ts`.
`handleExportVideo` had to be adapted to upstream's `trackExportLifecycle()`; re-sync if upstream
changes that again.

Config fields (`types/llm_config.ts` + `models/user_config.py`): `DISABLE_VIDEO_NARRATION`,
`VIDEO_NARRATION_PROVIDER`, `COMFYUI_TTS_URL` (falls back to `COMFYUI_URL`), `COMFYUI_TTS_WORKFLOW`,
round-tripped through `utils/get_env.py` / `set_env.py` / `user_config.py`.

## 2. Feature B — AI motion clips for slide images (ComfyUI LTX) — new on `main`

Replace any slide image with a short image-to-video clip **in the final video export only**. The
still image stays the visual everywhere else (canvas, PDF, PPTX). User docs: `docs/motion-video.md`.

### Configuration
Settings → **Motion Video** (sidebar entry, `settings/VideoMotionProvider.tsx`) or onboarding
step 5 of 6 (`PresentonMode.tsx`, header in `OnBoardingHeader.tsx`). Env / `LLMConfig` fields:
`DISABLE_MOTION_VIDEO`, `MOTION_VIDEO_PROVIDER` (`comfyui`), `COMFYUI_MOTION_URL` (falls back to
`COMFYUI_URL`), `COMFYUI_MOTION_WORKFLOW`, plus `VIDEO_MOTION_MAX_CONCURRENCY` (default 1; env only —
image gen, TTS and LTX share one GPU). All passed through in `docker-compose.yml` for all four
services. Hidden in Presenton-Cloud mode, same as the other providers.

**Workflow node-title convention** (API-format workflow JSON; matched case-insensitively on
`_meta.title`):

| Title | Required | Filled with |
| --- | --- | --- |
| `Load Image` (alias `Input Image`) | yes | the slide image, uploaded via `/upload/image` |
| `Input Prompt` (aliases `Motion Prompt`, `Prompt`) | no | the motion prompt |
| `Width`, `Height` | no | size from the image's aspect ratio (1280×720 for 16:9, else ~0.92 MP in multiples of 32) |
| `Duration` | no | clip length in seconds |

Only nodes holding a **literal number** are written; int nodes wired to another node (subgraph
inner nodes) follow their source. Width/Height nodes at 0 (as in the user's workflow) *must* be
filled. The workflow needs a save-video node that outputs `.mp4`/`.webm`. The user's working
workflow files live at the repo root as `comfyui_*.json` (image gen, TTS, video gen) — personal
configs, not required by the app.

### Generation flow (editor)
Select an image → **Film** button in the image toolbar → `MotionClipModal.tsx`:
1. `GET /api/v1/ppt/motion-video/status` → `{enabled, configured}`.
2. `POST .../suggest-prompt` — the text LLM suggests a motion prompt seeded from the image's stored
   `element.prompt` (no vision). 25 s timeout so a slow local LLM can't block the dialog.
3. **Length (seconds)** field, pre-filled from the slide's script (see §3), editable, 2–20 s.
4. `POST .../generate/async` → `AsyncTaskModel` of type `image.generate_motion_clip`, polled every
   3 s through `/api/v1/async-tasks/status/{id}` (`services/api/motion-video.ts`).
5. Result stored as `element.motion_video` (a sidecar on the existing image element, not a new
   element type). Failure leaves the still image and any previous clip untouched (inline error + toast).
6. Regenerating deletes the superseded file; "Remove" calls `POST .../delete`.
Double-clicking an image does **nothing** in the current editor code, so the toolbar button is the
entry point. The canvas never plays video; images with a clip get a small play badge
(`MotionClipBadge` in `surface/nodes.tsx`, editor-only) that opens the clip in a new tab.

### Backend files
- `services/motion_video_service.py` — ComfyUI LTX client (upload → inject nodes → submit → poll
  `/history` → download; scans every output key for video extensions, prefers `type: output`).
  Re-encodes video-only H.264 (`-map 0:v:0 -an`) so LTX's audio can never collide with narration.
  Timeout 1800 s, per-process semaphore from `VIDEO_MOTION_MAX_CONCURRENCY`.
- `api/v1/ppt/endpoints/motion_video.py` — status, suggest-prompt, generate/async, delete, and
  `POST /motion-video/presentation/{id}/export-clips` (PPTX companion zip).
- `utils/llm_calls/generate_motion_prompt.py`, `utils/video_motion_provider.py`,
  `enums/video_motion_provider.py`, env plumbing in `utils/{get,set}_env.py` / `user_config.py`.
- Storage: `get_motion_videos_directory()` → `<app_data>/motion[/users/<owner_id>]/<uuid>.mp4`.
  `"motion"` was added to `PRIVATE_APP_DATA_ROOTS` (`api/v1/auth/assets.py`) and the cloud-proxy
  prefixes so the browser can play clips via `/app_data/motion/...` with per-owner authorization.
- `templates/v2/models/elements.py` — `Image.motion_video: Optional[str]`. **Required**: Pydantic
  ignores unknown fields, so without it the value is silently stripped on save.

### Video export compositing (`services/motion_layout.py` + `video_export_service.py`)
Slides are exported as a rasterized PDF page looped for the narration duration. For slides whose
image carries a clip, the segment is composited instead (all other slides use the unchanged path):
- The base layer is the rasterized slide; the clip overlays it at the image's box.
- Segment length = narration audio + end pad. A **shorter** clip fades out over 0.5 s to the still;
  a **longer** clip is truncated. A slide with **no narration** runs for the clip's length (min 2.5 s).
- Geometry mirrors the Konva editor: `absoluteElementBox` / `layoutContainerChildren` for placement,
  `ImageNode` fit/focus/`crop_scale` math (default fit is `contain`), flips, per-corner radii,
  opacity, and `container` clipping — applied as a Pillow alpha mask + ffmpeg `alphamerge`.
- **Not composited** (falls back to the static image, with a logged warning): images under
  `flex`/`grid`/`list-view` parents (positions are computed by the editor's flow-layout engine at
  render time), rotated images or rotated ancestors, and images with a `clip_path`.
- Slide UI lives in `SlideModel.ui` (`elements` + `components[].elements`, stage 1280×720), **not**
  `content`. `disabled` motion video ⇒ export ignores existing clips. Any per-slide compositing
  error falls back to the flat loop.

### PDF / PPTX
PDF ignores clips. PPTX cannot embed video: the export runtime that builds it
(`@presenton/export-core` in `presentation-export/`) is synced in at Docker build time and is not in
this repo. Accepted fallback: the .pptx keeps static images and the clips download as
`<title>-motion-clips.zip` (`slide-NN.mp4`) via `app/api/export-motion-clips/route.ts` +
`downloadMotionClipsBundle()` in `PresentationHeader.tsx`. Browser export only — the Electron IPC
path is untouched.

### Gotchas found while building this
1. **Dialogs hosted inside the image toolbar must carry the editor's marker attributes.** A
   document-level capture `pointerdown` handler (`TemplateV2KonvaSlide.tsx`, ~line 2490) clears the
   selection for any press outside the slide unless the target is inside
   `[data-template-v2-floating-toolbar='true']` or `[data-inline-edit-ignore='true']`. Clearing the
   selection unmounts the toolbar — and the dialog with it — before the click completes. This is
   exactly why the first version's "Generate clip" button appeared to do nothing. `MotionClipModal`
   now mirrors `ImagePickerModal` (both attributes, `z-[10050]/[10051]` above the floating toolbar,
   `onPointerDown` stopPropagation). Any new dialog opened from a toolbar needs the same.
2. `adaptImage` in `slide-editor/importing/template-v2-import.ts` drops `prompt` (and would drop
   `motion_video`), but it only runs for template import, not for saved slides.
3. The stored image `data` may be an absolute URL or a `/app_data/...` path; the backend accepts both
   (plus `data:` URIs and http(s) downloads).

## 3. Feature C — editable speaker notes + script-based clip length — new on `main`

- `SlideActionBar.tsx`: the "Speaker notes" popover is now a textarea. Edits go to a local draft and
  are written to the store with `updateSlide` after 700 ms idle, on popover close, when the slide
  changes, and on unmount; the existing auto-save (`useAutoSave` → `PATCH
  /api/v1/ppt/presentation/slide_update`) persists them, and narration/presenter mode read the same
  `speaker_note`. The notes button now shows on **every** slide so notes can be added where none exist.
- `slide-editor/images/motion-duration.ts` — `suggestMotionDuration()`: ~150 words/min (CJK by
  character at 4.5 chars/s), clamped to 2–20 s. Used by the Motion clip dialog's Length field (with a
  "Use suggested" link) and by the notes popover's "≈ N s of narration" line. The slide's note reaches
  the dialog via `TemplateV2KonvaSlide` (`useSelector`) → `ElementToolbar` → `ImageToolbar` → modal.

## 3b. Fix — generation hangs at "Generating presentation data…" with some local models

**Symptom:** with some models (LM Studio: Qwen3 14B, Gemma 4 26B-A4B; Ministral 3 14B erroring) the outline
page's overlay parks at 95% and never reaches the slides. Llama 3 8B and DeepSeek-R1 14B were fine.
That overlay is the `POST /api/v1/ppt/presentation/prepare` request, which awaits a single LLM call
(`generate_presentation_structure` — picks a layout per slide).

**Causes found (all in the shared structured-output path, `utils/llm_utils.py`):**
1. *Validation retry loop.* The structure schema demands exactly N slide indexes. A model that miscounts
   triggered up to 4 full regenerations (plus 3 parse retries each), each with a growing prompt — minutes
   for a thinking model, so it looked hung. `/prepare` already repairs a wrong count/range
   (`_normalize_presentation_structure`), so the layout call now takes the first answer
   (`validate_schema_max_loop_count=1`).
2. *Malformed retry messages.* The correction was appended as a **second consecutive user message**, which
   strict chat templates reject (Mistral/Ministral: "conversation roles must alternate…" — the Jinja error in
   the LM Studio log; Gemma templates behave similarly). Now `structured_validation_feedback_messages()` sends
   an assistant turn (the invalid JSON) then a user turn.
3. *No end to a runaway generation.* No `max_tokens`, no timeout, no stop condition, so a model that never
   stops (repetition loop, endless whitespace after its JSON, endless reasoning) held the request open forever.
   `_generate_structured_content` now watches the stream (`utils/structured_output_guard.py`): it stops when a
   complete JSON object has arrived and the model keeps writing (>256 chars), or on >2000 whitespace chars in a
   row, no `{` within 20k chars, `LLM_STRUCTURED_MAX_CHARS` (default 100000) or `LLM_STRUCTURED_MAX_SECONDS`
   (default 1800, 0 = off). A complete JSON is used; otherwise the call fails with a clear 400 ("The model
   never finished its reply (…)") instead of hanging.
4. *Replies wrapped in code fences, `<think>` blocks or prose* made the llmai OpenAI-compatible client raise
   `Expecting value…`. The streamed text is now salvaged (`extract_json_object`) before giving up, and
   `extract_structured_content` falls back to the same tolerant parser.

**Diagnostics:** the backend now logs `Structured LLM call started/still running (every 30 s)/finished` with
prompt size, reply chars, thinking chars, finish reason and whether it was stopped early. The outline overlay
shows "Still waiting for the model (Xm Ys)…" after 45 s. If a model still stalls, send those log lines.

**Not confirmed:** whether Qwen3/Gemma stalled from cause 1 (slow retries) or 3 (a real runaway) — reproduced
both with a fake OpenAI-compatible server (`/tmp` scripts, not committed) but not with the real models. LM
Studio's context length and its "separate reasoning content" setting are worth checking for those models.
Tests: `tests/unit/test_structured_output_guard.py`, `tests/unit/test_llm_structured_generation.py`.

## 4. Other work (unchanged from earlier notes)

Title-slide layout fix in `utils/llm_calls/generate_presentation_structure.py`
(`_ensure_title_layout_for_first_slide` / `_find_title_layout_indices`, keyword priority list with
`title` last, `_MAX_MATCH_FRACTION = 0.75`; diagnostic `[title_layout_correction]` logging on every
call). Its 8-scenario test suite was run ad hoc and never committed — worth turning into a real pytest
file. Layout selection for *any* slide is LLM-driven from each layout's `name`/`description`
(`templates/presentation_layout.py:to_string()`), not from array position.

## 5. Build / deploy gotchas (read before touching config)

1. **`uv.lock` is the source of truth for Docker builds.** `Dockerfile` runs `uv export --frozen
   --no-dev --no-emit-project`; always run `uv lock` in `servers/fastapi/` after editing
   `pyproject.toml`. (`pymupdf` once shipped broken for exactly this reason.) The motion-clip work
   added **no** new Python dependencies (Pillow, aiohttp, zipfile were already available).
2. **Settings may not take effect without `USER_CONFIG_PATH`.** The UI persists to the
   `ProviderSettings` table, but code reads `os.environ`, synced from a legacy JSON file
   (`utils/user_config.py:update_env_with_user_config()`). `docker-compose.yml` defaults
   `USER_CONFIG_PATH=/app_data/userConfig.json`. If a setting "doesn't stick", check
   `docker compose exec production env | grep <VAR>` before and after saving. This also applies to the
   motion-video settings (the status endpoint reads `os.environ`).
3. **`templates/` is baked into the image** (`COPY templates /app/templates`); custom templates go in
   `templates/<name>/template.json` at the repo root and need `docker compose build production`.
   Uploading a `.pptx` through the app's template API is a different route (stored in the DB, no rebuild).
4. Compose service names are `production`, `production-gpu`, `development`, `development-gpu`.
5. `ffmpeg`/`ffprobe` are installed in `Dockerfile` and `Dockerfile.dev`; both narration and motion
   compositing need them.

## 6. Tests

- Backend (`servers/fastapi`, `python -m pytest tests -q`): 827 passed at handover. New:
  `tests/unit/test_motion_layout.py` (placement, fit math, owner scoping),
  `tests/unit/test_motion_video_service.py` (fake-ComfyUI end-to-end incl. audio stripping, node
  injection for the LTX workflow, size selection), `tests/integration/test_motion_video_endpoints.py`
  (status, gating, task lifecycle, superseded-clip cleanup, deletion scope, clips zip).
- Frontend (`servers/nextjs`): `npm test` → 24 passing (`tests/motion-duration.test.mjs` is new);
  `npx tsc --noEmit -p tsconfig.json` is clean. Tools that rewrite `tsconfig.tsbuildinfo` — revert it.
- Compositing was verified by rendering real ffmpeg output and inspecting frames. The dialog and
  notes-editor behaviour was verified in a simulated DOM (jsdom + a real redux store), not committed
  because `jsdom` isn't a project dependency.
- **Not verified:** a full run against a real ComfyUI/LTX server through to an exported MP4, the Docker
  build, and the PPTX companion download. Do this before relying on the feature.

## 7. Known limitations / ideas

- "Stop waiting" in the motion dialog stops polling but cannot cancel the ComfyUI job; a finished clip
  may then be left on disk. Clips are also not deleted when an image element or slide is deleted.
- LTX crops the start image to the requested width/height (`crop: center`), so the clip's framing can
  differ slightly from the still when the image's aspect ratio isn't reproduced exactly.
- Generated notes sometimes begin with the literal text "Speaker note:", which narration reads aloud.
  Now editable by hand; could be stripped at generation/narration time.
- The motion-prompt suggestion doesn't use the slide's script even though the endpoint accepts a
  `slide_context`; wiring it in would make suggestions reflect the narration.
- Flex/grid-nested images could be composited by porting the editor's flow-layout engine to Python.
- Electron PPTX export doesn't bundle the motion-clips zip.

## 8. Suggested first steps for Claude Code

1. Read this file, then `git log --oneline -20` and `git status` to confirm real state.
2. Confirm narrated export still works end to end
   (`docker compose logs production | grep -i "video_export\|motion_video\|ComfyUI"` after a test export).
3. Run one motion-clip generation and an export with a real LTX workflow; check the logs for
   `Motion workflow prepared: image=… size=…x… duration=…`, and for
   `Motion clip on slide N not composited (…)` warnings that explain any fallback to the still.
4. If extending narration or title-slide logic, turn the ad hoc tests into files under
   `servers/fastapi/tests/`.
