"""Scoped source access, immutable patch proposals and trusted atomic application.

Roots and identities must come from trusted project configuration, never model arguments.
Applying a proposal requires an authenticated approval handler; a matching hash alone is
not evidence of human consent. This component does not run source, install dependencies,
or claim to detect every secret that might be embedded in otherwise allowed source text.
"""
import difflib
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import threading
import uuid

from tools.hub_admin import AdminError, _restrict_directory as _protect_directory


MAX_FILE_BYTES = 256_000
MAX_FILES = 10_000
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_MATCHES = 50
MAX_STAGE_ENTRIES = 50_000
MAX_ACL_INPUT_BYTES = 8 * 1024 * 1024
EXTENSIONS = {".py", ".rs", ".ts", ".tsx", ".js", ".json", ".toml", ".md", ".css", ".html", ".yml", ".yaml", ".txt"}
DENIED_DIRECTORIES = {"node_modules", "data", "venv", "target", "build", "artifact", "artifacts", "dist", "__pycache__", "onedrive"}
SECRET_NAMES = ("credential", "password", "secret", "token", "private_key", "private-key", "privatekey",
                "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "apikey", "api_key")
RESERVED = {"con", "prn", "aux", "nul", "conin$", "conout$"} | {f"{prefix}{i}" for prefix in ("com", "lpt") for i in range(1, 10)}
HASH = re.compile(r"[0-9a-f]{64}\Z")
IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
PROPOSAL_ID = re.compile(r"[0-9a-f]{32}\Z")
STAGE_FILES = {"proposal.json", "replacement.utf8", "review.patch", "claim.json", "result.json", "snapshot.json"}

# Read-only ACL verification of an already bounded, validated exact-path inventory.
# Paths travel over stdin as UTF-8 data; they are never interpolated into script source.
_WINDOWS_VERIFY_ACL = r"""
$ErrorActionPreference = 'Stop'
try {
    [Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $expected = @($sid, 'S-1-5-18') | Select-Object -Unique
    $allow = [System.Security.AccessControl.AccessControlType]::Allow
    $rights = [System.Security.AccessControl.FileSystemRights]::FullControl
    $inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
    $none = [System.Security.AccessControl.PropagationFlags]::None
    $count = 0
    while (($target = [Console]::In.ReadLine()) -ne $null) {
        $count++
        if ($count -gt 50000 -or -not [System.IO.Path]::IsPathRooted($target)) { throw 'Invalid inventory' }
        $attributes = [System.IO.File]::GetAttributes($target)
        if (($attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Linked artifact' }
        $directory = ($attributes -band [System.IO.FileAttributes]::Directory) -ne 0
        if ($directory) { $acl = [System.IO.Directory]::GetAccessControl($target) }
        else { $acl = [System.IO.File]::GetAccessControl($target) }
        if ($acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -ne $sid) { throw 'Unexpected owner' }
        if ($count -eq 1 -and (-not $directory -or -not $acl.AreAccessRulesProtected)) { throw 'Unprotected root' }
        $rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
        if ($rules.Count -ne $expected.Count) { throw 'Unexpected rules' }
        foreach ($identity in $expected) {
            $matching = @($rules | Where-Object { $_.IdentityReference.Value -eq $identity })
            if ($matching.Count -ne 1) { throw 'Missing required rule' }
        }
        foreach ($rule in $rules) {
            if ($rule.IdentityReference.Value -notin $expected -or $rule.AccessControlType -ne $allow -or
                $rule.FileSystemRights -ne $rights -or $rule.PropagationFlags -ne $none) { throw 'Unexpected rights' }
            if ($count -eq 1 -and $rule.IsInherited) { throw 'Inherited root rule' }
            if ($directory -and $rule.InheritanceFlags -ne $inherit) { throw 'Invalid directory inheritance' }
            if (-not $directory -and $rule.InheritanceFlags -ne [System.Security.AccessControl.InheritanceFlags]::None) {
                throw 'Invalid file inheritance'
            }
        }
    }
    if ($count -eq 0) { throw 'Empty inventory' }
    exit 0
} catch {
    [Console]::Error.WriteLine('Staging permissions could not be verified.')
    exit 1
}
"""


class WorkspaceError(RuntimeError):
    """A scope, stale-source, storage or proposal-integrity failure."""


class PatchOutcomeUnknown(WorkspaceError):
    """The file replacement occurred, but durable result recording failed; never replay."""

    status = "unknown"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _file_lines(content: str) -> list[str]:
    segments = content.split("\n")
    return [segment + "\n" for segment in segments[:-1]] + ([segments[-1]] if segments[-1] else [])


def _entry(path: Path):
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        raise WorkspaceError("Linked paths, junctions and reparse points are not permitted.")
    return metadata


def _ancestors(path: Path) -> None:
    for component in (path, *path.parents):
        if component.exists() or component.is_symlink():
            _entry(component)


def _part_allowed(part: str) -> bool:
    lower = part.casefold()
    return (bool(part) and part not in {".", ".."} and not part.startswith(".")
            and not part.endswith((".", " ")) and len(part) <= 255
            and lower not in DENIED_DIRECTORIES and not lower.startswith("onedrive - ")
            and lower.split(".", 1)[0] not in RESERVED
            and not any(term in lower for term in SECRET_NAMES)
            and not any(ord(char) < 32 or char in '<>:"|?*\\' for char in part))


def _write_new(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())


def _source_parts(relative: str) -> list[str]:
    if (not isinstance(relative, str) or len(relative) > 1024
            or not all(_part_allowed(part) for part in relative.split("/"))
            or Path(relative).suffix.casefold() not in EXTENSIONS):
        raise WorkspaceError("Only permitted relative source paths may be accessed.")
    return relative.split("/")


def _read_file(path: Path, limit: int):
    before = _entry(path)
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit or before.st_nlink > 1:
        raise WorkspaceError("Source must be a bounded regular file without hard links.")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise WorkspaceError("Source path changed before it could be read.")
        data = source.read(limit + 1)
        after = os.fstat(source.fileno())
    if len(data) > limit or (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise WorkspaceError("Source changed during reading or exceeded its size limit.")
    return data, after


def _verify_permissions(paths: list[Path]) -> None:
    if os.name != "nt":
        for path in paths:
            metadata = _entry(path)
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise WorkspaceError("Existing staging artifacts are not private to the current OS user.")
        return
    from connectors.windows_acl import WindowsAclError, verify_private_paths
    try:
        verify_private_paths(paths)
    except WindowsAclError:
        raise WorkspaceError("Existing staging permissions could not be verified; access is disabled.") from None


def _verify_staging_directory(directory: Path) -> None:
    """Check known artifacts only, bounded to 50,000 entries; never repair existing ACLs."""
    root_metadata = _entry(directory)
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise WorkspaceError("Staging must be a regular directory.")
    inventory, pending, total = [directory], [(directory, "root")], len(str(directory).encode("utf-8")) + 1
    while pending:
        parent, kind = pending.pop()
        with os.scandir(parent) as entries:
            for entry in entries:
                if len(inventory) >= MAX_STAGE_ENTRIES:
                    raise WorkspaceError("Existing staging inventory exceeds its entry limit.")
                path = Path(entry.path)
                metadata = _entry(path)
                is_directory = stat.S_ISDIR(metadata.st_mode)
                if not is_directory and (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1):
                    raise WorkspaceError("Unexpected or hard-linked staging artifact.")
                if kind == "root":
                    permitted = (is_directory and PROPOSAL_ID.fullmatch(path.name)) or (not is_directory and path.name == ".edith-workspace.json")
                    next_kind = "artifact"
                elif kind == "artifact":
                    permitted = (is_directory and path.name == "snapshot") or (not is_directory and path.name in STAGE_FILES)
                    next_kind = "snapshot"
                else:
                    permitted = _part_allowed(path.name) and (is_directory or path.suffix.casefold() in EXTENSIONS)
                    next_kind = "snapshot"
                if not permitted or "\r" in str(path) or "\n" in str(path):
                    raise WorkspaceError("Existing staging contains an unexpected artifact.")
                total += len(str(path).encode("utf-8")) + 1
                if total > MAX_ACL_INPUT_BYTES:
                    raise WorkspaceError("Existing staging permission inventory exceeds its size limit.")
                inventory.append(path)
                if is_directory:
                    pending.append((path, next_kind))
    _verify_permissions(inventory)


class Workspace:
    def __init__(self, root, staging_dir, principal_id, project_id):
        source, stage = Path(root), Path(staging_dir)
        if not source.is_absolute() or not stage.is_absolute():
            raise WorkspaceError("Configure absolute source and staging directories.")
        if any(not isinstance(value, str) or not IDENTITY.fullmatch(value) for value in (principal_id, project_id)):
            raise WorkspaceError("Configure explicit principal and project identifiers.")
        try:
            _ancestors(source)
            _ancestors(stage)
            if not source.is_dir() or not stage.parent.is_dir():
                raise WorkspaceError("Source and staging parent directories must exist.")
            self.root = source.resolve()
            self.staging_dir = stage.resolve()
            if self.staging_dir.is_relative_to(self.root) or self.root.is_relative_to(self.staging_dir):
                raise WorkspaceError("Use a dedicated staging directory separate from the source tree.")
            if any(part.casefold() == "onedrive" or part.casefold().startswith("onedrive - ") for part in self.staging_dir.parts):
                raise WorkspaceError("Store staging artifacts outside OneDrive and other synchronized directories.")
            self.principal_id, self.project_id = principal_id, project_id
            self._binding = {"version": 1, "principal_id": principal_id, "project_id": project_id,
                             "root_fingerprint": _sha(os.path.normcase(str(self.root)).encode("utf-8"))}
            marker = self.staging_dir / ".edith-workspace.json"
            if self.staging_dir.exists():
                _verify_staging_directory(self.staging_dir)
                raw, _ = _read_file(marker, 4096)
                if json.loads(raw) != self._binding:
                    raise WorkspaceError("The staging directory belongs to a different workspace or identity.")
            else:
                self.staging_dir.mkdir(mode=0o700)
                _protect_directory(self.staging_dir)
                _write_new(marker, _canonical(self._binding))
        except (OSError, ValueError, AdminError):
            raise WorkspaceError("Cannot establish a protected, dedicated workspace staging directory.") from None
        self._lock = threading.RLock()

    def _source_path(self, relative: str) -> Path:
        candidate = self.root.joinpath(*_source_parts(relative))
        try:
            _ancestors(candidate)
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(self.root):
                raise WorkspaceError("Source path escapes its configured project.")
            _source_parts(resolved.relative_to(self.root).as_posix())
            _ancestors(resolved)
            return resolved
        except OSError:
            raise WorkspaceError("The selected source is unavailable.") from None

    def _read_regular(self, path):
        return _read_file(path, MAX_FILE_BYTES)

    def source_snapshot(self, path):
        candidate = self._source_path(path)
        try:
            content, _ = self._read_regular(candidate)
            text = content.decode("utf-8")
            if "\x00" in text:
                raise WorkspaceError("Binary content is not source text.")
        except (OSError, UnicodeError):
            raise WorkspaceError("Source is unreadable or is not UTF-8 text.") from None
        return {"path": candidate.relative_to(self.root).as_posix(), "content": text, "sha256": _sha(content), "bytes": len(content),
                "principal_id": self.principal_id, "project_id": self.project_id}

    def search(self, query):
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise WorkspaceError("Search requires 1 to 200 characters of literal text.")
        matches, scanned, total = [], 0, 0
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            permitted = []
            for name in sorted(dirs):
                if not _part_allowed(name):
                    continue
                try:
                    _entry(Path(directory) / name)
                    permitted.append(name)
                except (OSError, WorkspaceError):
                    pass
            dirs[:] = permitted
            for filename in sorted(files):
                if not _part_allowed(filename) or Path(filename).suffix.casefold() not in EXTENSIONS:
                    continue
                if scanned >= MAX_FILES or total >= MAX_SNAPSHOT_BYTES:
                    return {"matches": matches, "files_scanned": scanned, "truncated": True}
                relative = (Path(directory) / filename).relative_to(self.root).as_posix()
                scanned += 1
                try:
                    snapshot = self.source_snapshot(relative)
                except WorkspaceError:
                    continue
                if total + snapshot["bytes"] > MAX_SNAPSHOT_BYTES:
                    return {"matches": matches, "files_scanned": scanned, "truncated": True}
                total += snapshot["bytes"]
                for line_number, text in enumerate(snapshot["content"].splitlines(), 1):
                    if query.casefold() in text.casefold():
                        matches.append({"path": snapshot["path"], "line": line_number, "text": text[:500]})
                        if len(matches) >= MAX_MATCHES:
                            return {"matches": matches, "files_scanned": scanned, "truncated": True}
        return {"matches": matches, "files_scanned": scanned, "truncated": False}

    def _new_stage(self) -> tuple[str, Path]:
        _ancestors(self.staging_dir)
        identifier = uuid.uuid4().hex
        folder = self.staging_dir / identifier
        # On recent Windows Python, mode=0700 installs a new ACL containing
        # Administrators. Inherit the protected parent's exact ACL instead.
        folder.mkdir(mode=0o777 if os.name == "nt" else 0o700)
        return identifier, folder

    def prepare_patch(self, path, expected_sha256, new_content):
        if not isinstance(expected_sha256, str) or not HASH.fullmatch(expected_sha256):
            raise WorkspaceError("An exact source SHA-256 is required.")
        if not isinstance(new_content, str) or "\x00" in new_content:
            raise WorkspaceError("Replacement must be UTF-8 source text.")
        try:
            replacement = new_content.encode("utf-8")
        except UnicodeError:
            raise WorkspaceError("Replacement must be valid UTF-8 source text.") from None
        if len(replacement) > MAX_FILE_BYTES:
            raise WorkspaceError("Replacement exceeds the 256 KB source limit.")
        original = self.source_snapshot(path)
        path = original["path"]
        if not hmac.compare_digest(expected_sha256, original["sha256"]):
            raise WorkspaceError("Source changed; read it again before preparing a patch.")
        if replacement == original["content"].encode("utf-8"):
            raise WorkspaceError("Replacement does not change the source.")
        lines = difflib.unified_diff(_file_lines(original["content"]), _file_lines(new_content),
                                     fromfile=f"a/{path}", tofile=f"b/{path}")
        diff = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
        with self._lock:
            try:
                identifier, folder = self._new_stage()
                manifest = {**self._binding, "id": identifier, "path": path, "base_sha256": expected_sha256,
                            "new_sha256": _sha(replacement), "diff_sha256": _sha(diff.encode("utf-8"))}
                digest = _sha(_canonical(manifest))
                _write_new(folder / "replacement.utf8", replacement)
                _write_new(folder / "review.patch", diff.encode("utf-8"))
                _write_new(folder / "proposal.json", _canonical({**manifest, "proposal_hash": digest}))
            except OSError:
                raise WorkspaceError("Could not stage the patch; source was not modified.") from None
        return {**manifest, "proposal_hash": digest, "diff": diff, "status": "awaiting_review"}

    def _proposal(self, identifier: str, proposal_hash: str) -> tuple[dict, bytes, Path]:
        if not isinstance(identifier, str) or not PROPOSAL_ID.fullmatch(identifier) or not isinstance(proposal_hash, str) or not HASH.fullmatch(proposal_hash):
            raise WorkspaceError("A valid proposal identifier and exact approval hash are required.")
        folder = self.staging_dir / identifier
        try:
            _ancestors(folder)
            raw, _ = _read_file(folder / "proposal.json", 8192)
            receipt = json.loads(raw)
            fields = set(self._binding) | {"id", "path", "base_sha256", "new_sha256", "diff_sha256", "proposal_hash"}
            if not isinstance(receipt, dict) or set(receipt) != fields or receipt["id"] != identifier:
                raise WorkspaceError("Invalid staged proposal.")
            if any(receipt[key] != value for key, value in self._binding.items()):
                raise WorkspaceError("Proposal belongs to a different project or principal.")
            manifest = {key: value for key, value in receipt.items() if key != "proposal_hash"}
            digest = _sha(_canonical(manifest))
            if not isinstance(receipt["proposal_hash"], str) or not hmac.compare_digest(digest, proposal_hash) or not hmac.compare_digest(digest, receipt["proposal_hash"]):
                raise WorkspaceError("Staged proposal changed after review.")
            replacement, _ = _read_file(folder / "replacement.utf8", MAX_FILE_BYTES)
            diff, _ = _read_file(folder / "review.patch", MAX_FILE_BYTES * 4 + 8192)
            if _sha(replacement) != receipt["new_sha256"] or _sha(diff) != receipt["diff_sha256"]:
                raise WorkspaceError("Staged content differs from the reviewed proposal.")
            return receipt, replacement, folder
        except (OSError, ValueError, TypeError):
            raise WorkspaceError("The staged proposal is unavailable or invalid.") from None

    def apply_patch(self, proposal_id, proposal_hash, expected_sha256):
        """Apply only after a trusted handler authenticates and approves this exact hash.

        Local hashes bind the review, but do not replace user authorization. All executions
        are single-use. An interrupted apply must be reconciled, never silently retried.
        """
        with self._lock:
            receipt, replacement, folder = self._proposal(proposal_id, proposal_hash)
            if expected_sha256 != receipt["base_sha256"]:
                raise WorkspaceError("Approval baseline does not match this proposal.")
            source = self._source_path(receipt["path"])
            original, metadata = self._read_regular(source)
            if _sha(original) != expected_sha256:
                raise WorkspaceError("Source changed after review; prepare a new patch.")
            temporary = source.parent / f".edith-write-{uuid.uuid4().hex}.tmp"
            replaced = False
            try:
                _write_new(folder / "claim.json", _canonical({"proposal_hash": proposal_hash, "state": "claimed"}))
                _write_new(temporary, replacement)
                temporary.chmod(stat.S_IMODE(metadata.st_mode))
                # Revalidate after writing the temporary file to avoid overwriting an observed edit.
                current, _ = self._read_regular(self._source_path(receipt["path"]))
                if _sha(current) != expected_sha256:
                    raise WorkspaceError("Source changed during preparation; the approved patch was not applied.")
                os.replace(temporary, source)
                replaced = True
                result = {"id": proposal_id, "proposal_hash": proposal_hash, "principal_id": self.principal_id,
                          "project_id": self.project_id, "path": receipt["path"], "base_sha256": expected_sha256,
                          "new_sha256": receipt["new_sha256"], "status": "applied"}
                _write_new(folder / "result.json", _canonical(result))
                return result
            except OSError:
                if replaced:
                    raise PatchOutcomeUnknown("Patch may be applied, but its receipt could not be saved. Outcome unknown; do not replay.") from None
                raise WorkspaceError("Patch was not applied or was already claimed. Reconcile before retrying.") from None
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def create_worker_snapshot(self, paths):
        """Copy explicitly selected source only, at most 10,000 files and 16 MB, without execution."""
        if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_FILES or any(not isinstance(path, str) for path in paths):
            raise WorkspaceError("Select between one and 10,000 exact source paths.")
        if len({os.path.normcase(path) for path in paths}) != len(paths):
            raise WorkspaceError("Snapshot paths must be unique.")
        snapshots, total = [], 0
        for path in paths:
            canonical = self._source_path(path).relative_to(self.root).as_posix()
            if canonical != path:
                raise WorkspaceError("Worker snapshots require exact canonical source path spellings, without aliases.")
            item = self.source_snapshot(path)
            if item["path"] != path:
                raise WorkspaceError("Source spelling changed during snapshot preparation.")
            total += item["bytes"]
            if total > MAX_SNAPSHOT_BYTES:
                raise WorkspaceError("Selected snapshot exceeds 16 MB.")
            snapshots.append(item)
        try:
            identifier, folder = self._new_stage()
            destination = folder / "snapshot"
            destination.mkdir(mode=0o777 if os.name == "nt" else 0o700)
            files = []
            for item in snapshots:
                target = destination.joinpath(*item["path"].split("/"))
                target.parent.mkdir(mode=0o777 if os.name == "nt" else 0o700, parents=True, exist_ok=True)
                _write_new(target, item["content"].encode("utf-8"))
                files.append({"path": item["path"], "sha256": item["sha256"], "bytes": item["bytes"]})
            manifest = {**self._binding, "id": identifier, "files": files, "total_bytes": total}
            digest = _sha(_canonical(manifest))
            _write_new(folder / "snapshot.json", _canonical({**manifest, "snapshot_hash": digest}))
            return {**manifest, "snapshot_hash": digest, "directory": str(destination), "status": "prepared_not_executed"}
        except OSError:
            raise WorkspaceError("Could not prepare the bounded worker snapshot; no source was executed.") from None

    def verify_worker_snapshot(self, snapshot_id, snapshot_hash):
        """Verify staged contents immediately before planning; this does not authorize execution."""
        if (not isinstance(snapshot_id, str) or not PROPOSAL_ID.fullmatch(snapshot_id)
                or not isinstance(snapshot_hash, str) or not HASH.fullmatch(snapshot_hash)):
            raise WorkspaceError("An exact snapshot identifier and hash are required.")
        folder = self.staging_dir / snapshot_id
        destination = folder / "snapshot"
        try:
            _ancestors(destination)
            raw, _ = _read_file(folder / "snapshot.json", MAX_FILES * 1280 + 8192)
            receipt = json.loads(raw)
            fields = set(self._binding) | {"id", "files", "total_bytes", "snapshot_hash"}
            if not isinstance(receipt, dict) or set(receipt) != fields or receipt["id"] != snapshot_id:
                raise WorkspaceError("Invalid snapshot receipt.")
            if any(receipt[key] != value for key, value in self._binding.items()):
                raise WorkspaceError("Snapshot belongs to a different project or principal.")
            digest = _sha(_canonical({key: value for key, value in receipt.items() if key != "snapshot_hash"}))
            if (not isinstance(receipt["snapshot_hash"], str) or not hmac.compare_digest(digest, snapshot_hash)
                    or not hmac.compare_digest(digest, receipt["snapshot_hash"])):
                raise WorkspaceError("Snapshot receipt changed after preparation.")
            if (not isinstance(receipt["files"], list) or not 1 <= len(receipt["files"]) <= MAX_FILES
                    or type(receipt["total_bytes"]) is not int or not 0 <= receipt["total_bytes"] <= MAX_SNAPSHOT_BYTES):
                raise WorkspaceError("Snapshot contents exceed configured limits.")
            expected, allowed_dirs, total = {}, set(), 0
            for item in receipt["files"]:
                if not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes"}:
                    raise WorkspaceError("Invalid snapshot file entry.")
                parts = _source_parts(item["path"])
                key = os.path.normcase(item["path"])
                if (key in expected or not isinstance(item["sha256"], str) or not HASH.fullmatch(item["sha256"])
                        or type(item["bytes"]) is not int or not 0 <= item["bytes"] <= MAX_FILE_BYTES):
                    raise WorkspaceError("Invalid or duplicate snapshot source.")
                expected[key] = item
                allowed_dirs.update("/".join(parts[:count]) for count in range(1, len(parts)))
                total += item["bytes"]
            if total != receipt["total_bytes"] or not destination.is_dir():
                raise WorkspaceError("Snapshot size or directory does not match its receipt.")
            found = set()
            for directory, dirs, files in os.walk(destination, followlinks=False):
                for name in dirs:
                    path = Path(directory) / name
                    _entry(path)
                    if path.relative_to(destination).as_posix() not in allowed_dirs:
                        raise WorkspaceError("Snapshot contains an unselected directory.")
                for name in files:
                    path = Path(directory) / name
                    key = os.path.normcase(path.relative_to(destination).as_posix())
                    if key not in expected or key in found:
                        raise WorkspaceError("Snapshot contains an unselected file.")
                    data, _ = _read_file(path, MAX_FILE_BYTES)
                    if len(data) != expected[key]["bytes"] or _sha(data) != expected[key]["sha256"]:
                        raise WorkspaceError("Snapshot source changed after preparation.")
                    found.add(key)
            if found != set(expected):
                raise WorkspaceError("Snapshot is missing a selected source.")
            return {**receipt, "directory": str(destination), "status": "verified_not_executed"}
        except (OSError, ValueError, TypeError):
            raise WorkspaceError("The selected snapshot is unavailable or invalid.") from None
