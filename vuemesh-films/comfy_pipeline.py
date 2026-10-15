#!/usr/bin/env python3
"""VUEMesh films through ComfyUI, in two stages per shot.

  1. stills   Qwen-Image makes a few stills for every shot (your workflow, your settings).
  2. review   A page shows them side by side; pick one per shot.
  3. videos   LTX turns each picked still into a clip, with the shot's motion prompt.

Uses your own two workflows, exported from ComfyUI with "Export (API)". Only the Python
standard library is needed (Python 3.9 or newer), on Windows, macOS or Linux.

    python comfy_pipeline.py inspect image_workflow.json      # see what it found in a workflow
    python comfy_pipeline.py stills                           # every shot (skips what's done)
    python comfy_pipeline.py stills --only wedding            # one film (or wedding/05-dance-wide)
    python comfy_pipeline.py review                           # opens the review page
    python comfy_pipeline.py videos                           # every picked still to a clip
    python comfy_pipeline.py all                              # stills, then videos from the first of each

Settings (server, workflow files, output folder) are in pipeline.json beside this script;
the shots and their prompts are in shots.json.
"""
import argparse
import copy
import json
import mimetypes
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))


class PipelineError(Exception):
    """Something to fix (ComfyUI unreachable, a node not found, a render failed): said plainly."""
CLIENT = str(uuid.uuid4())

# ---------------------------------------------------------------- settings --

DEFAULTS = {
    "server": "http://127.0.0.1:8188",
    "image_workflow": "image_workflow.json",
    "video_workflow": "video_workflow.json",
    "music_workflow": "music_workflow.json",
    "shots": "shots.json",
    "out": "renders",
    "timeout_image_s": 900,
    "timeout_video_s": 3600,
    "timeout_music_s": 900,
}


def load_settings(**overrides):
    path = os.path.join(HERE, "pipeline.json")
    s = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            s.update(json.load(f))
    s.update({k: v for k, v in overrides.items() if v is not None})
    for k in ("image_workflow", "video_workflow", "music_workflow", "shots", "out"):
        if not os.path.isabs(s[k]):
            s[k] = os.path.join(HERE, s[k])
    return s


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------- the workflow --
# A workflow in API format is {node_id: {"class_type": ..., "inputs": {...}, "_meta": {"title": ...}}}.
# Each thing the script sets is found by the node's title first (rename a node in ComfyUI to
# one of these to be exact), then by what the node is. "inspect" shows what was found.

TITLES = {
    "prompt": ("VIDEO_PROMPT", "IMAGE_PROMPT", "PROMPT", "POSITIVE"),
    "negative": ("NEGATIVE",),
    "seed": ("SEED",),
    "size": ("SIZE",),
    "length": ("LENGTH",),
    "fps": ("FPS",),
    "image": ("START_IMAGE",),
    "save": ("SAVE",),
    # (music: ACE-Step's style tags, lyrics, length and tempo)
    "tags": ("MUSIC_TAGS", "TAGS"),
    "lyrics": ("LYRICS",),
    "seconds": ("SECONDS", "DURATION"),
    "bpm": ("BPM",),
}


def title(node):
    return str((node.get("_meta") or {}).get("title") or "").strip().upper()


def is_link(v):
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], str)


def text_key(node):
    for k in ("text", "prompt", "positive_prompt"):
        if isinstance(node.get("inputs", {}).get(k), str):
            return k
    return None


def trace_text(wf, link, side="positive", seen=None):
    """Follow a conditioning link upstream to the node holding the prompt's text, keeping to
    the same side (positive or negative) through conditioning nodes that carry both."""
    seen = seen or set()
    nid = link[0]
    if nid in seen or nid not in wf:
        return None
    seen.add(nid)
    node = wf[nid]
    if text_key(node):
        return nid
    ins = node.get("inputs", {})
    other = "negative" if side == "positive" else "positive"
    order = ([ins[side]] if is_link(ins.get(side)) else []) + [v for k, v in ins.items() if is_link(v) and k not in (side, other)]
    if not order and is_link(ins.get(other)) and side == "positive":
        order = [ins[other]]
    for v in order:
        hit = trace_text(wf, v, side, seen)
        if hit:
            return hit
    return None


