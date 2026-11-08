# Local screen-reading models for `phoneuse text` / `phoneuse find` on Windows:
# RapidOCR (PP-OCRv6) and GoClick-Large (GUI grounding), in their own venv (.venv-vision),
# because GoClick needs transformers 4.45. With an NVIDIA GPU both run on CUDA 13.
#   powershell -ExecutionPolicy Bypass -File scripts\windows\setup-vision.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. "$PSScriptRoot\common.ps1"

$Py = Find-Python
$Venv = Join-Path $Root ".venv-vision"
if (-not (Test-Path "$Venv\Scripts\python.exe")) {
    Write-Step "Creating $Venv"
    & $Py -m venv $Venv
}
$VPy = "$Venv\Scripts\python.exe"
& $VPy -m pip install --quiet --upgrade pip

$gpu = $null -ne (Get-Command nvidia-smi -ErrorAction SilentlyContinue)
if ($gpu) {
    # One CUDA for both: onnxruntime-gpu is built for CUDA 13 and brings its CUDA and cuDNN
    # DLLs as pip packages ([cuda,cudnn]); torch's CUDA 13.0 wheel matches them (two cuDNN
    # builds of the same DLL name can't share a process).
    Write-Step "Installing torch with CUDA 13 (about 3 GB)"
    & $VPy -m pip install --quiet torch --index-url https://download.pytorch.org/whl/cu130
    $ort = "onnxruntime-gpu[cuda,cudnn]"
} else {
    Write-Step "Installing torch (CPU)"
    & $VPy -m pip install --quiet torch
    $ort = "onnxruntime"
}
if ($LASTEXITCODE -ne 0) { throw "torch failed to install" }
Write-Step "Installing OCR and the grounding model's libraries"
& $VPy -m pip install --quiet "transformers==4.45.0" timm einops huggingface_hub rapidocr $ort pillow numpy psutil
if ($LASTEXITCODE -ne 0) { throw "the vision libraries failed to install" }
Write-Step "Downloading GoClick-Large"
& $VPy -c "from huggingface_hub import snapshot_download; snapshot_download('HongxinLi/GoClick-Large', allow_patterns=['*.json', '*.py', 'model.safetensors'])"
if ($LASTEXITCODE -ne 0) { throw "the model download failed" }
Write-Host "vision models ready" -ForegroundColor Green
