# Repository graph

This graph covers the project's application source and runtime dependencies. The checked-in `venv/` contains third-party environment files and is not part of the application graph.

## Runtime flow

```mermaid
flowchart TD
    A["main.py: initialize pyttsx3 engine"] --> B["speak startup prompts"]
    B --> C["listing(): identify speaker"]
    C --> D["wishMe(): choose greeting from current hour"]
    D --> E["take_command(): listen for request"]
    E --> F{"Recognized phrase"}
    F -->|tell me a joke| G["speak joke"]
    F -->|tell about yourself| H["speak assistant introduction"]
    F -->|even or odd| I["extract number and speak parity"]
    F -->|spell| J["spell requested word"]
    F -->|open browser| K["register Chrome and open websites"]
    F -->|open YouTube / WhatsApp / Instagram / mail| L["use pyautogui to enter website"]
```

## Code and dependency relationships

```mermaid
flowchart LR
    M["main.py"] --> SR["SpeechRecognition"]
    M --> TTS["pyttsx3"]
    M --> DT["datetime"]
    M --> GUI["pyautogui"]
    M --> WB["webbrowser"]
    M --> S["speak(audio)"]
    M --> LI["listing()"]
    M --> WM["wishMe()"]
    M --> TC["take_command()"]
    LI --> SR
    LI --> S
    WM --> DT
    WM --> S
    TC --> SR
    TC --> S
    TC --> GUI
    TC --> WB
```

`main.py` invokes the startup sequence at module scope: it initializes speech synthesis, greets the user, runs speaker identification, announces a time-based greeting, and then listens for one command.