def find_roles(wf, stage):
    """Which nodes take the prompt, negative, seed, size, length, fps, start image and save prefix."""
    roles = {k: [] for k in TITLES}
    by_title = {}
    for nid, n in wf.items():
        t = title(n)
        for role, names in TITLES.items():
            if t in names and not (role == "prompt" and t == ("IMAGE_PROMPT" if stage == "video" else "VIDEO_PROMPT")):
                by_title.setdefault(role, []).append(nid)
    # prompt and negative: the sampler's positive and negative inputs, traced back to their text
    if "prompt" not in by_title or "negative" not in by_title:
        for nid, n in wf.items():
            ins = n.get("inputs", {})
            if is_link(ins.get("positive")):
                p = trace_text(wf, ins["positive"], "positive")
                if p and "prompt" not in by_title:
                    roles["prompt"] = [p]
                if is_link(ins.get("negative")):
                    q = trace_text(wf, ins["negative"], "negative")
                    if q and q != p and "negative" not in by_title:
                        roles["negative"] = [q]
                if roles["prompt"]:
                    break
    if not roles["prompt"] and "prompt" not in by_title:
        texts = [nid for nid, n in wf.items() if text_key(n)]
        if len(texts) == 1:
            roles["prompt"] = texts
    for nid, n in wf.items():
        ins = n.get("inputs", {})
        if any(isinstance(ins.get(k), int) and not isinstance(ins.get(k), bool) for k in ("seed", "noise_seed")):
            roles["seed"].append(nid)
        if isinstance(ins.get("width"), int) and isinstance(ins.get("height"), int):
            roles["size"].append(nid)
        if any(isinstance(ins.get(k), int) for k in ("length", "num_frames", "frames", "frame_count")):
            roles["length"].append(nid)
        if any(isinstance(ins.get(k), (int, float)) and not isinstance(ins.get(k), bool) for k in ("frame_rate", "fps")):
            roles["fps"].append(nid)
        if n.get("class_type") == "LoadImage" or (isinstance(ins.get("image"), str) and "Load" in n.get("class_type", "")):
            roles["image"].append(nid)
        if isinstance(ins.get("filename_prefix"), str):
            roles["save"].append(nid)
        if any(isinstance(ins.get(k), str) for k in ("tags", "caption", "style")):
            roles["tags"].append(nid)
        if isinstance(ins.get("lyrics"), str):
            roles["lyrics"].append(nid)
        if any(isinstance(ins.get(k), (int, float)) and not isinstance(ins.get(k), bool) for k in ("seconds", "duration", "audio_duration")):
            roles["seconds"].append(nid)
        if isinstance(ins.get("bpm"), (int, float)) and not isinstance(ins.get("bpm"), bool):
            roles["bpm"].append(nid)
    for role, ids in by_title.items():
        roles[role] = ids
    return roles


def need(roles, wanted, path):
    missing = [r for r in wanted if not roles.get(r)]
    if missing:
        hints = ", ".join(f"{r} (title a node {TITLES[r][0] if r != 'prompt' else 'IMAGE_PROMPT or VIDEO_PROMPT'})" for r in missing)
        raise PipelineError(f"In {os.path.basename(path)} I couldn't find: {hints}. Run: python comfy_pipeline.py inspect {os.path.basename(path)}")


def set_inputs(wf, ids, values):
    for nid in ids:
        ins = wf[nid]["inputs"]
        for keys, v in values:
            for k in keys:
                if k in ins and not is_link(ins[k]):
                    ins[k] = v


# --------------------------------------------------------------- ComfyUI --

