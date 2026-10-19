# Presenton Fork — Handover Notes

Fork: `github.com/PhillLittlewood/presenton`, forked from `github.com/presenton/presenton`.
Written for a fresh Claude Code session with direct file access. Verify current state against the
repo (`git log`, `git status`) before relying on anything here — some of this may have moved on.

## 0. Branches and how to get either version

| Branch / tag | What it is |
| --- | --- |
| `main` | **Current version.** Everything below: narrated video export, AI motion clips (generate or upload, with loop), editable speaker notes, the presentation-generation hang fix, and the multi-clip/flex-layout fix. |
| `pre-embedded-video` | The fork exactly as it was *before* any of the motion-clip work. Kept as a fallback. |
| tag `pre-embedded-video-v1` | Immutable pointer to the same commit as `pre-embedded-video`. |
| tag `backup-before-upstream-merge` | From before a 169-commit upstream merge, older still. |

```
git clone https://github.com/PhillLittlewood/presenton.git                         # new (main)
git clone -b pre-embedded-video https://github.com/PhillLittlewood/presenton.git presenton-old
git switch pre-embedded-video   # in an existing clone, then: docker compose build <service>
```

Switching versions needs an image rebuild (`docker compose build production`), since both the
Next.js frontend and the FastAPI backend changed. No database migration is involved either way.

**Upstream:** a PR was opened earlier to `presenton/presenton`; their `CONTRIBUTING.md` says
contributions outside `electron/` may not be accepted, and everything here lives in `servers/`.
Main was merged with upstream once already (see §6) — if pushing to `main` again adds commits to
an open PR from this branch, check that PR first.

## 1. Feature A — slide notes to narrated video (pre-existing, unchanged)

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
4. Build per-slide segments with ffmpeg and concat them into the final MP4 (see §2 for the
   per-slide compositing step this now includes when a slide has motion clips).

Key files: `services/narration_service.py` (text node must be titled **"Input Prompt"**),
`api/v1/ppt/endpoints/video.py` (sync + `/async` export endpoints, polled through the generic
`GET /api/v1/async-tasks/status/{id}`), `utils/video_narration_provider.py`,
`enums/video_narration_provider.py`, `utils/asset_directory_utils.py` (`get_videos_directory()`
owner-scoped via `_owned_directory()`). Frontend: `settings/VideoNarrationProvider.tsx`, onboarding
step 4, "Video" option in the editor's Export dropdown (`PresentationHeader.tsx`). Video export
uses its own Next.js proxy routes (`app/api/export-presentation-video/...`) because PDF/PPTX export
goes through a different client-side runtime (`runBundledPresentationExport`).

Config: `DISABLE_VIDEO_NARRATION`, `VIDEO_NARRATION_PROVIDER`, `COMFYUI_TTS_URL` (falls back to
`COMFYUI_URL`), `COMFYUI_TTS_WORKFLOW`, round-tripped through `utils/get_env.py` / `set_env.py` /
`user_config.py`.

## 2. Feature B — motion clips for slide images (generate with ComfyUI LTX, or upload your own)

Replace any slide image with a short motion clip **in the final video export only**. The still
image stays the visual everywhere else (canvas, PDF, PPTX). User docs: `docs/motion-video.md`.

### Configuration
Settings → **Motion Video** (`settings/VideoMotionProvider.tsx`) or onboarding step 5 of 6
(`PresentonMode.tsx`). Env / `LLMConfig` fields: `DISABLE_MOTION_VIDEO`, `MOTION_VIDEO_PROVIDER`
(`comfyui`), `COMFYUI_MOTION_URL` (falls back to `COMFYUI_URL`), `COMFYUI_MOTION_WORKFLOW`, plus
`VIDEO_MOTION_MAX_CONCURRENCY` (default 1; env only — image gen, TTS and LTX share one GPU). All
passed through `docker-compose.yml` for all four services. Hidden in Presenton-Cloud mode.
**Uploading a clip needs none of this** — see below.

**Workflow node-title convention** (API-format workflow JSON; matched case-insensitively on
`_meta.title`):

| Title | Required | Filled with |
| --- | --- | --- |
| `Load Image` (alias `Input Image`) | yes | the slide image, uploaded via `/upload/image` |
| `Input Prompt` (aliases `Motion Prompt`, `Prompt`) | no | the motion prompt |
| `Width`, `Height` | no | size from the image's aspect ratio (1280×720 for 16:9, else ~0.92 MP in multiples of 32) |
| `Duration` | no | clip length in seconds, from the dialog's Length field |

