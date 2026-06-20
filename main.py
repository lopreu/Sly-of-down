import os
import uuid
import yt_dlp
import traceback
import glob  # Biblioteca para achar qualquer extensão de arquivo
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, HttpUrl

app = FastAPI()

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Banco de dados em memória para as tarefas
tasks = {}

class DownloadRequest(BaseModel):
    url: HttpUrl

def download_video_task(task_id: str, url: str):
    def progress_hook(d):
        if d['status'] == 'downloading':
            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate')
            downloaded_bytes = d.get('downloaded_bytes', 0)
            if total_bytes:
                progress = (downloaded_bytes / total_bytes) * 100
                tasks[task_id]["progress"] = round(progress, 2)
        elif d['status'] == 'finished':
            tasks[task_id]["progress"] = 100.0

    output_template = os.path.join(DOWNLOAD_DIR, f"{task_id}.%(ext)s")
    
    # Formato balanceado para Nuvem e PC (Garante compatibilidade de iPhone/Android)
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'merge_output_format': 'mp4',
        'outtmpl': output_template,
        'progress_hooks': [progress_hook],
        'nocolor': True,
        'quiet': True,
        'writethumbnail': False
    }

    # Se estiver rodando no PC localmente, usa os cookies do Edge para burlar proteção
    if "RENDER" not in os.environ:
        ydl_opts['cookiesfrombrowser'] = ('edge', None, None, None)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
            
        # Procura na pasta o arquivo gerado, independentemente de ser .mp4, .mkv, etc.
        arquivos_salvos = glob.glob(os.path.join(DOWNLOAD_DIR, f"{task_id}.*"))
        
        if arquivos_salvos:
            arquivo_real = arquivos_salvos[0]
            tasks[task_id]["status"] = "done"
            tasks[task_id]["progress"] = 100.0
            tasks[task_id]["file"] = arquivo_real
        else:
            raise Exception("Arquivo não encontrado após o download.")

    except Exception as e:
        traceback.print_exc()
        tasks[task_id]["status"] = "error"
        tasks[task_id]["file"] = str(e)

@app.post("/download")
def download(req: DownloadRequest, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())
    tasks[task_id] = {"status": "processing", "progress": 0.0, "file": None}
    background_tasks.add_task(download_video_task, task_id, str(req.url))
    return {"task_id": task_id, "message": "Download started"}

@app.get("/status/{task_id}")
def status(task_id: str):
    return tasks.get(task_id, {"error": "not found"})

@app.get("/download-file/{task_id}")
def download_file(task_id: str):
    task = tasks.get(task_id)
    if task and task["status"] == "done" and task["file"]:
        if os.path.exists(task["file"]):
            # Identifica a extensão original (.mp4, .mkv) para salvar no dispositivo do usuário
            extensao = os.path.splitext(task["file"])[1]
            return FileResponse(task["file"], filename=f"SlyOfDown_Media_{task_id}{extensao}")
    return {"error": "Arquivo não disponível ou ainda em processamento."}

