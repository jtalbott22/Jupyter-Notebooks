Notebooks I think are fun...

## [vuemesh-films](vuemesh-films/): how the films on vuemesh.com were made

The three short films on the [VUEMesh](https://vuemesh.com) home page (a wedding first dance, a
concert and an arena game) were made entirely on a home gaming PC with open models: 28 shots
written as prompts, drawn as stills with Qwen-Image, brought to life with LTX image-to-video,
scored with ACE-Step, then cut, titled and graded with ffmpeg. One script and one notebook drive
it all through your own ComfyUI; every prompt is in `shots.json`. Start with
[its README](vuemesh-films/README.md) to make your own. It runs on the stack in
[self-hosted-ai-stack](https://github.com/jtalbott22/self-hosted-ai-stack).

## [bird_yolo_pipeline_streaming.ipynb](bird_yolo_pipeline_streaming.ipynb): a camera that knows its birds

Pulls the species seen near you from eBird, downloads labelled photos from iNaturalist, trains a
YOLO model on them, and runs it on a live camera stream to name the birds at the feeder.
