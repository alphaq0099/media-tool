# Media Tool user guide

Version 1.0.0. The first prebuilt release is Windows x64; Linux source setup is
documented below for developers and testers.

## Dependencies

Open **Tools → Dependencies** to inspect installed tools. Checking does not install anything. Download buttons open provider pages. Windows installation requires selecting tools and explicitly accepting the installation agreements.

- **FFmpeg/FFprobe:** both executables are required. Put them on `PATH` or in the application folder, `tools/` or `tools/bin/`. Required encoders include `libx264`, `libx265`, `libvpx-vp9`, AAC and `libopus`, depending on the operation.
- **7-Zip:** install the command-line executable (`7z`, `7zz` or `7za`). Windows discovery also checks registered/common 7-Zip locations.
- **Tesseract:** install the engine and required language data. Language codes include `eng`; combinations use `+`, such as `eng+hin`.
- **LibreOffice:** install the suite or required converter components. Its command-line entry point is `soffice`.

Windows discovery also checks common Tesseract/LibreOffice locations and newly registered tool paths. If a tool is missing, check its location, recheck dependencies, and restart the application if necessary.

### Linux setup

The tested distribution is Debian 13 x64. Source installations need Python 3.13 with venv support and Qt's display/runtime libraries. A Debian setup can use:

```bash
sudo apt-get update
sudo apt-get install python3.13-venv libegl1 libopengl0 libxkbcommon0 \
  libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 \
  libxcb-xinerama0 libxcb-xkb1 libxkbcommon-x11-0
```

Install optional tools as needed through your distribution. Package names and Python availability differ on other distributions. Building needs `binutils`; headless GUI tests also need Xvfb and supporting tools. A working desktop display is required for normal use.

## Files, previews and destinations

Choose a tool before adding inputs. Images and Video accept their supported media types automatically. Audio accepts video for extraction through **Video sources**. Unsupported inputs are reported inline.

Use **+ Open** or drag/drop. Folder intake expands to compatible files in media tools; **Subfolders** controls recursive discovery. Archives retains folder roots as archive inputs. Folders has its own root-folder and inspection controls.

Search and extension filters limit visible inputs. For most file operations, selected rows take priority; with no selection, visible rows are used. Clear a selection to process the entire visible batch.

For tools supporting an empty destination, it means saving beside inputs. A combined archive uses the first planned input's location. Type/paste a path or use Browse to save elsewhere. Select whether existing outputs should be skipped or given unique names.

**Preview** shows planned names, conflicts, unsupported items and task details. Starting directly still validates inputs. Later setting changes do not rewrite an already queued job's saved settings.

## Images

Choose JPEG, PNG or WebP output. Inputs include common JPEG/PNG/WebP/BMP and supported single-page TIFF files.

- **Original size** converts without requested resizing.
- **Width & height** sets bounds. Keeping aspect ratio fits inside them; unlocking it can stretch the image.
- **Percentage** scales the oriented image.
- **Don't upscale** prevents enlargement unless explicitly disabled.

JPEG/WebP quality affects compression. JPEG fills transparency with the selected background; supported PNG/WebP output can retain it. Metadata handling covers supported fields only. EXIF orientation is applied before sizing. Animation, multipage and unsupported high-bit-depth inputs are rejected.

Review dimensions and sizes in Results. Use image comparison where offered.

## Video

Choose the processing mode and container:

| Output | Conversion codecs |
| --- | --- |
| MP4 / MKV | H.264 or H.265, with supported selected audio |
| WebM | VP9, with Opus audio where selected |

**Quality** sets the encoder's quality target; **Speed** controls compression effort. High quality can be combined with Fastest. Size and detail vary with content and preset. Matching source resolution does not mean matching bitrate or preserving every detail.

Resize uses bounds or percentage; video aspect ratio stays locked. Target-size compression supports H.264/H.265 in MP4/MKV and applies the limit to each output. MB and MiB are different units. Impossible budgets are rejected; failed size targets are not reported as successful final outputs. WebM conversion uses quality mode.

### Tracks and subtitles

An audio track is a sound stream, such as a language or commentary. Choose all, specific tracks, or no audio. Choices apply to the streams available in each input.