# Frontend Sly of Down (Interface Avançada)
@app.get("/", response_class=HTMLResponse)
def index():
    return """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Sly of Down - Baixador Universal</title>
        <style>
            * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Segoe UI', sans-serif; }
            body { background: radial-gradient(circle, #ffffff 20%, #ebdfff 100%); color: #1e1b4b; display: flex; justify-content: center; align-items: center; min-height: 100vh; position: relative; overflow: hidden; }
            .bg-icon { position: absolute; color: #a78bfa; opacity: 0.12; z-index: 0; pointer-events: none; animation: pulse-slow 5s infinite ease-in-out; }
            @keyframes pulse-slow { 0%, 100% { transform: scale(1) rotate(var(--rot, 0deg)); opacity: 0.12; } 50% { transform: scale(1.1) rotate(calc(var(--rot, 0deg) + 6deg)); opacity: 0.18; } }
            .container { background-color: #ffffff; padding: 40px 30px; border-radius: 24px; box-shadow: 0 25px 50px -12px rgba(124, 58, 237, 0.18); width: 100%; max-width: 480px; text-align: center; border: 1px solid rgba(233, 213, 255, 0.7); position: relative; z-index: 2; }
            .btn-3d { width: 100px; height: 100px; background: linear-gradient(135deg, #a78bfa 0%, #7c3aed 100%); border-radius: 28px; display: inline-flex; align-items: center; justify-content: center; box-shadow: 0 15px 30px -5px rgba(124, 58, 237, 0.4), inset 0 4px 0px rgba(255, 255, 255, 0.4), inset 0 -5px 10px rgba(0, 0, 0, 0.25); border: 1px solid rgba(255, 255, 255, 0.2); margin-bottom: 24px; position: relative; overflow: hidden; }
            .btn-3d::after { content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 50%; background: linear-gradient(to bottom, rgba(255, 255, 255, 0.25) 0%, rgba(255, 255, 255, 0) 100%); border-radius: 28px 28px 0 0; }
            .btn-3d svg { width: 48px; height: 48px; z-index: 2; animation: pulse-down 2s infinite ease-in-out; }
            @keyframes pulse-down { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(4px); } }
            .mascote-container { position: relative; width: 120px; height: 120px; margin: 0 auto 20px auto; }
            .chibi-runner { position: absolute; width: 60px; height: 90px; left: 30px; top: 15px; animation: run-bounce 0.5s infinite alternate ease-in-out; }
            @keyframes run-bounce { 0% { transform: translateY(0) rotate(5deg); } 100% { transform: translateY(-6px) rotate(10deg); } }
            .chibi-head { position: absolute; width: 45px; height: 45px; background-color: #ffedd5; border-radius: 50%; top: 0; left: 8px; z-index: 3; border: 2px solid #1e1b4b; }
            .chibi-hair { position: absolute; width: 50px; height: 25px; background-color: #7c3aed; border-radius: 25px 25px 0 0; top: -3px; left: -4px; }
            .chibi-eye { position: absolute; width: 6px; height: 6px; background-color: #1e1b4b; border-radius: 50%; top: 22px; }
            .chibi-eye.left { left: 12px; } .chibi-eye.right { left: 28px; }
            .chibi-body { position: absolute; width: 32px; height: 35px; background-color: #38bdf8; top: 42px; left: 14px; border-radius: 8px; z-index: 2; border: 2px solid #1e1b4b; }
            .chibi-arm, .chibi-leg { position: absolute; background-color: #ffedd5; border: 2px solid #1e1b4b; border-radius: 6px; }
            .chibi-arm { width: 10px; height: 24px; top: 45px; transform-origin: top center; }
            .left-arm { left: 8px; animation: arm-swing-left 0.5s infinite alternate linear; z-index: 1; }
            .right-arm { left: 38px; animation: arm-swing-right 0.5s infinite alternate linear; z-index: 4; }
            .chibi-leg { width: 12px; height: 28px; top: 72px; transform-origin: top center; }
            .left-leg { left: 16px; animation: leg-swing-left 0.5s infinite alternate linear; z-index: 1; }
            .right-leg { left: 28px; animation: leg-swing-right 0.5s infinite alternate linear; z-index: 2; }
            @keyframes arm-swing-left { 0% { transform: rotate(50deg); } 100% { transform: rotate(-40deg); } }
            @keyframes leg-swing-left { 0% { transform: rotate(-45deg); } 100% { transform: rotate(35deg); } }
            .speed-line { position: absolute; height: 4px; background-color: #c084fc; border-radius: 2px; animation: wind 0.3s infinite linear; }
            .line-1 { width: 30px; left: -10px; top: 40px; } .line-2 { width: 20px; left: -20px; top: 60px; animation-delay: 0.1s; } .line-3 { width: 25px; left: -5px; top: 80px; animation-delay: 0.2s; }
            @keyframes wind { 0% { transform: translateX(0); opacity: 1; } 100% { transform: translateX(-40px); opacity: 0; } }
            h1 { font-size: 28px; font-weight: 900; margin-bottom: 8px; background: linear-gradient(135deg, #7c3aed 0%, #c084fc 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; letter-spacing: -1px; text-transform: uppercase; }
            p { font-size: 14px; color: #6b7280; margin-bottom: 30px; }
            input { background-color: #fdfcff; border: 2px solid #e9d5ff; color: #1e1b4b; padding: 16px; border-radius: 12px; font-size: 15px; outline: none; width: 100%; margin-bottom: 15px; }
            button { background: linear-gradient(135deg, #7c3aed 0%, #a855f7 100%); color: #ffffff; border: none; padding: 16px; border-radius: 12px; font-size: 16px; font-weight: 700; cursor: pointer; width: 100%; box-shadow: 0 4px 12px rgba(124, 58, 237, 0.2); }
            .progress-container { display: none; margin-top: 10px; text-align: left; }
            .progress-bar-bg { background-color: #f3e8ff; height: 14px; border-radius: 7px; overflow: hidden; border: 1px solid #e9d5ff; margin-top: 5px; }
            .progress-bar-fill { background: linear-gradient(90deg, #8b5cf6, #d946ef); width: 0%; height: 100%; transition: width 0.3s; }
            .message { margin-top: 24px; font-size: 14px; display: none; padding: 12px; border-radius: 8px; font-weight: 600; text-align: center; }
            .success { background-color: #f0fdf4; color: #166534; border: 1px solid #bbf7d0; }
            .error { background-color: #fef2f2; color: #991b1b; border: 1px solid #fecaca; }
        </style>
    </head>
    <body>
        <svg class="bg-icon" style="top: 10%; left: 8%; width: 80px; height: 80px; --rot: -15deg;" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M12 15L7 10H10V4H14V10H17L12 15Z" fill="currentColor"/><path d="M19 17H5V19H19V17Z" fill="currentColor"/></svg>
        <svg class="bg-icon" style="top: 15%; right: 10%; width: 100px; height: 100px; --rot: 20deg; animation-delay: 1s;" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M12 15L7 10H10V4H14V10H17L12 15Z" fill="currentColor"/><path d="M19 17H5V19H19V17Z" fill="currentColor"/></svg>
        <svg class="bg-icon" style="bottom: 12%; left: 12%; width: 110px; height: 110px; --rot: 10deg; animation-delay: 2s;" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M12 15L7 10H10V4H14V10H17L12 15Z" fill="currentColor"/><path d="M19 17H5V19H19V17Z" fill="currentColor"/></svg>
        <svg class="bg-icon" style="bottom: 18%; right: 15%; width: 70px; height: 70px; --rot: -25deg; animation-delay: 1.5s;" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M12 15L7 10H10V4H14V10H17L12 15Z" fill="currentColor"/><path d="M19 17H5V19H19V17Z" fill="currentColor"/></svg>

        <div class="container">
            <div class="btn-3d" id="icon-3d">
                <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M12 15L7 10H10V4H14V10H17L12 15Z" fill="#ffffff"/><path d="M19 17H5V19H19V17Z" fill="#ffffff"/></svg>
            </div>
            <div class="mascote-container" id="mascote-loading" style="display: none;">
                <div class="chibi-runner">
                    <div class="chibi-head"><div class="chibi-hair"></div><div class="chibi-eye left"></div><div class="chibi-eye right"></div><div class="chibi-blush"></div></div>
                    <div class="chibi-body"></div><div class="chibi-arm left-arm"></div><div class="chibi-arm right-arm"></div><div class="chibi-leg left-leg"></div><div class="chibi-leg right-leg"></div>
                </div>
                <div class="speed-line line-1"></div><div class="speed-line line-2"></div><div class="speed-line line-3"></div>
            </div>
            <h1>Sly of Down</h1>
            <p>Baixe vídeos em Altíssima Qualidade</p>
            <div id="input-section">
                <input type="text" id="url-input" placeholder="Cole o link do vídeo ou Stremio aqui...">
                <button onclick="startDownload()">Baixar Vídeo</button>
            </div>
            <div id="progress-section" class="progress-container">
                <div style="display: flex; justify-content: space-between; font-size: 14px; font-weight: bold;">
                    <span id="status-text">Baixando...</span>
                    <span id="percentage">0%</span>
                </div>
                <div class="progress-bar-bg"><div id="progress-bar" class="progress-bar-fill"></div></div>
            </div>
            <div id="msg-box" class="message"></div>
        </div>
        <script>
            async function startDownload() {
                const url = document.getElementById('url-input').value.trim();
                if(!url) return;
                document.getElementById('icon-3d').style.display = 'none';
                document.getElementById('mascote-loading').style.display = 'block';
                document.getElementById('input-section').style.display = 'none';
                document.getElementById('progress-section').style.display = 'block';
                document.getElementById('msg-box').style.display = 'none';
                
                try {
                    const res = await fetch('/download', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url}) });
                    if(!res.ok) throw new Error("Link inválido.");
                    const data = await res.json();
                    poll(data.task_id);
                } catch(e) {
                    showError(e.message);
                }
            }
            function poll(id) {
                const interval = setInterval(async () => {
                    const res = await fetch('/status/'+id);
                    const data = await res.json();
                    if(data.status === 'processing') {
                        document.getElementById('progress-bar').style.width = data.progress + '%';
                        document.getElementById('percentage').innerText = data.progress + '%';
                    } else if(data.status === 'done') {
                        clearInterval(interval);
                        document.getElementById('progress-bar').style.width = '100%';
                        document.getElementById('percentage').innerText = '100%';
                        showSuccess("Concluído! Baixando arquivo...");
                        window.location.href = '/download-file/'+id;
                    } else if(data.status === 'error') {
                        clearInterval(interval);
                        showError("Erro no download.");
                    }
                }, 1000);
            }
            function showError(msg) {
                const b = document.getElementById('msg-box'); b.className = 'message error'; b.innerText = msg; b.style.display = 'block';
                resetUI();
            }
            function showSuccess(msg) {
                const b = document.getElementById('msg-box'); b.className = 'message success'; b.innerText = msg; b.style.display = 'block';
                setTimeout(resetUI, 4000);
            }
            function resetUI() {
                document.getElementById('icon-3d').style.display = 'inline-flex';
                document.getElementById('mascote-loading').style.display = 'none';
                document.getElementById('input-section').style.display = 'block';
                document.getElementById('progress-section').style.display = 'none';
                document.getElementById('url-input').value = '';
            }
        </script>
    </body>
    </html>
    """
