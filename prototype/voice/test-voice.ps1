param([string]$OutputDirectory = (Join-Path $env:TEMP 'edith-voice-test'))
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$testDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $testDirectory -Force | Out-Null
$wavePath = Join-Path $testDirectory 'synthetic-study-light.wav'
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
        16000,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono
    )
    $synth.SetOutputToWaveFile($wavePath, $format)
    $synth.Speak('turn on the study light')
} finally { $synth.Dispose() }
$resultText = & powershell.exe -NoProfile -NonInteractive -File (Join-Path $PSScriptRoot 'transcribe.ps1') -WavePath $wavePath
if ($LASTEXITCODE -ne 0) { throw 'Synthetic speech recognition failed.' }
$result = $resultText | ConvertFrom-Json
if ($result.text.ToLowerInvariant() -ne 'turn on the study light') {
    throw ('Unexpected synthetic transcript: ' + $result.text)
}
[pscustomobject]@{
    passed = $true
    fixture = $wavePath
    text = $result.text
    confidence = $result.confidence
    limitation = 'Synthetic audio only. Real microphone accuracy, accents, far-field audio and noisy rooms were not tested.'
} | ConvertTo-Json -Compress
