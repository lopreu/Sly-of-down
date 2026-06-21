import os
import uuid
import glob
import shutil
import traceback

import yt_dlp
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Sly of Down — Universal")

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

tasks = {}

TEMP_EXTS = (".part", ".ytdl", ".temp", ".f", ".frag")
SUB_EXTS = (".vtt", ".srt", ".ass", ".ssa", ".lrc", ".json3", ".srv1", ".srv2", ".srv3", ".ttml")


# ----------------------------------------------------------------------------
# AMBIENTE — ffmpeg e cookies (resolve as armadilhas de hospedagem no Render)
# ----------------------------------------------------------------------------
def _resolve_ffmpeg():
    """Acha o ffmpeg: 1) no PATH do sistema; 2) binário estático do imageio-ffmpeg.
    No Render (Python service) normalmente não há ffmpeg no PATH — o imageio-ffmpeg
    fornece um binário pronto, então merge e legenda funcionam mesmo na nuvem."""
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return os.path.dirname(sys_ffmpeg)  # a pasta contém ffmpeg + ffprobe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()  # caminho completo do binário
    except Exception:
        return None


FFMPEG_LOCATION = _resolve_ffmpeg()


def _apply_cookies(opts, allow_browser):
    """Cookies em ordem de preferência:
       1) arquivo cookies.txt (funciona ATÉ no Render se enviado como Secret File);
       2) cookies do navegador Edge (somente quando rodando localmente)."""
    candidates = [
        os.environ.get("YTDLP_COOKIES"),
        os.path.join(os.getcwd(), "cookies.txt"),
        "/etc/secrets/cookies.txt",  # caminho dos Secret Files no Render
    ]
    for path in candidates:
        if path and os.path.exists(path):
            opts["cookiefile"] = path
            return
    if allow_browser and "RENDER" not in os.environ:
        opts["cookiesfrombrowser"] = ("edge", None, None, None)


# ----------------------------------------------------------------------------
# OPÇÕES DO yt-dlp (todas verificadas contra o yt-dlp 2026.06.09)
# ----------------------------------------------------------------------------
def _build_opts(task_id, merge_format, want_subs, allow_browser, quality, progress_hook):
    # Preferência de qualidade SEM filtro rígido (nunca rejeita stream do Stremio):
    #   - "compat": prioriza H.264/AAC/MP4 -> toca nativo em iPhone/iOS (até 4K se houver
    #     H.264; senão pega o melhor H.264 disponível, normalmente 1080p).
    #   - "max": prioriza resolução (até ~4K) independentemente do codec (pode ser VP9/AV1,
    #     que iPhones antigos não tocam nativamente).
    if quality == "max":
        fmt_sort = ["res:2160", "vbr", "vcodec", "acodec", "ext:mp4:m4a"]
    else:  # compat (padrão) — blindado para iOS
        fmt_sort = ["vcodec:h264", "acodec:aac", "ext:mp4:m4a", "res:2160"]

    opts = {
        # Sem filtro rígido de codec/altura: o format_sort apenas ORDENA a preferência,
        # então funciona em YouTube, Instagram, TikTok, X, Vimeo E streams do Stremio.
        "format": "bestvideo*+bestaudio/best",
        "format_sort": fmt_sort,
        "merge_output_format": merge_format,
        "outtmpl": os.path.join(DOWNLOAD_DIR, f"{task_id}.%(ext)s"),
        "progress_hooks": [progress_hook],

        # SEMPRE UM ÚNICO VÍDEO (ignora playlist/mix/carrossel em qualquer plataforma)
        "noplaylist": True,
        "playlist_items": "1",

        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "nocolor": True,

        "ignoreerrors": False,       # erros reais aparecem -> o ladder reage
        "continuedl": True,
        "nocheckcertificate": True,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "concurrent_fragment_downloads": 4,

        # Desafios JS do YouTube (assinatura/nsig). js_runtimes DEVE ser dict.
        "js_runtimes": {"deno": {"path": None}, "node": {"path": None}},
        "remote_components": ["ejs:github", "ejs:npm"],

        "postprocessors": [],
    }

    if FFMPEG_LOCATION:
        opts["ffmpeg_location"] = FFMPEG_LOCATION

    if want_subs:
        # Legenda EXCLUSIVAMENTE em inglês (oficial + automática); ausência não é fatal.
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = ["en.*", "en"]
        opts["subtitlesformat"] = "srt/best"
        opts["postprocessors"].append(
            {"key": "FFmpegEmbedSubtitle", "already_have_subtitle": True}
        )

    _apply_cookies(opts, allow_browser)
    return opts