class Comfy:
    def __init__(self, server):
        self.server = server.rstrip("/")

    def _req(self, path, data=None, headers=None, timeout=60):
        req = urllib.request.Request(self.server + path, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:800]
            raise PipelineError(f"ComfyUI said {e.code} for {path}: {body}")
        except urllib.error.URLError as e:
            raise PipelineError(f"Can't reach ComfyUI at {self.server} ({e.reason}). Is it running? (set \"server\" in pipeline.json)")

    def queue(self, wf):
        body = json.dumps({"prompt": wf, "client_id": CLIENT}).encode()
        r = json.loads(self._req("/prompt", body, {"Content-Type": "application/json"}))
        if r.get("node_errors"):
            raise PipelineError("ComfyUI didn't accept the workflow: " + json.dumps(r["node_errors"])[:800])
        return r["prompt_id"]

    def wait(self, pid, timeout_s, label):
        start, dots = time.time(), 0
        while True:
            h = json.loads(self._req(f"/history/{pid}") or b"{}")
            if pid in h:
                st = h[pid].get("status", {})
                if st.get("status_str") == "error":
                    msgs = [m for m in st.get("messages", []) if m and m[0] == "execution_error"]
                    raise PipelineError(f"{label}: ComfyUI hit an error: {json.dumps(msgs)[:800]}")
                if st.get("completed", True):
                    print(f"\r  {label}: done in {int(time.time() - start)} s" + " " * 10)
                    return h[pid].get("outputs", {})
            if time.time() - start > timeout_s:
                raise PipelineError(f"{label}: still not done after {timeout_s} s (raise timeout_* in pipeline.json)")
            dots = (dots + 1) % 4
            print(f"\r  {label}: rendering{'.' * dots}{' ' * (3 - dots)} {int(time.time() - start)} s", end="", flush=True)
            time.sleep(2)

    def files(self, outputs, exts):
        found = []
        for node_out in outputs.values():
            for key in ("images", "gifs", "videos", "video", "animated", "files", "audio"):
                for f in node_out.get(key, []) or []:
                    if isinstance(f, dict) and f.get("filename", "").lower().endswith(exts):
                        found.append(f)
        return found

    def download(self, f, dest):
        q = urllib.parse.urlencode({"filename": f["filename"], "subfolder": f.get("subfolder", ""), "type": f.get("type", "output")})
        data = self._req(f"/view?{q}", timeout=600)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as out:
            out.write(data)

    def upload(self, path, name=None, subfolder="vuemesh"):
        boundary = uuid.uuid4().hex
        name = name or os.path.basename(path)
        with open(path, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(name)[0] or "image/png"
        parts = []
        for k, v in (("overwrite", "true"), ("type", "input"), ("subfolder", subfolder)):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\nContent-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        r = json.loads(self._req("/upload/image", b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={boundary}"}))
        return f"{r['subfolder']}/{r['name']}" if r.get("subfolder") else r["name"]


# ------------------------------------------------------------------ shots --

def shot_list(shots_doc, only):
    for film in shots_doc["films"]:
        for shot in film["shots"]:
            key = f"{film['id']}/{shot['id']}"
            if not only or key == only or film["id"] == only:
                yield film, shot, key


def frames_for(seconds, fps):
    # LTX wants 8n+1 frames
    return max(9, int(round(seconds * fps / 8.0)) * 8 + 1)


def still_path(out, key, v):
    return os.path.join(out, *key.split("/"), f"still_v{v}.png")


def picks_path(out):
    return os.path.join(out, "picks.json")


def load_picks(out):
    p = picks_path(out)
    return load_json(p) if os.path.exists(p) else {}


# ------------------------------------------------------------------ stages --

def stage_stills(s, args):
    doc, base = load_json(s["shots"]), load_json(s["image_workflow"])
    roles = find_roles(base, "image")
    need(roles, ("prompt", "save"), s["image_workflow"])
    d = doc["defaults"]["image"]
    comfy = Comfy(s["server"])
    for film, shot, key in shot_list(doc, args.only):
        o = {**d, **(shot.get("image_settings") or {})}
        n = args.variants or o.get("variants", 4)
        for v in range(1, n + 1):
            dest = still_path(s["out"], key, v)
            if os.path.exists(dest) and not args.force:
                continue
            wf = copy.deepcopy(base)
            set_inputs(wf, roles["prompt"], [((text_key(wf[roles["prompt"][0]]),), shot["image"])])
            if roles["negative"] and o.get("negative") is not None:
                set_inputs(wf, roles["negative"], [((text_key(wf[roles["negative"][0]]),), o["negative"])])
            set_inputs(wf, roles["seed"], [(("seed", "noise_seed"), shot.get("seed", 1000) * 100 + v if args.fixed_seeds else random.randrange(1, 2**31))])
            set_inputs(wf, roles["size"], [(("width",), o["width"]), (("height",), o["height"])])
            set_inputs(wf, roles["save"], [(("filename_prefix",), f"vuemesh/{film['id']}_{shot['id']}_v{v}")])
            files = comfy.files(comfy.wait(comfy.queue(wf), s["timeout_image_s"], f"{key} still {v}/{n}"), (".png", ".jpg", ".jpeg", ".webp"))
            if not files:
                raise PipelineError(f"{key}: the workflow finished but saved no image (is there a Save Image node?)")
            comfy.download(files[0], dest)
    write_review(s, doc)
    print(f"Stills are in {s['out']}. Next: pick one per shot (the notebook's picker, or: python comfy_pipeline.py review)")


def stage_videos(s, args):
    doc, base = load_json(s["shots"]), load_json(s["video_workflow"])
    roles = find_roles(base, "video")
    need(roles, ("prompt", "image", "save"), s["video_workflow"])
    d = doc["defaults"]["video"]
    picks = load_picks(s["out"])
    comfy = Comfy(s["server"])
    for film, shot, key in shot_list(doc, args.only):
        v = int(picks.get(key, 1))
        still = still_path(s["out"], key, v)
        if not os.path.exists(still):
            print(f"  {key}: no still {v} yet (run stills first); skipped")
            continue
        dest = os.path.join(s["out"], *key.split("/"), f"clip_v{v}.mp4")
        if os.path.exists(dest) and not args.force:
            continue
        o = {**d, **(shot.get("video_settings") or {})}
        fps = o["fps"]
        wf = copy.deepcopy(base)
        name = comfy.upload(still, f"{film['id']}_{shot['id']}_still_v{v}.png")  # (each shot its own name in ComfyUI's input folder)
        set_inputs(wf, roles["image"], [(("image",), name)])
        set_inputs(wf, roles["prompt"], [((text_key(wf[roles["prompt"][0]]),), shot["motion"])])
        if roles["negative"] and o.get("negative") is not None:
            set_inputs(wf, roles["negative"], [((text_key(wf[roles["negative"][0]]),), o["negative"])])
        set_inputs(wf, roles["seed"], [(("seed", "noise_seed"), random.randrange(1, 2**31))])
        set_inputs(wf, roles["size"], [(("width",), o["width"]), (("height",), o["height"])])
        set_inputs(wf, roles["length"], [(("length", "num_frames", "frames", "frame_count"), frames_for(shot.get("seconds", 4), fps))])
        set_inputs(wf, roles["fps"], [(("frame_rate", "fps"), fps)])
        set_inputs(wf, roles["save"], [(("filename_prefix",), f"vuemesh/{film['id']}_{shot['id']}")])
        files = comfy.files(comfy.wait(comfy.queue(wf), s["timeout_video_s"], f"{key} clip (still {v})"), (".mp4", ".webm", ".mov", ".gif", ".webp"))
        if not files:
            raise PipelineError(f"{key}: the workflow finished but saved no video (is there a Save Video / Video Combine node?)")
        ext = os.path.splitext(files[0]["filename"])[1].lower()
        dest = os.path.splitext(dest)[0] + ext
        comfy.download(files[0], dest)
    write_review(s, doc)
    print(f"Clips are in {s['out']} (each shot's folder): watch them in the notebook, or on the review page.")


def planned_seconds(film):
    """How long the film's cut runs, from its edit (the music is made a little longer)."""
    ed = film.get("edit") or {}
    cuts = ed.get("cuts", [])
    t = sum((c.get("out", 4) - c.get("in", 0)) / c.get("speed", 1) for c in cuts)
    t -= sum(c.get("dur", 0.5) for c in cuts[1:] if c.get("into", "cut") != "cut")
    if ed.get("end"):
        t += ed["end"].get("seconds", 3) - 0.8
    return max(5.0, t)


AUDIO = (".flac", ".mp3", ".wav", ".opus", ".ogg", ".m4a")


def music_path(out, film_id, v):
    folder = os.path.join(out, "music", film_id)
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            if name.startswith(f"music_v{v}.") and name.lower().endswith(AUDIO):
                return os.path.join(folder, name)
    return None


def music_for(out, film_id, picks):
    """The picked music take for a film, else take 1, else none. (Put your own recording there as music_v1.wav to use it.)"""
    return music_path(out, film_id, picks.get(f"music/{film_id}", 1)) or music_path(out, film_id, 1)


def stage_music(s, args):
    doc, base = load_json(s["shots"]), load_json(s["music_workflow"])
    roles = find_roles(base, "music")
    if not roles["tags"] and roles["prompt"]:
        roles["tags"] = roles["prompt"]   # (a workflow that takes the style as a plain prompt)
    need(roles, ("tags", "save"), s["music_workflow"])
    comfy = Comfy(s["server"])
    for film in doc["films"]:
        if args.only and film["id"] != args.only:
            continue
        m = film.get("music")
        if not m:
            continue
        seconds = round(planned_seconds(film) + 2)
        n = args.variants or m.get("variants", 3)
        for v in range(1, n + 1):
            if music_path(s["out"], film["id"], v) and not args.force:
                continue
            wf = copy.deepcopy(base)
            for nid in roles["tags"]:
                ins = wf[nid]["inputs"]
                for k in ("tags", "caption", "style", "text", "prompt"):
                    if isinstance(ins.get(k), str):
                        ins[k] = m["tags"]
                        break
            set_inputs(wf, roles["lyrics"], [(("lyrics",), m.get("lyrics", "[instrumental]"))])
            set_inputs(wf, roles["seconds"], [(("seconds", "duration", "audio_duration"), seconds)])
            if m.get("bpm"):
                set_inputs(wf, roles["bpm"], [(("bpm",), m["bpm"])])
            set_inputs(wf, roles["seed"], [(("seed", "noise_seed"), random.randrange(1, 2**31))])
            set_inputs(wf, roles["save"], [(("filename_prefix",), f"vuemesh/music_{film['id']}_v{v}")])
            files = comfy.files(comfy.wait(comfy.queue(wf), s.get("timeout_music_s", 900), f"{film['id']} music {v}/{n} ({seconds} s)"), AUDIO)
            if not files:
                raise PipelineError(f"{film['id']}: the music workflow finished but saved no audio (is there a Save Audio node?)")
            ext = os.path.splitext(files[0]["filename"])[1].lower()
            comfy.download(files[0], os.path.join(s["out"], "music", film["id"], f"music_v{v}{ext}"))
    write_review(s, doc)
    print(f"Music is in {os.path.join(s['out'], 'music')}. Pick a take per film (the notebook, or the review page); the cut uses it.")


def music(only=None, variants=None, force=False, **settings):
    """ACE-Step music for each film (from a notebook, or: python comfy_pipeline.py music)."""
    stage_music(load_settings(**settings), _args(only=only, variants=variants, force=force))


# ------------------------------------------------------------------ review --

def write_review(s, doc):
    """A page of every shot's stills (click one to pick it; Save picks writes picks.json) and its clip."""
    out, picks = s["out"], load_picks(s["out"])
    rows = []
    for film in doc["films"]:
        rows.append(f"<h2>{film['title']}</h2><p class=story>{film.get('story', '')}</p>")
        mdir = os.path.join(out, "music", film["id"])
        takes = sorted(f for f in os.listdir(mdir) if f.startswith("music_v") and f.lower().endswith(AUDIO)) if os.path.isdir(mdir) else []
        if takes or film.get("music"):
            mk = f"music/{film['id']}"
            mp = int(picks.get(mk, 1))
            players = "".join(
                f"<button class='v m{' on' if int(t[7:].split('.')[0]) == mp else ''}' data-key='{mk}' data-v='{t[7:].split('.')[0]}'><span>{t[7:].split('.')[0]}</span><audio controls preload=none src='music/{film['id']}/{t}'></audio></button>"
                for t in takes) or "<em>no music yet (python comfy_pipeline.py music)</em>"
            rows.append(f"<section><h3>Music <small>{(film.get('music') or {}).get('tags', '')}</small></h3><div class=row>{players}</div></section>")
        for shot in film["shots"]:
            key = f"{film['id']}/{shot['id']}"
            folder = os.path.join(out, *key.split("/"))
            stills = sorted(f for f in os.listdir(folder) if f.startswith("still_v")) if os.path.isdir(folder) else []
            clips = sorted(f for f in os.listdir(folder) if f.startswith("clip_v")) if os.path.isdir(folder) else []
            pick = int(picks.get(key, 1))
            imgs = "".join(
                f"<button class='v{' on' if int(f[7:-4]) == pick else ''}' data-key='{key}' data-v='{f[7:-4]}'><img loading=lazy src='{key}/{f}' alt='{key} still {f[7:-4]}'><span>{f[7:-4]}</span></button>"
                for f in stills) or "<em>no stills yet</em>"
            vids = "".join(f"<video controls preload=none src='{key}/{c}'></video>" for c in clips)
            ov = f"<p class=ov>Overlay: {shot['overlay']}</p>" if shot.get("overlay") else ""
            rows.append(f"<section><h3>{shot['id']} <small>{shot.get('seconds', 4)} s</small></h3><p>{shot['image']}</p><p class=mo>{shot['motion']}</p>{ov}<div class=row>{imgs}</div>{vids}</section>")
    html = f"""<!doctype html><meta charset=utf-8><title>VUEMesh films: review</title>
<style>
body{{font:15px/1.45 system-ui,sans-serif;background:#121519;color:#eef2f6;margin:0;padding:24px 32px 120px}}
h1{{margin:0 0 4px}} h2{{margin:40px 0 4px;border-top:1px solid #2f3640;padding-top:24px}} .story{{color:#b4bcc8;margin:0 0 12px}}
section{{margin:18px 0 28px}} h3{{margin:0 0 4px}} h3 small{{color:#808a98;font-weight:400}}
section p{{margin:2px 0;color:#b4bcc8;max-width:110ch}} .mo{{color:#9fd7b1}} .ov{{color:#ffb21e}}
.row{{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}} .v{{position:relative;padding:0;border:3px solid #2f3640;border-radius:6px;background:none;cursor:pointer}}
.v img{{display:block;width:300px;border-radius:3px}} .v span{{position:absolute;left:6px;top:6px;background:#000c;color:#fff;font-weight:700;padding:0 6px;border-radius:3px}}
.v.on{{border-color:#2fd56b;box-shadow:0 0 14px #2fd56b88}} .v.m{{padding:26px 8px 8px}} .v.m span{{top:4px}} .v.m audio{{width:300px;display:block}} video{{width:480px;margin:6px 10px 0 0;border-radius:4px;background:#000}}
.bar{{position:fixed;left:0;right:0;bottom:0;background:#05070a;border-top:1px solid #2f3640;padding:12px 32px;display:flex;gap:14px;align-items:center}}
.bar button{{font:700 15px system-ui;background:#ff2f2a;color:#fff;border:0;border-radius:4px;padding:10px 18px;cursor:pointer}}
</style>
<h1>VUEMesh films: review</h1><p class=story>Click the still you want for each shot (green), then <b>Save picks</b> and put the downloaded <code>picks.json</code> in <code>{out}</code>. Then run <code>python comfy_pipeline.py videos</code>.</p>
{''.join(rows)}
<div class=bar><button id=save>Save picks</button><span id=n></span></div>
<script>
const picks = {json.dumps(picks)};
document.querySelectorAll('.v').forEach(b => b.onclick = () => {{
  document.querySelectorAll(`.v[data-key="${{b.dataset.key}}"]`).forEach(x => x.classList.remove('on'));
  b.classList.add('on'); picks[b.dataset.key] = +b.dataset.v; count();
}});
const count = () => document.querySelector('#n').textContent = Object.keys(picks).length + ' shots picked (the rest use still 1)';
count();
document.querySelector('#save').onclick = () => {{
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([JSON.stringify(picks, null, 2)], {{type: 'application/json'}}));
  a.download = 'picks.json'; a.click();
}};
</script>"""
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "review.html"), "w", encoding="utf-8") as f:
        f.write(html)
    return os.path.join(out, "review.html")


# ------------------------------------------------- from a notebook (or Python) --

def _args(**kw):
    from types import SimpleNamespace
    return SimpleNamespace(**{"only": None, "force": False, "variants": None, "fixed_seeds": False, **kw})


def check(server=None):
    """Can this machine reach ComfyUI? Returns its system stats (GPU, memory) or says why not."""
    s = load_settings(server=server)
    info = json.loads(Comfy(s["server"])._req("/system_stats"))
    dev = (info.get("devices") or [{}])[0]
    print(f"ComfyUI at {s['server']}: {dev.get('name', 'a device')}, "
          f"{round((dev.get('vram_free') or 0) / 2**30, 1)} of {round((dev.get('vram_total') or 0) / 2**30, 1)} GB free")
    return info


def stills(only=None, variants=None, force=False, fixed_seeds=False, **settings):
    """Qwen-Image stills for every shot (or one film, or one shot)."""
    stage_stills(load_settings(**settings), _args(only=only, variants=variants, force=force, fixed_seeds=fixed_seeds))


def videos(only=None, force=False, **settings):
    """LTX clips from the picked stills."""
    stage_videos(load_settings(**settings), _args(only=only, force=force))


def inspect(workflow, **settings):
    cmd_inspect(load_settings(**settings), _args(workflow=workflow))


def shots(**settings):
    """[(film, shot, "film/shot")] in order."""
    s = load_settings(**settings)
    return list(shot_list(load_json(s["shots"]), None))


# ---------------------------------------------------------------- commands --

def cmd_inspect(s, args):
    path = args.workflow if os.path.isabs(args.workflow) else os.path.join(HERE, args.workflow)
    wf = load_json(path)
    if "nodes" in wf and "links" in wf:
        raise PipelineError("That's the regular (UI) format. In ComfyUI use Workflow > Export (API), and inspect that file.")
    is_music = any(isinstance(n.get("inputs", {}).get(k), str) for n in wf.values() for k in ("tags", "lyrics")) or "Audio" in " ".join(n.get("class_type", "") for n in wf.values()) or "music" in os.path.basename(path).lower()
    stage = "music" if is_music else "video" if any(isinstance(n.get("inputs", {}).get(k), int) for n in wf.values() for k in ("length", "num_frames", "frames")) or "video" in os.path.basename(path).lower() else "image"
    roles = find_roles(wf, stage)
    if stage == "music" and not roles["tags"] and roles["prompt"]:
        roles["tags"] = roles["prompt"]
    print(f"{os.path.basename(path)}: looks like the {stage} workflow")
    wanted = {"image": ("prompt", "negative", "seed", "size", "save"),
              "video": ("prompt", "negative", "seed", "size", "length", "fps", "image", "save"),
              "music": ("tags", "lyrics", "seconds", "bpm", "seed", "save")}[stage]
    for role in wanted:
        ids = roles.get(role) or []
        names = ", ".join(f"{i} {wf[i].get('class_type')}" + (f" \"{title(wf[i])}\"" if title(wf[i]) else "") for i in ids) or "-- not found"
        print(f"  {role:9} {names}")
    names = {r: ("IMAGE_PROMPT" if stage == "image" else "VIDEO_PROMPT") if r == "prompt" else TITLES[r][0] for r in wanted}
    if stage == "music":
        print("  (lyrics and bpm are optional: without them, the style tags carry \"instrumental\" and the tempo)")
    print("If something's wrong or missing, rename that node in ComfyUI (right-click > Title) to one of:",
          ", ".join(f"{r}: {names[r]}" for r in wanted), "and export again.")


# ------------------------------------------------------------ the finished films --

def ffmpeg_exe():
    """ffmpeg on the PATH, or the one the imageio-ffmpeg package brings (pip install imageio-ffmpeg)."""
    import shutil
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise PipelineError("ffmpeg isn't here. Install it (apt-get install ffmpeg), or: pip install imageio-ffmpeg")


def clip_seconds(path):
    import re, subprocess
    out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", path], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def clip_for(out, key, picks):
    """The clip made from the picked still, or else any clip of that shot."""
    folder = os.path.join(out, *key.split("/"))
    if not os.path.isdir(folder):
        return None
    clips = sorted(f for f in os.listdir(folder) if f.startswith("clip_v"))
    want = [f for f in clips if f.split(".")[0] == f"clip_v{picks.get(key, 1)}"]
    pick = (want or clips or [None])[0]
    return os.path.join(folder, pick) if pick else None


def _collect(s, dest=None):
    """Every finished clip in one folder: <film>-<shot>-v<still>.mp4, and a list."""
    import shutil
    doc = load_json(s["shots"])
    dest = dest or os.path.join(s["out"], "final", "clips")
    os.makedirs(dest, exist_ok=True)
    rows, n, picks = [], 0, load_picks(s["out"])
    for film, shot, key in shot_list(doc, None):
        folder = os.path.join(s["out"], *key.split("/"))
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.startswith("clip_v") and name.rsplit(".", 1)[-1].lower() in ("mp4", "webm", "mov", "gif", "webp"):
                v = name[len("clip_v"):].split(".")[0]
                target = f"{film['id']}-{shot['id']}-v{v}.{name.rsplit('.', 1)[-1]}"
                shutil.copy2(os.path.join(folder, name), os.path.join(dest, target))
                rows.append((target, "chosen" if str(picks.get(key, 1)) == v else "other still", film["title"], shot.get("overlay") or ""))
                n += 1
    with open(os.path.join(dest, "list.txt"), "w", encoding="utf-8") as f:
        f.write("film-shot-still.mp4 | which still | film | overlay needed on this shot\n")
        for r in rows:
            f.write(" | ".join(r) + "\n")
    print(f"{n} clips collected into {dest} (list.txt says which are the chosen stills, and which shots need an overlay)")
    return dest


W, H, FPS = 1280, 720, 24
LOOKS = {
    # (a light grade, so clips made one at a time look like one film)
    "warm": "eq=contrast=1.04:saturation=1.06:gamma=1.02,colorbalance=rs=0.04:gs=0.01:bs=-0.04:rm=0.03:bm=-0.03,vignette=angle=PI/5",
    "punchy": "eq=contrast=1.12:saturation=1.15,vignette=angle=PI/4.5",
    "cool": "eq=contrast=1.08:saturation=1.04,colorbalance=rs=-0.02:bs=0.04:bm=0.03",
    "none": "null",
}
XFADE = {"cut": "fade", "dissolve": "fade", "black": "fadeblack", "flash": "fadewhite"}


def _q(path):
    # (inside single quotes ffmpeg takes everything as it is, colons included)
    return "'" + path.replace("\\", "/").replace("'", r"'\''") + "'"


def _fade_alpha(a, b, ramp=0.4):
    a, b, r = round(a, 3), round(b, 3), round(ramp, 3)
    return f"'if(lt(t,{a}),0,if(lt(t,{round(a + r, 3)}),(t-{a})/{r},if(lt(t,{round(b - r, 3)}),1,if(lt(t,{b}),({b}-t)/{r},0))))'"


def _render_film(s, film, music=None, dest=None):
    import subprocess, tempfile
    ed = film.get("edit")
    if not ed:
        raise PipelineError(f"{film['id']}: no edit in shots.json")
    picks, fonts = load_picks(s["out"]), os.path.join(HERE, "fonts")
    music = music or music_for(s["out"], film["id"], picks)   # (the picked take, if there's music made)
    bold, medium = os.path.join(fonts, "BarlowCondensed-Bold.ttf"), os.path.join(fonts, "BarlowCondensed-Medium.ttf")
    tmp = tempfile.mkdtemp(prefix="vuemesh-cut-")
    def textfile(name, text):
        p = os.path.join(tmp, name)
        open(p, "w", encoding="utf-8").write(text)
        return _q(p)
    inputs, segs, skipped = [], [], []
    count = lambda: sum(1 for x in inputs if x == "-i")   # (the next input's number)
    look = LOOKS.get(ed.get("look", "none"), "null")
    for c in ed["cuts"]:
        key = f"{film['id']}/{c['shot']}"
        path = clip_for(s["out"], key, picks)
        if not path:
            skipped.append(c["shot"])
            continue
        length = clip_seconds(path)
        a, b = c.get("in", 0.3), min(c.get("out", length), max(0.1, length - 0.05))
        if b - a < 0.5:
            a, b = 0, length
        speed = c.get("speed", 1.0)
        idx = count()
        inputs += ["-i", path]
        chain = (f"[{idx}:v]trim=start={a:.3f}:end={b:.3f},setpts=(PTS-STARTPTS)/{speed},fps={FPS},"
                 f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,format=yuv420p,{look}")
        t = ed.get("title")
        if t and t.get("on") == c["shot"]:
            fa, fb = t.get("from", 0.6), t.get("to", 2.6)
            chain += (f",drawbox=x=72:y=ih-216:w=10:h=118:color=0xff2f2a@1:t=fill:enable='between(t,{fa},{fb})'"
                      f",drawtext=fontfile={_q(bold)}:textfile={textfile('title.txt', t['text'])}:fontsize=88:fontcolor=white:x=100:y=h-230:"
                      f"shadowcolor=black@0.55:shadowx=0:shadowy=3:alpha={_fade_alpha(fa, fb)}")
            if t.get("sub"):
                chain += (f",drawtext=fontfile={_q(medium)}:textfile={textfile('sub.txt', t['sub'])}:fontsize=36:fontcolor=white:x=102:y=h-128:"
                          f"shadowcolor=black@0.55:shadowx=0:shadowy=2:alpha={_fade_alpha(fa + 0.15, fb)}")
        segs.append({"chain": chain, "seconds": (b - a) / speed, "into": c.get("into", "cut"), "dur": c.get("dur", 0.5)})
    if not segs:
        raise PipelineError(f"{film['id']}: no clips yet (make the videos first)")
    end = ed.get("end")
    if end:
        idx = count()
        inputs += ["-f", "lavfi", "-i", f"color=c=0x0b0d10:s={W}x{H}:r={FPS}:d={end.get('seconds', 3)}"]
        es = end.get("seconds", 3)
        chain = (f"[{idx}:v]format=yuv420p,settb=AVTB,"
                 f"drawtext=fontfile={_q(bold)}:textfile={textfile('end.txt', end['text'])}:fontsize=64:fontcolor=white:x=(w-text_w)/2:y=h/2-70:alpha={_fade_alpha(0.3, es + 1, 0.5)}")
        if end.get("sub"):
            chain += f",drawtext=fontfile={_q(medium)}:textfile={textfile('endsub.txt', end['sub'])}:fontsize=40:fontcolor=0xffb21e:x=(w-text_w)/2:y=h/2+12:alpha={_fade_alpha(0.6, es + 1, 0.5)}"
        segs.append({"chain": chain, "seconds": es, "into": "black", "dur": 0.8})
    # Every shot is a whole number of frames, and every transition too, so the timing adds up exactly
    # (a run of one-frame crossfades for cuts drifted, and ffmpeg quietly stopped the film part-way).
    for seg in segs:
        seg["frames"] = max(2, round(seg["seconds"] * FPS))
    # first shot fades up from black; the rest come in as the edit says
    if segs[0]["into"] in ("black", "dissolve"):
        segs[0]["chain"] += f",fade=t=in:st=0:d={segs[0]['dur']}"
    parts = [f"{seg['chain']},trim=end_frame={seg['frames']},setpts=PTS-STARTPTS,settb=AVTB[s{i}]" for i, seg in enumerate(segs)]
    total, last = segs[0]["frames"], "s0"
    for i, seg in enumerate(segs[1:], 1):
        if seg["into"] == "cut":
            # a cut: the next shot simply follows
            parts.append(f"[{last}][s{i}]concat=n=2:v=1:a=0[x{i}]")
            total += seg["frames"]
        else:
            d = max(1, min(round(seg["dur"] * FPS), seg["frames"] // 2, total // 2))
            parts.append(f"[{last}][s{i}]xfade=transition={XFADE.get(seg['into'], 'fade')}:duration={d / FPS:.6f}:offset={(total - d) / FPS:.6f}[x{i}]")
            total += seg["frames"] - d
        last = f"x{i}"
    total = total / FPS
    parts.append(f"[{last}]fade=t=out:st={max(0, total - 0.8):.3f}:d=0.8,format=yuv420p[v]")
    if music:
        inputs += ["-i", music]
        n = sum(1 for x in inputs if x == "-i") - 1
        parts.append(f"[{n}:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,afade=t=in:d=1.5,afade=t=out:st={max(0, total - 2):.3f}:d=2[a]")
    else:
        inputs += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        n = sum(1 for x in inputs if x == "-i") - 1
        parts.append(f"[{n}:a]anull[a]")
    dest = dest or os.path.join(s["out"], "final", f"{film['id']}.mp4")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", ";".join(parts),
           "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
           "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", dest]
    print(f"  {film['title']}: {len(segs) - (1 if end else 0)} shots, about {total:.0f} s, " + (f"music: {os.path.basename(music)}" if music else "silent") + (f" (no clip yet: {', '.join(skipped)})" if skipped else ""), flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise PipelineError(f"{film['id']}: ffmpeg said: {r.stderr.strip()[-600:]}")
    return dest, total


def _reel(s, films, dest=None):
    """The films one after another, through black."""
    import subprocess
    dest = dest or os.path.join(s["out"], "final", "reel.mp4")
    inputs, parts, total, last_v, last_a = [], [], 0.0, None, None
    for i, (path, secs) in enumerate(films):
        inputs += ["-i", path]
        parts.append(f"[{i}:v]settb=AVTB,format=yuv420p[v{i}];[{i}:a]aresample=48000[a{i}]")
        if i == 0:
            last_v, last_a, total = "v0", "a0", secs
            continue
        d = 0.6
        parts.append(f"[{last_v}][v{i}]xfade=transition=fadeblack:duration={d}:offset={total - d:.3f}[xv{i}]")
        parts.append(f"[{last_a}][a{i}]acrossfade=d={d}[xa{i}]")
        last_v, last_a, total = f"xv{i}", f"xa{i}", total - d + secs
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", ";".join(parts),
           "-map", f"[{last_v}]", "-map", f"[{last_a}]", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", dest]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise PipelineError(f"the reel: ffmpeg said: {r.stderr.strip()[-600:]}")
    print(f"  the reel: {len(films)} films, about {total:.0f} s")
    return dest


def _cut(s, only=None, music=None, reel=True):
    doc = load_json(s["shots"])
    done = []
    for film in doc["films"]:
        if only and film["id"] != only:
            continue
        done.append(_render_film(s, film, music=(music or {}).get(film["id"]) if isinstance(music, dict) else music))
    out = [p for p, _ in done]
    if reel and len(done) > 1:
        out.append(_reel(s, done))
    print("Finished films:\n  " + "\n  ".join(out))
    return out


def collect(dest=None, **settings):
    """Every finished clip in one folder, with a list (from a notebook, or: python comfy_pipeline.py collect)."""
    return _collect(load_settings(**settings), dest)


def cut(only=None, music=None, reel=True, **settings):
    """The finished films, edited as each film's "edit" in shots.json says: renders/final/<film>.mp4 and the reel.
    music: a file for every film, or {"wedding": "song.mp3", ...}; without it the films are silent."""
    return _cut(load_settings(**settings), only, music, reel)


def main():
    ap = argparse.ArgumentParser(description="VUEMesh films through ComfyUI: Qwen-Image stills, then LTX image to video.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("stills", "videos", "all"):
        p = sub.add_parser(name)
        p.add_argument("--only", help="a film (wedding) or one shot (wedding/05-dance-wide)")
        p.add_argument("--force", action="store_true", help="make it again even if it's already there")
        p.add_argument("--variants", type=int, help="stills per shot (default from shots.json)")
        p.add_argument("--fixed-seeds", action="store_true", help="same seeds every run (to compare settings)")
    sub.add_parser("review")
    p = sub.add_parser("music", help="ACE-Step music for each film (instrumental beds)")
    p.add_argument("--only", help="one film (wedding, concert, arena)")
    p.add_argument("--force", action="store_true")
    p.add_argument("--variants", type=int, help="takes per film (default from shots.json: 3)")
    sub.add_parser("collect", help="copy every clip into one folder, with a list")
    p = sub.add_parser("cut", help="render the finished films (and the reel) from each film's edit")
    p.add_argument("--only", help="one film (wedding, concert, arena)")
    p.add_argument("--music", help="a music file to lay under the films")
    p.add_argument("--no-reel", action="store_true")
    p = sub.add_parser("inspect")
    p.add_argument("workflow")
    args = ap.parse_args()
    try:
        run(args)
    except PipelineError as e:
        print(f"\n{e}", file=sys.stderr)
        sys.exit(1)


def run(args):
    s = load_settings()
    if args.cmd == "inspect":
        return cmd_inspect(s, args)
    if args.cmd == "music":
        return stage_music(s, args)
    if args.cmd == "collect":
        return _collect(s)
    if args.cmd == "cut":
        return _cut(s, args.only, args.music, not args.no_reel)
    if args.cmd == "review":
        page = write_review(s, load_json(s["shots"]))
        print(page)
        return webbrowser.open("file://" + os.path.abspath(page))
    if args.cmd in ("stills", "all"):
        stage_stills(s, args)
    if args.cmd in ("videos", "all"):
        stage_videos(s, args)


if __name__ == "__main__":
    main()
