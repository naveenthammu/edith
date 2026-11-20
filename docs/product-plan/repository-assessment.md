# EDITH repository assessment

> **Historical baseline only.** This document and its inventories describe commit `60fa113`, before the 47-commit update to `a006344`. They do not describe the current application or dependencies. See the [updated code assessment](updated-code-assessment.md) and [household blueprint](household-agent-blueprint.md). The original findings below are retained for traceability.

Reviewed 20 November 2026. Baseline commit: `60fa1132886b343e10a889f86307a1d5a2ac7886` (`main`). This assessment supports the [commercialization plan](commercialization-plan.md); it does not certify the application for release.

## Coverage and evidence

Every one of the **3,056 tracked files** was read byte-for-byte for classification, size, and SHA-256 inventory: 77,693,903 bytes, 1,849 text files, and 1,207 binary files. The text files contain 510,079 lines, overwhelmingly third-party code. There are **11 files outside `venv/`**, including one 102-line application, and **124 installed-distribution metadata records**.

- [File inventory](repository-file-inventory.csv): one row per original tracked file, including binary files and environment launchers.
- [Dependency inventory](dependency-inventory.csv): names, versions, declared licences, classifiers and declared dependencies from installed metadata. This is an inventory of the checked-in environment, not a verified minimal runtime dependency graph.
- [Machine-readable review scope](review-scope.json): commit, counts and limitations.
- All first-party text files and editor configuration were read completely. `.DS_Store` was inventoried as binary metadata. All distribution metadata was parsed; relevant dependency implementation and licence notices were examined.
- Binary contents were hashed, not decompiled. Third-party source was not manually reviewed line by line. This is not a complete vulnerability, malware or supply-chain audit. Old versions are a maintenance concern, not proof of a particular exploitable vulnerability.

No tracked application files were changed. No dependencies were installed, tracked executables launched, microphone recording started, desktop automation performed, or application audio sent to an external service during this review. Local Python 3.13 was used for inventory and isolated mocked behavior checks.

## What the application actually does

`main.py` initializes speech output, speaks a greeting, captures a name, gives a time-based greeting, captures **one command**, performs matching actions, then exits. It uses substring matching rather than an AI reasoning model. It has no continuous assistant loop, graphical interface, account system, database, installation package, billing, updater, telemetry service, integrations that read/write business records, or production tests.

| Existing behavior | Evidence in `main.py` | Commercial implication |
|---|---|---|
| Speech output through `pyttsx3` | Lines 7–11 | An output experiment, not a supported speech subsystem |
| Microphone recognition through `recognize_google` | Lines 18–19 and 44–45 | External speech processing; no provider disclosure or production service contract in the application |
| Personal greeting selected by the word Naveen | Lines 22–25 | Not identity verification or speaker recognition |
| Positive integer even/odd checking | Lines 53–63 | Demonstration utility with little business value |
| Browser and website shortcuts | Lines 72–101 | Fixed destinations and fragile implementation |
| Startup and command functions called at module level | Lines 26, 38 and 102 | Importing the module can start microphone/UI behavior |

The README is two short lines. `test.idec` contains `hi there`, not a test suite. `.idea` files are IDE configuration. `.gitattributes` normalizes text line endings. `venv/pyvenv.cfg` declares Python 3.9.1 and `/usr/local/bin`; native binaries and launchers belong to a macOS environment, not a reproducible Windows installation.

## Release blockers

| Priority | Finding | Evidence and required action |
|---|---|---|
| P0 | Unsafe targeting of desktop actions | `main.py:84–101` clicks `(201, 86)`, types and presses Enter without verifying the active application. Replace this with approved URL launch operations; keep text in an EDITH preview with explicit copy/paste in the pilot. |
| P0 | Offensive canned response | `main.py:48–49` contains explicit misogynistic sexual content. Remove before any customer demonstration. |
| P0 | Speech transport and privacy unsuitable for a product claim | Both recognition calls use defaults. The bundled recognizer's Google path uses an HTTP endpoint and a generic key. Replace the provider path, disclose processing, and test the chosen implementation. Do not advertise this version as private/offline. |
| P0 | Dependency licence and packaging ambiguity | MIT at the repository root does not override bundled packages' terms. Rebuild a minimal environment and resolve the notices detailed below before distribution. |
| P1 | Recognition failures crash the flow | No `UnknownValueError`, `RequestError`, device-error or cancellation handling around either capture. Add explicit bounded recording, retry and typed fallback. |
| P1 | No usable persistent interaction | One command per launch; import side effects; no state machine, status indicator or stop control. |
| P1 | Broken and inconsistent commands | `word` is a list compared with `"spell"` at line 68; command checks are case sensitive; independent `if` branches can execute multiple actions. Replace with normalized, single-intent routing and explicit unsupported-command handling. |
| P1 | Platform-specific browser path | `main.py:77` uses `/Applications/Google Chrome.app`. No actual Windows execution was verified. |
| P1 | No reproducible supported build | No `pyproject.toml`, application requirements/lock file, installer or CI. Do not distribute the committed virtual environment. |
| P2 | Incomplete utilities and content | Negative/spoken numbers do not work; noon/16:00 greeting behavior is inconsistent; YouTube opens a fixed music-video URL. Remove low-value utilities from the business scope. |