# ----------------------------------------------------------------------------
# UTILITÁRIOS DE ARQUIVO
# ----------------------------------------------------------------------------
def _cleanup_partials(task_id):
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{task_id}*")):
        if f.endswith(TEMP_EXTS) or ".part-" in f or f.endswith(SUB_EXTS):
            try:
                os.remove(f)
            except OSError:
                pass


def _sweep_subs(task_id):
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{task_id}*")):
        if f.endswith(SUB_EXTS):
            try:
                os.remove(f)
            except OSError:
                pass


def _find_final_file(task_id):
    best = []
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{task_id}*")):
        if f.endswith(TEMP_EXTS) or f.endswith(SUB_EXTS) or ".part-" in f:
            continue
        try:
            best.append((os.path.getsize(f), f))
        except OSError:
            continue
    if not best:
        return None
    best.sort(reverse=True)
    return best[0][1]


# ----------------------------------------------------------------------------
# DOWNLOAD COM LADDER DE RESILIÊNCIA
# ----------------------------------------------------------------------------
def download_video_task(task_id, url, quality):
    def progress_hook(d):
        st = tasks.get(task_id)
        if not st:
            return
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes", 0)
            if total:
                st["progress"] = round(min(done / total * 100, 99.0), 1)
        elif d.get("status") == "finished":
            st["progress"] = 99.0

    local = "RENDER" not in os.environ
    plan, seen = [], set()

    def add(merge_format, want_subs, allow_browser):
        key = (merge_format, want_subs, allow_browser)
        if key not in seen:
            seen.add(key)
            plan.append(key)

    add("mp4", True, local)    # ideal: MP4 (iOS/4K conforme escolha) + legenda EN
    add("mkv", True, local)    # se o remux p/ MP4 falhar (HLS/codec), MKV aceita tudo
    add("mp4", True, False)    # se o cookie do navegador travar, tenta sem ele
    add("mp4", False, False)   # último recurso: ao menos o vídeo

    last_error = None
    for merge_format, want_subs, allow_browser in plan:
        _cleanup_partials(task_id)
        try:
            opts = _build_opts(task_id, merge_format, want_subs, allow_browser, quality, progress_hook)
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            _sweep_subs(task_id)
            final = _find_final_file(task_id)
            if final and os.path.exists(final) and os.path.getsize(final) > 0:
                tasks[task_id]["status"] = "done"
                tasks[task_id]["progress"] = 100.0
                tasks[task_id]["file"] = final
                return
            last_error = "O arquivo final não foi gerado nesta tentativa."
        except Exception as e:
            last_error = str(e)
            continue

    traceback.print_exc()
    tasks[task_id]["status"] = "error"
    tasks[task_id]["file"] = last_error or "Falha desconhecida no download."


# ----------------------------------------------------------------------------
# ENDPOINTS
# ----------------------------------------------------------------------------
class DownloadRequest(BaseModel):
    # str (não HttpUrl): evita normalização que quebra links diretos de stream.
    url: str
    quality: str = "compat"  # "compat" (iOS) | "max" (até 4K)


@app.post("/download")
def download(req: DownloadRequest, background_tasks: BackgroundTasks):
    url = (req.url or "").strip()
    if url.lower().startswith("magnet:"):
        return JSONResponse(status_code=400, content={
            "error": "Links magnet/torrent não são suportados. Use o stream direto http/https."})
    if not url.lower().startswith(("http://", "https://")):
        return JSONResponse(status_code=400, content={"error": "URL inválida."})

    quality = req.quality if req.quality in ("compat", "max") else "compat"
    task_id = str(uuid.uuid4())
    tasks[task_id] = {"status": "processing", "progress": 0.0, "file": None}
    background_tasks.add_task(download_video_task, task_id, url, quality)
    return {"task_id": task_id, "message": "Download started"}


@app.get("/status/{task_id}")
def status(task_id: str):
    return tasks.get(task_id, {"error": "not found"})


@app.get("/download-file/{task_id}")
def download_file(task_id: str):
    task = tasks.get(task_id)
    if task and task.get("status") == "done" and task.get("file"):
        path = task["file"]
        if os.path.exists(path):
            ext = os.path.splitext(path)[1] or ".mp4"
            return FileResponse(path, media_type="application/octet-stream",
                                filename=f"SlyOfDown_{task_id}{ext}")
    return JSONResponse(status_code=404, content={"error": "Arquivo não disponível."})


