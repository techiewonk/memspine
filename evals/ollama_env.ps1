<#
.SYNOPSIS
  Set the Ollama server settings the MemSpine LoCoMo runs are specified against, then
  restart Ollama (gap register E3/E4/F4).

.DESCRIPTION
  Sets these as USER environment variables (persisted, so a restarted Ollama picks them up):
    OLLAMA_FLASH_ATTENTION=1      flash attention on
    OLLAMA_KV_CACHE_TYPE=q8_0     8-bit KV cache (halves KV memory vs f16)
    OLLAMA_CONTEXT_LENGTH=8192    matches the harness --server-ctx default
    OLLAMA_NUM_PARALLEL=2         two request slots; KV memory is per slot
    OLLAMA_KEEP_ALIVE=-1          keep models loaded between calls
    OLLAMA_DEBUG=0                debug logging off (a debug-level server.log reached 237 MB)

  Then stops the Ollama tray app / server and starts "ollama serve" again.

  THIS RESTARTS OLLAMA and interrupts any running benchmark that is using it. Do not run it
  while a run is in flight. The harness records every OLLAMA_* variable in manifest.runtime.env
  and GPU memory at run start / end in manifest.runtime.gpu, so a run states what it ran under.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File evals\ollama_env.ps1          # apply and restart
  powershell -ExecutionPolicy Bypass -File evals\ollama_env.ps1 -NoRestart
#>
param([switch]$NoRestart)

$settings = [ordered]@{
    OLLAMA_FLASH_ATTENTION = '1'
    OLLAMA_KV_CACHE_TYPE   = 'q8_0'
    OLLAMA_CONTEXT_LENGTH  = '8192'
    OLLAMA_NUM_PARALLEL    = '2'
    OLLAMA_KEEP_ALIVE      = '-1'
    OLLAMA_DEBUG           = '0'
}

foreach ($name in $settings.Keys) {
    [Environment]::SetEnvironmentVariable($name, $settings[$name], 'User')
    Set-Item -Path "Env:$name" -Value $settings[$name]
    Write-Host "$name=$($settings[$name])"
}

if ($NoRestart) {
    Write-Host 'Variables set; Ollama not restarted (-NoRestart).'
    return
}

Get-Process -Name 'ollama app', 'ollama' -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
Start-Process -FilePath 'ollama' -ArgumentList 'serve' -WindowStyle Hidden
Write-Host 'Ollama restarted with the settings above.'
