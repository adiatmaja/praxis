# Assets

Place the README demo recording here as `demo.gif` (referenced by the root `README.md`).

## Target specs
- **Length:** 15–25 seconds (trim all dead/loading time)
- **Size:** ≤ 10 MB (GitHub renders inline; larger files load slowly or get blocked)
- **Dimensions:** ~1280px wide, then downscale to 800px for the GIF
- **Format:** GIF (autoplays in GitHub READMEs; MP4 does not)

## How to produce it
1. Record the run with **ScreenToGif** (Windows, free) or capture MP4 + convert.
2. If you recorded MP4, convert + optimize with ffmpeg + gifsicle:

   ```bash
   ffmpeg -i demo.mp4 -vf "fps=12,scale=800:-1:flags=lanczos" -loop 0 demo-raw.gif
   gifsicle -O3 --lossy=80 demo-raw.gif -o demo.gif
   ```

3. Confirm `demo.gif` is < 10 MB, then commit it here.