Priority is a release planning judgment, not a formal security severity classification.

## Safely verified behavior

The reviewer extracted function definitions using Python's AST and supplied mocks for the microphone, recognizer, speech, browser, clock and desktop automation. This verified control flow without running the application normally.

| Input or condition | Observed result in isolated mock |
|---|---|
| `spell apple` | No spelling output |
| `Open mail` | No matching action |
| `open youtube` | No matching action |
| `open YouTube` | Fixed music-video URL emitted to mocked typing |
| `even or odd -3` / `even or odd three` | No parity result |
| `open mail open Instagram` | Both action sequences emitted |
| Recognizer exception | Propagates from both listening functions |
| Fake clock at 12:00 / 16:00 | Morning / generic fallback respectively |

These are not measurements of real speech accuracy, audio latency, Windows compatibility, actual UI targeting or customer demand. None of those has been established.

## Dependency and licence findings

The checked-in environment includes SpeechRecognition 3.8.1, pyttsx3 2.90, PyAutoGUI 0.9.52, PyAudio 0.2.11, requests 2.25.1, urllib3 1.26.2, numerous PyObjC 7.0.1 frameworks, and additional packages not imported by `main.py`. See the complete CSV for all 124 metadata records.

- The root `LICENSE` grants MIT permissions, including sale, subject to preserving its notice. This does not establish exclusive rights to the name or relicense dependencies.
- The **bundled pyttsx3 2.90** metadata and licence file declare GPLv3. Do not apply the terms of a newer release to these older files. GPL does permit selling software; the issue to resolve is the distribution model and its obligations, not whether charging money is prohibited. The [historical release page](https://pypi.org/project/pyttsx3/2.90/) corroborates this version's declaration.
- MouseInfo 0.1.3 metadata declares GPLv3+. PyMsgBox 1.0.9 metadata declares GPLv3+, while its source header says Modified BSD: this discrepancy needs resolution before redistribution.
- PyScreeze metadata says MIT while its included licence is three-clause BSD. Preserve the actual notices and resolve provenance rather than trusting one metadata field.
- certifi includes MPL-2.0 terms; chardet has LGPL terms; SpeechRecognition includes separately licensed FLAC binaries. A top-level MIT label is not an adequate third-party notice bundle.
- Avoid reusing this environment. Build on a supported Python version from explicit, minimal dependencies; inspect the exact locked wheels, transitive native libraries and model files; generate an SBOM and third-party notices for the release artifact. Removing a dependency from future builds does not erase obligations for any copies already distributed.

Concrete dependency evidence, paths relative to `venv/lib/python3.9/site-packages/`:

| Evidence | Location |
|---|---|
| Generic speech key described for personal/testing use, subject to revocation | `speech_recognition/__init__.py:808–812` |
| HTTP recognition endpoint, FLAC payload and network call | `speech_recognition/__init__.py:831–840` |
| Unbounded default operation timeout / capture parameters | `speech_recognition/__init__.py:472` and `:579` |
| pyttsx3 GPL declaration and package-specific GPLv3-or-later notice | `pyttsx3-2.90.dist-info/METADATA:18` and `LICENSE:634–649` in that directory |
| PyMsgBox conflicting licence declarations | `PyMsgBox-1.0.9.dist-info/METADATA:8–10` and `pymsgbox/__init__.py:6` |
| Separately licensed bundled FLAC command-line binaries | `SpeechRecognition-3.8.1.dist-info/METADATA:412`; the referenced `LICENSE-FLAC.txt` is not tracked |
| macOS-specific wheel compatibility tags | `pyobjc_core-7.0.1.dist-info/WHEEL:4` and framework `WHEEL` files |

The dependency review inventoried 59 licence/notice files with 19 distinct contents. Of the 124 installed distributions, 100 are PyObjC packages; 61 distribution wheel tags are macOS x86-64. The installed-distribution count excludes nested vendored libraries in pip, setuptools and pkg_resources. Windows TTS dependencies and some unused packages' requirements are absent; this is not a complete working Windows environment. The [FLAC project's licence description](https://xiph.org/flac/license.html) distinguishes BSD libraries from GPL command-line tools.

Python 3.9 reached end of life on 31 October 2025 according to its [official release schedule](https://peps.python.org/pep-0596/). A new environment and compatibility tests are required; the plan proposes evaluating Python 3.13 with current compatible dependencies. The bundled SpeechRecognition imports `audioop`, which [Python removed in 3.13](https://docs.python.org/3/library/audioop.html), so upgrading the interpreter alone will not repair this checkout. Python's [virtual-environment guidance](https://docs.python.org/3/library/venv.html) also describes environments as nonportable and disposable.

## Decision

The repository is a useful starting experiment, **not a sellable application today**. Most commercial value would come from a new reliable interaction layer, limited workflow engine, deployment/support package, and customer-specific configuration. The sustainable asset would be repeatable workflows and service quality, not this small command script. Do not quote a completion percentage based on the number of files in `venv/`.