@app.get("/health")
def health():
    return {"status": "ok", "ffmpeg": bool(FFMPEG_LOCATION)}


# ----------------------------------------------------------------------------
# FRONTEND — "Dreamscape" surreal (aurora + portal + blobs + starfield)
# ----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Sly of Down — Universal</title>
<style>
  :root{
    --txt:#f4f1ff; --muted:#b9b2e0;
    --violet:#8b5cf6; --violet2:#b794ff; --pink:#f472b6; --pink2:#ec4899;
    --cyan:#5ad1ff; --gold:#ffd479;
    --glass:rgba(28,22,58,.45); --stroke:rgba(255,255,255,.14);
    --ok:#4ade80; --err:#fb7185;
  }
  *{box-sizing:border-box;margin:0;padding:0;font-family:'Segoe UI',system-ui,-apple-system,sans-serif}
  html,body{height:100%}
  body{
    min-height:100dvh;display:flex;align-items:center;justify-content:center;padding:22px;
    color:var(--txt);position:relative;overflow:hidden;background:#0a0716;
  }

  /* AURORA viva — gradiente cônico que gira muito devagar */
  .aurora{position:fixed;inset:-30%;z-index:0;filter:blur(60px) saturate(135%);opacity:.85;
    background:
      conic-gradient(from 0deg at 30% 30%, #2b1066, #6d28d9, #db2777, #0ea5e9, #6d28d9, #2b1066);
    animation:spin 38s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}

  /* Blobs surreais que se deformam (morphing) */
  .blob{position:fixed;z-index:1;border-radius:42% 58% 63% 37%/41% 44% 56% 59%;
    filter:blur(42px);opacity:.55;mix-blend-mode:screen;animation:morph 14s ease-in-out infinite}
  .b1{width:340px;height:340px;background:#7c3aed;top:-60px;left:-40px}
  .b2{width:300px;height:300px;background:#ec4899;bottom:-70px;right:-50px;animation-delay:-4s}
  .b3{width:240px;height:240px;background:#22d3ee;top:40%;right:-30px;animation-delay:-8s}
  @keyframes morph{
    0%,100%{border-radius:42% 58% 63% 37%/41% 44% 56% 59%;transform:translateY(0) rotate(0)}
    33%{border-radius:67% 33% 41% 59%/57% 62% 38% 43%;transform:translateY(-22px) rotate(40deg)}
    66%{border-radius:38% 62% 54% 46%/49% 36% 64% 51%;transform:translateY(14px) rotate(-30deg)}
  }

  /* Starfield sutil */
  .stars{position:fixed;inset:0;z-index:1;pointer-events:none;opacity:.6;
    background-image:
      radial-gradient(1.4px 1.4px at 12% 22%, #fff, transparent),
      radial-gradient(1.2px 1.2px at 70% 14%, #fff, transparent),
      radial-gradient(1.6px 1.6px at 42% 70%, #fff, transparent),
      radial-gradient(1.1px 1.1px at 86% 56%, #fff, transparent),
      radial-gradient(1.3px 1.3px at 28% 88%, #fff, transparent),
      radial-gradient(1.2px 1.2px at 92% 84%, #fff, transparent);
    animation:twinkle 5s ease-in-out infinite alternate}
  @keyframes twinkle{from{opacity:.35}to{opacity:.75}}

  /* Textura de grão para dar aspecto onírico/analógico */
  .grain{position:fixed;inset:0;z-index:2;pointer-events:none;opacity:.06;mix-blend-mode:overlay;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}

  /* CARTÃO de vidro */
  .card{position:relative;z-index:5;width:100%;max-width:470px;text-align:center;
    background:var(--glass);backdrop-filter:blur(26px) saturate(130%);-webkit-backdrop-filter:blur(26px) saturate(130%);
    border:1px solid var(--stroke);border-radius:30px;padding:38px 30px 28px;
    box-shadow:0 40px 80px -28px rgba(0,0,0,.7), inset 0 1px 0 rgba(255,255,255,.16)}

  /* PORTAL — ícone com anel de luz girando */
  .portal{position:relative;width:108px;height:108px;margin:0 auto 22px;display:flex;align-items:center;justify-content:center}
  .ring{position:absolute;inset:0;border-radius:50%;
    background:conic-gradient(from 0deg,#b794ff,#ec4899,#5ad1ff,#ffd479,#b794ff);
    filter:blur(2px);animation:spin 6s linear infinite;
    -webkit-mask:radial-gradient(farthest-side,transparent calc(100% - 7px),#000 0);
            mask:radial-gradient(farthest-side,transparent calc(100% - 7px),#000 0)}
  .core{width:78px;height:78px;border-radius:24px;display:flex;align-items:center;justify-content:center;
    background:linear-gradient(135deg,#a78bfa,#7c3aed);position:relative;overflow:hidden;
    box-shadow:0 12px 26px -6px rgba(124,58,237,.7), inset 0 3px 0 rgba(255,255,255,.4), inset 0 -6px 12px rgba(0,0,0,.35)}
  .core::after{content:'';position:absolute;top:0;left:0;right:0;height:48%;background:linear-gradient(rgba(255,255,255,.3),transparent)}
  .core svg{width:38px;height:38px;z-index:2;filter:drop-shadow(0 2px 4px rgba(0,0,0,.3));animation:bob 2s infinite ease-in-out}
  @keyframes bob{0%,100%{transform:translateY(0)}50%{transform:translateY(4px)}}

  h1{font-size:29px;font-weight:900;letter-spacing:.5px;text-transform:uppercase;margin-bottom:6px;
    background:linear-gradient(110deg,#b794ff,#ec4899,#5ad1ff);-webkit-background-clip:text;background-clip:text;
    -webkit-text-fill-color:transparent;background-size:200% auto;animation:hue 6s linear infinite}
  @keyframes hue{to{background-position:200% center}}
  .sub{font-size:13.5px;color:var(--muted);margin-bottom:16px;line-height:1.5}

  .chips{display:flex;gap:7px;justify-content:center;flex-wrap:wrap;margin-bottom:20px}
  .chip{font-size:11px;font-weight:700;color:#e7e1ff;padding:5px 11px;border-radius:999px;
    border:1px solid var(--stroke);background:rgba(255,255,255,.05)}
  .chip b{color:var(--violet2)}

  /* Toggle de qualidade */
  .seg{display:flex;gap:6px;background:rgba(10,7,22,.55);border:1px solid var(--stroke);
    border-radius:13px;padding:5px;margin-bottom:14px}
  .seg button{flex:1;border:none;cursor:pointer;color:var(--muted);background:transparent;
    font-size:12.5px;font-weight:700;padding:10px 8px;border-radius:9px;transition:.25s;line-height:1.2}
  .seg button small{display:block;font-weight:500;font-size:10px;opacity:.7;margin-top:2px}
  .seg button.on{color:#fff;background:linear-gradient(135deg,var(--violet),var(--pink2));
    box-shadow:0 6px 16px -6px rgba(124,58,237,.7)}

  .inputwrap{position:relative;margin-bottom:12px}
  input{width:100%;background:rgba(10,7,22,.55);border:2px solid var(--stroke);color:var(--txt);
    padding:16px 92px 16px 16px;border-radius:14px;font-size:15px;outline:none;transition:.25s}
  input::placeholder{color:#7d76a6}
  input:focus{border-color:var(--violet2);box-shadow:0 0 0 4px rgba(139,92,246,.22);background:rgba(10,7,22,.8)}
  .paste{position:absolute;right:8px;top:50%;transform:translateY(-50%);
    background:rgba(139,92,246,.22);color:var(--violet2);border:1px solid rgba(139,92,246,.4);
    border-radius:9px;font-size:12px;font-weight:700;padding:8px 12px;cursor:pointer;transition:.2s}
  .paste:hover{background:rgba(139,92,246,.36)}

  button.go{width:100%;border:none;cursor:pointer;color:#fff;font-size:16px;font-weight:800;letter-spacing:.3px;
    padding:16px;border-radius:14px;transition:.25s;position:relative;overflow:hidden;
    background:linear-gradient(135deg,var(--violet),var(--pink2));
    box-shadow:0 12px 28px -10px rgba(236,72,153,.7)}
  button.go:hover{transform:translateY(-2px);box-shadow:0 16px 34px -10px rgba(236,72,153,.8)}
  button.go:active{transform:translateY(0)}

  .progress{display:none;margin-top:6px;text-align:left}
  .plabel{display:flex;justify-content:space-between;font-size:13px;font-weight:700;color:#d8d2ff;margin-bottom:9px}
  .pbg{height:13px;border-radius:8px;overflow:hidden;border:1px solid var(--stroke);background:rgba(255,255,255,.06)}
  .pfill{height:100%;width:0;border-radius:8px;background:linear-gradient(90deg,var(--violet),var(--pink2),var(--cyan));
    background-size:220% 100%;animation:flow 1.6s linear infinite;transition:width .35s ease}
  @keyframes flow{to{background-position:220% 0}}

  .msg{display:none;margin-top:22px;font-size:13.5px;font-weight:600;padding:13px 14px;border-radius:12px;line-height:1.45}
  .msg.ok{background:rgba(74,222,128,.13);color:var(--ok);border:1px solid rgba(74,222,128,.3)}
  .msg.err{background:rgba(251,113,133,.13);color:var(--err);border:1px solid rgba(251,113,133,.3);text-align:left}

  .foot{margin-top:20px;font-size:11px;color:#8a83b3;line-height:1.6}

  /* Mascote chibi correndo */
  .mascot{position:relative;width:120px;height:116px;margin:0 auto 18px;display:none}
  .runner{position:absolute;width:60px;height:90px;left:30px;top:14px;animation:run .5s infinite alternate ease-in-out}
  @keyframes run{0%{transform:translateY(0) rotate(5deg)}100%{transform:translateY(-6px) rotate(10deg)}}
  .head{position:absolute;width:45px;height:45px;background:#ffedd5;border-radius:50%;top:0;left:8px;z-index:3;border:2px solid #1e1b4b}
  .hair{position:absolute;width:50px;height:25px;background:var(--violet);border-radius:25px 25px 0 0;top:-3px;left:-4px}
  .eye{position:absolute;width:6px;height:6px;background:#1e1b4b;border-radius:50%;top:22px}
  .eye.l{left:12px}.eye.r{left:28px}
  .blush{position:absolute;width:35px;height:6px;background:rgba(236,72,153,.4);top:26px;left:5px;border-radius:3px}
  .torso{position:absolute;width:32px;height:35px;background:var(--cyan);top:42px;left:14px;border-radius:8px;z-index:2;border:2px solid #1e1b4b}
  .arm,.leg{position:absolute;background:#ffedd5;border:2px solid #1e1b4b;border-radius:6px}
  .arm{width:10px;height:24px;top:45px;transform-origin:top center}
  .arm.l{left:8px;z-index:1;animation:al .5s infinite alternate linear}
  .arm.r{left:38px;z-index:4;animation:ar .5s infinite alternate linear}
  .leg{width:12px;height:28px;top:72px;transform-origin:top center}
  .leg.l{left:16px;z-index:1;animation:ll .5s infinite alternate linear}
  .leg.r{left:28px;z-index:2;animation:lr .5s infinite alternate linear}
  @keyframes al{0%{transform:rotate(50deg)}100%{transform:rotate(-40deg)}}
  @keyframes ar{0%{transform:rotate(-40deg)}100%{transform:rotate(50deg)}}
  @keyframes ll{0%{transform:rotate(-45deg)}100%{transform:rotate(35deg)}}
  @keyframes lr{0%{transform:rotate(35deg)}100%{transform:rotate(-45deg)}}
  .wind{position:absolute;height:4px;background:#c084fc;border-radius:2px;animation:w .3s infinite linear}
  .w1{width:30px;left:-10px;top:40px}.w2{width:20px;left:-20px;top:60px;animation-delay:.1s}.w3{width:25px;left:-5px;top:80px;animation-delay:.2s}
  @keyframes w{0%{transform:translateX(0);opacity:1}100%{transform:translateX(-40px);opacity:0}}

  @media (prefers-reduced-motion: reduce){
    .aurora,.blob,.ring,h1,.pfill,.stars{animation:none}
  }
</style>
</head>
<body>
  <div class="aurora"></div>
  <div class="blob b1"></div><div class="blob b2"></div><div class="blob b3"></div>
  <div class="stars"></div>
  <div class="grain"></div>

  <div class="card">
    <div class="portal" id="portal">
      <div class="ring"></div>
      <div class="core">
        <svg viewBox="0 0 24 24" fill="none"><path d="M12 15L7 10H10V4H14V10H17L12 15Z" fill="#fff"/><path d="M19 17H5V19H19V17Z" fill="#fff"/></svg>
      </div>
    </div>

    <div class="mascot" id="mascot">
      <div class="runner">
        <div class="head"><div class="hair"></div><div class="eye l"></div><div class="eye r"></div><div class="blush"></div></div>
        <div class="torso"></div><div class="arm l"></div><div class="arm r"></div><div class="leg l"></div><div class="leg r"></div>
      </div>
      <div class="wind w1"></div><div class="wind w2"></div><div class="wind w3"></div>
    </div>

    <h1>Sly of Down</h1>
    <p class="sub">Baixe de qualquer plataforma, em um arquivo só, com legenda em inglês embutida.</p>

    <div class="chips">
      <span class="chip">Legenda <b>EN</b></span>
      <span class="chip"><b>1</b> vídeo por vez</span>
      <span class="chip">YouTube · Stremio · IG · TikTok · X · Vimeo</span>
    </div>

    <div id="form">
      <div class="seg" id="seg">
        <button class="on" data-q="compat" onclick="pickQ(this)">Compatível<small>iPhone · MP4 H.264</small></button>
        <button data-q="max" onclick="pickQ(this)">Máxima<small>até 4K</small></button>
      </div>
      <div class="inputwrap">
        <input id="url" type="text" placeholder="Cole o link (YouTube, Stremio, Instagram, TikTok, X, Vimeo…)">
        <button class="paste" onclick="pasteUrl()">Colar</button>
      </div>
      <button class="go" onclick="startDownload()">Baixar agora</button>
    </div>

    <div id="progress" class="progress">
      <div class="plabel"><span id="ptxt">Preparando…</span><span id="pct">0%</span></div>
      <div class="pbg"><div id="pfill" class="pfill"></div></div>
    </div>

    <div id="msg" class="msg"></div>

    <div class="foot">
      Suporta sites compatíveis com yt-dlp + streams http/https do Stremio. Magnet/torrent não é suportado.
    </div>
  </div>

<script>
  const $ = id => document.getElementById(id);
  let quality = 'compat';

  function pickQ(btn){
    quality = btn.dataset.q;
    document.querySelectorAll('#seg button').forEach(b=>b.classList.remove('on'));
    btn.classList.add('on');
  }
  async function pasteUrl(){
    try{ $('url').value = (await navigator.clipboard.readText()).trim(); }catch(e){ $('url').focus(); }
  }
  function setView(s){
    $('form').style.display   = (s==='form')    ? 'block':'none';
    $('progress').style.display = (s==='loading') ? 'block':'none';
    $('portal').style.display = (s==='loading') ? 'none':'flex';
    $('mascot').style.display = (s==='loading') ? 'block':'none';
  }

  async function startDownload(){
    const url = $('url').value.trim();
    if(!url){ alert('Cole um link primeiro.'); return; }
    $('msg').style.display='none'; setView('loading');
    $('pfill').style.width='0%'; $('pct').innerText='0%'; $('ptxt').innerText='Preparando…';
    try{
      const r = await fetch('/download',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({url, quality})});
      const data = await r.json();
      if(!r.ok) throw new Error(data.error || 'Não foi possível iniciar.');
      poll(data.task_id);
    }catch(e){ showError(e.message); }
  }

  function poll(taskId){
    const it = setInterval(async ()=>{
      try{
        const data = await (await fetch('/status/'+taskId)).json();
        if(data.status==='processing'){
          const p = data.progress || 0;
          $('ptxt').innerText = p>=99 ? 'Processando vídeo + legenda…' : 'Baixando…';
          $('pfill').style.width = p+'%'; $('pct').innerText = Math.round(p)+'%';
        } else if(data.status==='done'){
          clearInterval(it);
          $('pfill').style.width='100%'; $('pct').innerText='100%'; $('ptxt').innerText='Concluído!';
          showOk('Pronto! Salvando o arquivo no seu dispositivo…');
          window.location.href='/download-file/'+taskId;
        } else if(data.status==='error'){
          clearInterval(it);
          showError('Falha: ' + (data.file || 'verifique o link.'));
        }
      }catch(e){ clearInterval(it); showError('Erro ao consultar o servidor.'); }
    }, 1000);
  }

  function showError(t){ setView('form'); const m=$('msg'); m.className='msg err'; m.innerText=t; m.style.display='block'; }
  function showOk(t){ const m=$('msg'); m.className='msg ok'; m.innerText=t; m.style.display='block';
    setTimeout(()=>{ setView('form'); $('url').value=''; m.style.display='none'; }, 5000); }

  $('url').addEventListener('keydown', e=>{ if(e.key==='Enter') startDownload(); });
</script>
</body>
</html>
    """