Choose compatible subtitle tracks or supply an external UTF-8 SRT/WebVTT file. Embedding adds a subtitle track; it does not burn text into the picture. Review omitted tracks, attachments and chapters before accepting omissions. Bitmap subtitle conversion is unsupported.

### Remux

**Remux (copy streams)** changes compatible containers or stream selection while copying encoded video/audio. Quality, resize and target-size controls do not apply. Unsupported codec/container combinations are blocked. Subtitle adaptation and container overhead can change size. Output verification still takes time.

The conversion path targets supported progressive 8-bit SDR media. HDR, interlacing, high bit depth and unsupported display transforms may be rejected. Play representative results in your usual player.

## Audio

Tasks include **Convert / extract**, **Trim**, **Split**, **Join**, **Volume**, **Normalize loudness**, and **Tags & cover**. Output formats include MP3, M4A/AAC, FLAC, WAV and Opus, according to task compatibility.

For trim/split, use the waveform preview and exact time fields. Join follows the selected input order. Encoding permits supported fades, sample-rate and channel changes.

**Copy encoded audio** copies compatible compressed packets without re-encoding. It cannot apply sample changes such as fades/resampling; trim boundaries can snap to packet boundaries. Choose conversion for sample-accurate edits or incompatible formats.

Volume applies gain; normalization targets perceived loudness with a true-peak limit. Tags and cover support depend on the format. Review messages and listen to a sample.

## Rename

Choose the naming mode and review proposed names. Sequential naming uses a prefix, starting number and padding. Filters and selection determine the batch.

**Only names matching** filters existing filenames using wildcards, such as `*.jpg`.
Leave it blank to rename all listed files. It does not set the new filename; an
unmatched input is skipped and its reason appears in Preview.

Rename changes existing filenames. **Preview undo** is available for supported completed batches when file identities and original destinations still match. External changes or occupied original names can block undo.

## Archives

Choose 7z, ZIP or CBZ, then the destination, name and conflict policy.

- **One archive:** combine selected/visible inputs.
- **Archive per input:** produce one archive per input row. A folder row produces an archive of that folder's planned contents.

Compression choices are Store (fastest), Moderate (fast), Balanced (normal) and Maximum (slowest). Already compressed media may gain little from higher compression.

Enter a password for supported encryption; leave it empty for no encryption. The eye controls visibility, with confirmation while hidden. 7z filename encryption defaults on with protection. ZIP names stay visible. CBZ encryption is unavailable for reader compatibility. Control characters are not allowed in archive passwords.

Hidden-file/folder options and exclusions control contents. Check **Archive contents** in Preview. Creation keeps inputs; any offered cleanup is a separate explicit action after successful verification.

## Documents

Choose the task first. PDF page selections are one-based: `1, 3-5` selects pages 1, 3, 4 and 5. Supported tasks follow the specified order; invalid ranges and duplicates are rejected.

- **Merge / split / extract / reorder / remove / rotate:** edit page sequence or orientation.
- **Images to PDF:** combine ordered images with page size, orientation and margin settings.
- **PDF to images:** export PNG/JPEG at the chosen DPI. Higher DPI increases raster memory and output size.
- **Extract PDF text:** export an existing text layer to UTF-8 TXT without recreating page layout.
- **Optimize PDF:** choose **Lossless cleanup** to compress page data and remove
  duplicate objects, or **Compress images (JPEG)** to recompress opaque pictures
  at the chosen JPEG quality. Optional downsampling also reduces image resolution.
  Text and vectors remain intact; transparent images are preserved. Smaller output
  is not guaranteed, especially for PDFs that are already compressed.
- **PDF metadata / passwords:** edit supported fields or add/change/remove protection with the valid input password where needed.
- **OCR scanned pages:** Tesseract with installed language data produces searchable PDF or TXT.
- **Office to PDF:** LibreOffice converts DOCX/ODT, XLSX/ODS and PPTX/ODP. Fonts, print areas and pagination affect results.