Only nodes holding a **literal number** are written; int nodes wired to another node (subgraph
inner nodes) follow their source. The workflow needs a save-video node outputting `.mp4`/`.webm`.
The user's own working workflow JSON files live at the repo root as `comfyui_*.json` (personal
configs, not required by the app).

### Generation flow (editor)
Select an image → **Film** button in the image toolbar → `MotionClipModal.tsx`. Two independent
ways to get a clip, in the same dialog:
- **Generate with AI**: `GET .../status` gates this section on ComfyUI being configured. The text
  LLM suggests a motion prompt from `element.prompt` (`POST .../suggest-prompt`, 25 s timeout so a
  slow local LLM can't block the dialog). **Length (seconds)** field is pre-filled from the slide's
  narration estimate (`slide-editor/images/motion-duration.ts`, ~150 words/min, CJK by character),
  editable 2–20 s. `POST .../generate/async` → `AsyncTaskModel` of type
  `image.generate_motion_clip`, polled every 3 s via `/api/v1/async-tasks/status/{id}`.
- **Upload**: a button at the top of the dialog, always available regardless of ComfyUI config.
  Accepts `.mp4/.webm/.mov/.mkv/.m4v/.avi` up to 300 MB (`ALLOWED_UPLOAD_VIDEO_EXTENSIONS` /
  `MAX_UPLOAD_VIDEO_BYTES` in `api/v1/ppt/endpoints/motion_video.py`), synchronous
  `POST /api/v1/ppt/motion-video/upload` (multipart, `file` + optional `previous_motion_video`).

Either path writes `element.motion_video` (sidecar on the existing image element, not a new
element type). Regenerating/re-uploading deletes the superseded file; "Remove" calls
`POST .../delete`. Failure leaves the still image and any previous clip untouched. Double-clicking
an image does **nothing** in this editor, so the toolbar button is the only entry point. The canvas
never plays video; a small badge (`MotionClipBadge` in `surface/nodes.tsx`, editor-only) opens the
current clip in a new tab.

**Loop**: a checkbox in the same dialog ("Loop the clip to fill the slide"), backed by
`element.motion_loop` (bool). Saves immediately on toggle — independent of Generate/Upload, so it
applies to a clip that already exists without regenerating it. See §2's Export behaviour for what
it changes.

### Backend files
- `services/motion_video_service.py` — ComfyUI LTX client (upload → inject nodes → submit → poll
  `/history` → download; scans every output key for video extensions, prefers `type: output`).
  `prepare_uploaded_clip(raw_path, output_directory)` is the public entry point the upload endpoint
  calls; it's a thin wrapper around the same `_strip_audio` the generation path uses, so both
  generated and uploaded clips end up identically normalized (video-only H.264 MP4, `-an`,
  `-pix_fmt yuv420p`). Timeout 1800 s for generation; per-process semaphore from
  `VIDEO_MOTION_MAX_CONCURRENCY`.
- `api/v1/ppt/endpoints/motion_video.py` — status, suggest-prompt, generate/async, **upload**,
  delete, and `POST /motion-video/presentation/{id}/export-clips` (PPTX companion zip, §2 PPTX).
  The upload handler streams to disk with a size cap, then calls `prepare_uploaded_clip`; on any
  failure it cleans up its own temp file (ffmpeg failure there raises `MotionVideoGenerationError`,
  mapped to a 400 "Could not read that file as a video").
- `utils/llm_calls/generate_motion_prompt.py`, `utils/video_motion_provider.py`,
  `enums/video_motion_provider.py`, env plumbing in `utils/{get,set}_env.py` / `user_config.py`.
- Storage: `get_motion_videos_directory()` → `<app_data>/motion[/users/<owner_id>]/<uuid>.mp4`.
  `"motion"` is in `PRIVATE_APP_DATA_ROOTS` (`api/v1/auth/assets.py`) and the cloud-proxy prefixes
  so the browser can play clips via `/app_data/motion/...` with per-owner authorization.
- `templates/v2/models/elements.py` — `Image.motion_video: Optional[str]`,
  `Image.motion_loop: Optional[bool]`. **Both required** — Pydantic ignores unknown fields, so
  without them the values are silently stripped on save.

### Flow layout — `services/flow_layout.py` (why multiple clips on one slide used to fail)
This is a **line-for-line Python port** of the editor's own flow-layout engine
(`servers/nextjs/components/slide-editor/layout/flowLayout.ts` +
`layout/wrappedFlexLayout.ts`) — not an approximation. That TypeScript module is the single
source of truth for `flex`/`list-view`/`grid`/`grid-view` child placement: it's used by both the
Konva editor *and* the real HTML export renderer (`lib/template-v2-json-to-html.ts` calls it and
bakes the resulting pixel box into each child's `size` before the browser ever lays anything out),
so porting it exactly — rather than reimplementing CSS flexbox from scratch — keeps motion-clip
placement pixel-accurate for slides using these containers.

**Why this existed as a bug**: the original code unconditionally skipped any image inside a
flex/grid parent ("positions are computed by the editor at render time"). Multi-image slide
layouts (side-by-side photos, card grids, galleries) are built almost exclusively with
flex/grid containers — confirmed by scanning every shipped template — while a single image is
usually placed directly. So a slide with one clip worked, and a slide with two or more had every
clip silently fall back to the static image. `services/flow_layout.py` (653 lines) computes real
pixel boxes for flex row/column (wrap, gap, justify/align, grow) and CSS-grid (column/row spans)
children; `services/motion_layout.py`'s `_walk` now calls it instead of skipping. 653+421 lines
across the two files, 9 tests in `tests/unit/test_flow_layout.py` plus 5 more in
`tests/unit/test_motion_layout_flow.py` covering the exact bug scenario.

One deliberate, documented approximation: a flex/grid **sibling** of the clipped image that is
text with no explicit width/height gets an estimated size (the same character-count heuristic the
app's own `htmlElementSize` fallback uses) rather than real browser text measurement
(`measureNoWrapTextWidth`, which needs an actual rendering engine). Images and every other element
type always carry an explicit size in this app's templates, so this is rarely exercised — flagged
in both the module docstring and `docs/motion-video.md`. Anything still genuinely indeterminate
(an element type with no size and no computable fallback) degrades the same way the original JS
does: collapses toward a small size rather than refusing, so motion placement never hard-fails on
layout grounds. Images that are **rotated** or have a `clip_path` are still skipped (unchanged —
the export has no way to rotate/mask the overlay clip to match).

### Video export compositing (`video_export_service.py::_build_motion_segment`)
Slides are exported as a rasterized PDF page looped for the narration duration. For slides whose
image carries a clip, the segment is composited instead (every other slide uses the unchanged
flat-loop path):
- The base layer is the rasterized slide; the clip overlays it at the image's box (from
  `flow_layout.py`/`motion_layout.py`), honoring fit/focus/`crop_scale`, flips, per-corner radii,
  opacity, and `container` clipping (flex/grid never clip — matches the real renderer's
  `overflow:visible`).
- Segment length = narration audio + end pad (or the clip's own length, min 2.5 s, if the slide
  has no narration).
- **Clip shorter than the segment**: fades out (0.5 s, `MOTION_FADE_SECONDS`) to the static image
  — unless `element.motion_loop` is true, in which case ffmpeg `-stream_loop -1` is applied to
  just that clip's input and the fade is skipped, so it repeats until the segment ends instead.
- **Clip longer than the segment**: truncated either way.
- Any per-slide compositing error (bad clip file, ffmpeg failure, etc.) falls back to the flat
  loop for that one slide and logs a warning — never fails the whole export.
- Slide UI lives in `SlideModel.ui` (`elements` + `components[].elements`, stage 1280×720), **not**
  `content`. `DISABLE_MOTION_VIDEO` makes export ignore existing clips entirely.

### PDF / PPTX
PDF ignores clips. PPTX cannot embed video (the export runtime that builds it,
`@presenton/export-core` under `presentation-export/`, is synced in at Docker build time and isn't
in this repo). Fallback: the .pptx keeps static images and the clips download as
`<title>-motion-clips.zip` (`slide-NN.mp4`) via `app/api/export-motion-clips/route.ts` +
`downloadMotionClipsBundle()` in `PresentationHeader.tsx`. Browser export only — Electron's PPTX
path doesn't bundle the zip.

### Gotchas found while building this
1. **A dialog hosted inside the image toolbar must carry the editor's marker attributes.** A
   document-level capture `pointerdown` handler (`TemplateV2KonvaSlide.tsx`, ~line 2490) clears the
   selection for any press outside the slide unless the target is inside
   `[data-template-v2-floating-toolbar='true']` or `[data-inline-edit-ignore='true']`. Clearing the
   selection unmounts the toolbar — and the dialog with it — before a click completes. This caused
   the first version's "Generate clip" button to silently do nothing.
   `MotionClipModal.tsx` mirrors `ImagePickerModal` (both attributes, `z-[10050]/[10051]`,
   `onPointerDown` stopPropagation). Any new dialog opened from a toolbar needs the same.
2. `adaptImage` in `slide-editor/importing/template-v2-import.ts` drops `prompt` (and would drop
   `motion_video`/`motion_loop`), but it only runs for template import, not saved slides.
3. The stored image `data` may be an absolute URL, a `/app_data/...` path, a `data:` URI, or an
   http(s) URL the backend downloads — all four are handled when materializing the LTX start frame.

## 3. Feature C — editable speaker notes + script-based clip length

- `SlideActionBar.tsx`: the "Speaker notes" popover is a textarea. Edits go to a local draft and
  are written to the store with `updateSlide` after 700 ms idle, on popover close, when the slide
  changes, and on unmount; the existing auto-save persists them, and narration/presenter mode read
  the same `speaker_note`. The notes button shows on every slide so notes can be added where none
  exist.
- `slide-editor/images/motion-duration.ts` — `suggestMotionDuration()`, shared by the notes
  popover's "≈ N s of narration" line and the Motion clip dialog's Length-field suggestion.

## 4. Fix — presentation generation hanging with slower/local/thinking models

**Symptom:** with some models (Qwen3, Gemma locally via LM Studio; Ministral erroring outright) the
outline page's loading overlay parks at 95% ("Generating presentation data…") and never reaches the
slides; Llama 3 8B and DeepSeek-R1 14B were fine. The overlay is `POST
/api/v1/ppt/presentation/prepare`, awaiting one LLM call (`generate_presentation_structure` — picks
a layout per slide).

**Root causes, all in `utils/llm_utils.py`:**
1. A wrong slide-index count triggered up to 4 full regenerations (`/prepare` already repairs a
   wrong count afterward) — now takes the first answer (`validate_schema_max_loop_count=1`).
2. The correction prompt was sent as a second consecutive **user** message, which strict chat
   templates (Mistral/Ministral, Gemma) reject ("conversation roles must alternate…" — the literal
   LM Studio error). `structured_validation_feedback_messages()` now sends an assistant turn (the
   invalid JSON) then a user turn.
3. Nothing ever stopped a model that never finishes (repetition loop, endless whitespace after its
   JSON, endless reasoning) — no `max_tokens`, no timeout. `utils/structured_output_guard.py`'s
   `JsonCompletionWatcher` now stops the stream once a complete JSON object has arrived and the
   model keeps writing, or on clearly degenerate output (env-tunable
   `LLM_STRUCTURED_MAX_CHARS`/`LLM_STRUCTURED_MAX_SECONDS`), returning the JSON if complete or a
   clear 400 otherwise.
4. Replies wrapped in code fences / `<think>` blocks / prose made the LLM client raise; now
   salvaged via `extract_json_object`.

**Diagnostics:** backend logs `Structured LLM call started/still running (every 30 s)/finished`
with prompt size, reply chars, thinking chars, finish reason. The outline overlay shows "Still
waiting for the model (Xm Ys)…" after 45 s. Tests: `tests/unit/test_structured_output_guard.py`,
`tests/unit/test_llm_structured_generation.py`.

## 5. Other pre-existing work (unchanged)

Title-slide layout fix in `utils/llm_calls/generate_presentation_structure.py`
(`_ensure_title_layout_for_first_slide` / `_find_title_layout_indices`, keyword priority list with
`title` last, `_MAX_MATCH_FRACTION = 0.75`). Its 8-scenario test suite was run ad hoc and never
committed — worth turning into a real pytest file. Layout selection for *any* slide is LLM-driven
from each layout's `name`/`description` (`templates/presentation_layout.py:to_string()`), not
array position.

## 6. Git / branch state

- `main` is the working branch; last confirmed state (this handover) is commit
  `4ddceafa "Allow uploading a pre-made video clip and add a loop option for motion clips"`,
  directly on top of `23763dda "Fix motion clips not playing when a slide has more than one"`,
  both on top of a completed upstream merge (`de512244`). All pushed to the fork.
- A prior upstream sync (169 commits from `presenton/presenton`) was done by merge, not rebase,
  with a `backup-before-upstream-merge` tag as a safety net; conflicts were in `docker-compose.yml`
  and `components/OnBoarding/PresentonMode.tsx`, both resolved. If pulling upstream again, expect
  conflicts in whichever of those (or `SettingPage.tsx`/`SettingSideBar.tsx`/
  `PresentationHeader.tsx`) upstream has also touched.
- `comfyui_imagegen.json`, `comfyui_tts.json`, `comfyui_videogen.json` at the repo root are the
  user's own working ComfyUI workflow files (personal configs — not required by the app, not
  referenced by any code path).

## 7. Build / deploy gotchas (read before touching config again)

1. **`uv.lock` is the source of truth for Docker builds.** `Dockerfile` runs `uv export --frozen
   --no-dev --no-emit-project`; always run `uv lock` in `servers/fastapi/` after editing
   `pyproject.toml`. None of the work above added a new Python dependency (Pillow, aiohttp,
   zipfile, dirtyjson were already present).
2. **Settings may not take effect without `USER_CONFIG_PATH`.** The UI persists to the
   `ProviderSettings` DB table, but runtime code reads `os.environ`, synced from a legacy JSON file
   via `utils/user_config.py:update_env_with_user_config()`. `docker-compose.yml` defaults
   `USER_CONFIG_PATH=/app_data/userConfig.json`. If a setting "doesn't stick", check
   `docker compose exec production env | grep <VAR>` before/after saving in the UI. Applies to
   motion-video settings too (the `/status` endpoint reads `os.environ` directly).
3. **`templates/` is baked into the image** (`COPY templates /app/templates`); needs
   `docker compose build production` to pick up changes.
4. Compose service names: `production`, `production-gpu`, `development`, `development-gpu`.
5. `ffmpeg`/`ffprobe` are required (installed in both Dockerfiles) — narration, motion generation,
   motion upload, and export compositing all shell out to them.

## 8. Tests

- Backend (`servers/fastapi`, `python -m pytest tests -q`): **1015 passed** as of the last commit.
  Everything motion-related: `tests/unit/test_motion_layout.py`, `test_motion_layout_flow.py`,
  `test_flow_layout.py`, `test_motion_video_service.py`, `test_structured_output_guard.py`,
  `test_llm_structured_generation.py`, `tests/integration/test_motion_video_endpoints.py` (status,
  gating, generate task lifecycle, upload success/validation/size-limit/cleanup,
  loop-vs-fade compositing with real ffmpeg, PPTX clips zip).
- Frontend (`servers/nextjs`): `npm test` → **65 passed**; `npx tsc --noEmit -p tsconfig.json`
  clean; `eslint` clean on every file touched. A tool run can rewrite `tsconfig.tsbuildinfo` —
  `git checkout` it back if that happens.
- Verified with real ffmpeg (not just unit tests): rendered and inspected actual frames for —
  single and multi-clip compositing, fit/crop/flip/corner-radius/container-clip combinations, the
  exact two-image-flex-row bug scenario (before/after), and loop vs. fade (sampled multiple points
  well past the clip's single-play length). Verified the upload endpoint against the real running
  FastAPI app with zero ComfyUI configuration.
- **Not verified:** the Motion clip dialog's new Upload/Loop UI in an actual browser (typecheck +
  lint only), the Docker build, and a full generate-or-upload-through-export run against a real
  ComfyUI/LTX server. Do these before trusting the feature end-to-end.

## 9. Known limitations / ideas for next session

- "Stop waiting" in the motion dialog stops polling but can't cancel an in-flight ComfyUI job; a
  clip may finish and land on disk anyway. Clips aren't deleted when their image/slide is deleted.
- LTX crops the start image to the requested width/height (`crop: center`), so a generated clip's
  framing can differ slightly from the still if its aspect ratio isn't reproduced exactly.
- No persistent "video library" for uploads (unlike images' "Use Your Image" library) — each
  upload replaces only the current image's clip. Worth adding if reuse across slides matters.
- Generated speaker notes sometimes start with the literal text "Speaker note:", which narration
  reads aloud. Editable by hand now; could be stripped at generation/narration time instead.
- The motion-prompt suggestion doesn't use the slide's script even though the endpoint accepts a
  `slide_context` param — wiring it in would make suggestions reflect the narration.
- Flow-layout text-sibling sizing is an estimate, not exact (see §2) — only matters for unusual
  text-heavy custom layouts sharing a row/column with a clipped image.

## 10. Suggested first steps for a new session

1. Read this file, then `git log --oneline -20` and `git status` to confirm actual current state —
   this file may already be stale.
2. Confirm narrated export and motion-clip export still work end to end
   (`docker compose logs production | grep -i "video_export\|motion_video\|ComfyUI"` after a test
   export) before building anything new on top.
3. Try the Upload button and the Loop checkbox in an actual browser — they're typechecked/linted
   but not click-tested in this repo.
4. If extending narration or title-slide logic, turn the ad hoc title-slide test scenarios into a
   real pytest file under `servers/fastapi/tests/`.
