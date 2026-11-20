param([string]$WavePath)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$recognizer = $null
$audioStream = $null
try {
    Add-Type -AssemblyName System.Speech
    $engines = @([System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers())
    $selected = $engines | Where-Object { $_.Culture.Name -eq 'en-US' } | Select-Object -First 1
    if (-not $selected) { throw 'English speech recognition is not installed.' }
    $recognizer = New-Object System.Speech.Recognition.SpeechRecognitionEngine($selected)
    $dictation = New-Object System.Speech.Recognition.DictationGrammar
    $dictation.Weight = 0.5
    $recognizer.LoadGrammar($dictation)
    $phrases = New-Object System.Speech.Recognition.Choices
    $phrases.Add([string[]]@(
        'turn on the study light', 'turn off the study light',
        'turn on the living room light', 'turn off the living room light',
        'turn on the bedroom fan', 'turn off the bedroom fan',
        'find a household note', 'what day is recycling',
        'when should I water the plants', 'how do I restart the router',
        'start a word game', 'review a shopping list',
        'hello Edith', 'help me with coding'
    ))
    $builder = New-Object System.Speech.Recognition.GrammarBuilder
    $builder.Culture = $selected.Culture
    $builder.Append($phrases)
    $commands = New-Object System.Speech.Recognition.Grammar($builder)
    $commands.Weight = 1.0
    $recognizer.LoadGrammar($commands)
    if ($WavePath) {
        # Optional synthetic test fixture. The application uses stdin, never a disk recording.
        $recognizer.SetInputToWaveFile($WavePath)
    } else {
        $audioStream = New-Object System.IO.MemoryStream
        [Console]::OpenStandardInput().CopyTo($audioStream)
        $audioStream.Position = 0
        $recognizer.SetInputToWaveStream($audioStream)
    }
    $result = $recognizer.Recognize([TimeSpan]::FromSeconds(15))
    if ($null -eq $result) {
        [pscustomobject]@{ text = ''; confidence = 0.0; engine = 'Windows System.Speech (local)' } | ConvertTo-Json -Compress
    } else {
        [pscustomobject]@{
            text = $result.Text
            confidence = [math]::Round([double]$result.Confidence, 4)
            engine = 'Windows System.Speech (local)'
        } | ConvertTo-Json -Compress
    }
} catch {
    [pscustomobject]@{ error = 'Local speech recognition could not read this recording. Please type or try again.' } | ConvertTo-Json -Compress
    exit 1
} finally {
    if ($null -ne $recognizer) { $recognizer.Dispose() }
    if ($null -ne $audioStream) { $audioStream.Dispose() }
}