Some PDF structures, including forms, signatures and active content, are blocked. Metadata removal is not redaction and does not remove visible confidential content. Macro-enabled, encrypted or externally linked Office documents may need manual conversion. Check order, appearance and text in an independent reader.

For OCR, first check **Tools → Dependencies → Tesseract**, including its language
data. In Documents, add a scanned PDF or image, choose **OCR scanned pages**, and
set **OCR languages** to `eng` for English, `hin` for Hindi, or `eng+hin` for both
(the selected language packs must be installed). Choose **Searchable PDF** to
keep page images with selectable/searchable text, or **TXT** for text only.
For ordinary A4/Letter scans, try **300 DPI**, then Preview and Start. OCR reads
printed text from scans; check the recognized text for mistakes.

While an operation runs, you can switch tools, add files, change settings and
preview another task. Use **Add to Queue** after previewing to run it afterward.
Documents lists PDF, supported image and Office sources; videos stay available
in Video and are hidden from the Documents list.
The active task keeps its saved inputs and settings. Only one processing job
runs at a time. Cancelling a new file scan does not cancel the running job;
use **Cancel operation** in its Results to stop it.

## Folders

**Inspect** calculates cached folder totals. Size limits filter individual files for inspection; depth/view filters do not change the tree. Maximum zero means no maximum. Use **Refresh** after external changes. Unreadable/changing trees can produce partial totals and scan issues.

**Flatten one level** moves `Root/Parent/Child` to `Root/Child`. Choose all or selected immediate parents, optionally include loose files, and review the preview. Inspection size limits do not limit flatten moves. Cross-volume moves are unsupported.

Empty-parent removal only considers affected parents that remain empty. Supported undo checks identities and destinations and can refuse changed trees or occupied original paths.

## Queue and workflows

Queue stays inside the main window. **Jobs** shows waiting/active work; **History** holds finished results and supported recovery actions.

To link operations:

1. Open **Queue → Workflow**. For a fresh draft, choose **New workflow** from Recipes.
2. Add inputs and choose the first step's tool.
3. Use **Configure step**, adjust the normal tool settings, and choose **Add step to workflow**.
4. Repeat for later steps and review their order and the Input / Process / Output preview.
5. Choose **Start workflow** or **Add workflow to Queue**. Save a recipe to reuse its configuration.

Example: `Original.mkv → Convert to MP4 → Rename → Archive as 7z`.

Each step consumes verified predecessor outputs. Previews can estimate names; actual names depend on execution/conflict handling. Workflows keep original inputs and generated results by default. Saved protected work can ask for password reentry after restarting.

### Pause, cancel and retry

Pause stops Queue from starting further work after the current unit finishes. Cancel requests stopping the active operation; some native image work must return first. Completed work stays recorded.

Retry through Queue/History. Depending on the task, retry restarts the current file or reconciles recorded moves/outputs. Undo requires a supported operation and valid saved state. Clearing history is separate from deleting media; review clearing options and retained undo records.

## Local data and troubleshooting

Settings, drafts and history stay in the operating system's local application-data/settings locations. History can contain filenames and paths. Password values are not saved as plaintext in settings or job snapshots. No account is required for local processing.

| Problem | What to check |
| --- | --- |
| Missing FFmpeg/FFprobe | Both executables must be discoverable; check Tools → Dependencies. |
| Encoder unavailable | The FFmpeg build must include the selected encoder and muxer. |
| Wrong batch inputs | Review tool, filters and selected rows. |
| Output conflict | Choose Skip/unique names or change destination. |
| OCR language missing | Install the language data and check again. |
| Empty PDF text export | A scan may need OCR. |
| Office layout differs | Check fonts, print areas and source page setup. |
| Recovery/undo blocked | Inspect the error for changed files or occupied destinations. |
| Qt cannot load xcb | Check display access and Qt/X11 libraries listed above. |

When reporting a problem, include versions, OS, operation and error text. Share only non-sensitive files and logs you have reviewed.

Use **Queue → History → Tools → Export support report** for technical versions,
allowlisted settings and outcomes. It excludes filenames, paths, passwords,
metadata and free-form logs. **Save report** retains the full job details and can
contain private paths. See [support instructions](../SUPPORT.md).
