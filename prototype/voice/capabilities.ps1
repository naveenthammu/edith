$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    Add-Type -AssemblyName System.Speech
    $recognizers = @([System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers())
    $languages = @($recognizers | ForEach-Object { $_.Culture.Name } | Sort-Object -Unique)
    [pscustomobject]@{
        available = ($recognizers.Count -gt 0)
        engine = 'Windows System.Speech (local)'
        languages = $languages
        detail = 'Local Windows recognition. English commands are the tested baseline; typed input remains available.'
    } | ConvertTo-Json -Compress
} catch {
    [pscustomobject]@{
        available = $false
        engine = 'Windows System.Speech (local)'
        languages = @()
        detail = 'The local Windows speech engine is unavailable. Use typed input.'
    } | ConvertTo-Json -Compress
}
