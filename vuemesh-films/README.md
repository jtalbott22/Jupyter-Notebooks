# VUEMesh films: Qwen-Image stills, then LTX image to video

Three films (wedding first dance, concert, arena), 28 shots, about 40 seconds each. Every
shot has two prompts in `shots.json`: an image prompt (Qwen-Image makes the still) and a
motion prompt (LTX turns the still into a clip). One script runs both stages through your
ComfyUI, with a review page in between to pick the best still for each shot.

Screens in the shots (the kiosk, phones, laptops, the LED wall, the arena's big board) are
left plain white on purpose: the VUEMesh screens go on afterwards as overlays, so the codes
and the studio are exact and readable. Each shot's `overlay` says what goes there.

## Set up once

1. **Python 3.9 or newer** (nothing to install beyond it).
2. **Your two workflows, in API format.** In ComfyUI, open your Qwen-Image workflow and use
   Workflow > Export (API); save it here as `image_workflow.json`. Do the same with your LTX
   image-to-video workflow as `video_workflow.json`. (Turn on the dev mode setting if you
   don't see Export (API).)
3. **Check what the script found:**

   ```
   python comfy_pipeline.py inspect image_workflow.json
   python comfy_pipeline.py inspect video_workflow.json
   ```

   It lists the nodes it will set: the prompt, the negative prompt, the seed, the size, the
   length and frame rate (video), the start image (video) and the save node. It finds them
   on its own in most workflows. If one is wrong or missing, rename that node in ComfyUI
   (right-click > Title) to `IMAGE_PROMPT` / `VIDEO_PROMPT`, `NEGATIVE`, `SEED`, `SIZE`,
   `LENGTH`, `FPS`, `START_IMAGE` or `SAVE`, and export again.
4. **ComfyUI's address** is in `pipeline.json` (`http://127.0.0.1:8188` by default; use the
   machine's address if ComfyUI runs on another computer, and start ComfyUI with `--listen`).

`image_workflow.example.json` and `video_workflow.example.json` show the shape the script
expects; use your own workflows, not these.

## In Jupyter (a Docker container)

Open `vuemesh_films.ipynb` and run the cells in order: it checks it can reach ComfyUI (and
shows the GPU), inspects your workflows, makes the stills, lets you pick one per shot right in
the notebook (thumbnails with buttons; picks save as you click), makes the clips, and plays
them inline.

**Reaching ComfyUI from inside the container:** there, `127.0.0.1` is the container itself.

- **ComfyUI in another container on the same Docker network** (e.g. the same compose file):
  use its service name, `http://comfyui:8188`.
- **ComfyUI on the host:** `http://host.docker.internal:8188`, and start ComfyUI with
  `--listen` (it only answers itself otherwise). Docker Desktop (Windows, Mac) knows that
  name; on Linux, add it to the Jupyter service (below).
- **ComfyUI on another computer:** its address, e.g. `http://192.168.1.20:8188`, with `--listen`.

Set it in the notebook's first cell, or as `COMFY_URL` in the container's environment. And
put this folder somewhere the container can see (a volume), so the renders land on your disk:

```yaml
services:
  jupyter:
    # ...your Jupyter image...
    volumes:
      - ./vuemesh-films:/home/jovyan/work/vuemesh-films
    environment:
      COMFY_URL: http://comfyui:8188            # or http://host.docker.internal:8188
    extra_hosts:
      - "host.docker.internal:host-gateway"     # Linux, with ComfyUI on the host
```

The picker uses ipywidgets (in most Jupyter images); without it the notebook shows the stills
with their numbers and you set picks in a line of code. Thumbnails need Pillow (PIL), which
most images have; otherwise it shows the full stills.

## Make the films (from a terminal)

```
python comfy_pipeline.py stills            # 4 stills per shot (or --variants 2), every film
python comfy_pipeline.py review            # opens the review page: click the best still per shot,
                                           # Save picks, put picks.json in the renders folder
python comfy_pipeline.py videos            # each picked still becomes a clip
```

- **One film or one shot:** `--only wedding`, or `--only wedding/05-dance-wide`.
- **Again:** anything already made is skipped; `--force` makes it again (new seed).
- **All at once, no review:** `python comfy_pipeline.py all` (uses still 1 for every shot).
- **Music (ACE-Step):** export your ACE-Step workflow as `music_workflow.json` (check it with `inspect`), then `python comfy_pipeline.py music` makes three instrumental takes per film, each as long as its cut, in the style of its `music.tags` in `shots.json`. Pick one per film on the review page or in the notebook; the cut uses it. If takes sound alike or identical, make them again with `--force` (or `force=True` in the notebook): each take gets its own seed, including seeds that come from a separate Int/PrimitiveInt node (ACE-Step's template does this). Your own recording works too: put it in `renders/music/<film>/` as `music_v1.wav`.
- **The finished films:** `python comfy_pipeline.py cut` (or the notebook's last section) edits each film as its `edit` in `shots.json` says (which part of each clip, its speed, cut / dissolve / through black / a white flash, the title, a colour grade, the end card) into `renders/final/wedding.mp4`, `concert.mp4`, `arena.mp4`, and `reel.mp4` with all three. Each uses its picked music take, fading in and out (`--music song.mp3` overrides that); with no music, it's silent. Needs ffmpeg (or `pip install imageio-ffmpeg`).
- Every clip in one folder with a list: `python comfy_pipeline.py collect` (it writes `renders/final/clips/` with `wedding-01-venue-v1.mp4`-style names and a `list.txt` saying which still was chosen and which shots still need their on-screen overlay).
- Everything lands in `renders/<film>/<shot>/`: `still_v1.png`… and `clip_v2.mp4` (the number
  is the still it came from). The review page shows the clips too.

## Sizes and lengths

From `shots.json` (`defaults`, or per shot with `image_settings` / `video_settings`):

- **Stills:** 1664 × 928 (16:9, a size Qwen-Image is made for).
- **Clips:** 1280 × 704 at 24 fps (both sides divisible by 32, as LTX needs); each shot's
  `seconds` becomes a frame count LTX accepts (8n + 1: 4 s is 97 frames, 6 s is 145).

Change them to suit your GPU: smaller clips render much faster for a first pass.

## The prompts

- **Image prompts** describe the frame: the people, the place, the light, the lens. Screens
  are described as plain white.
- **Motion prompts** say what happens, in order, and what the camera does. Shots with a
  screen to fill hold the camera steady (on a tripod) so the overlay can be placed cleanly.
- Everyone and everything is fictional: no real teams, brands or logos.

Edit `shots.json` freely; the review page shows each shot's prompts and its overlay note.
