"""Exact descriptor rules plus real NTFS/PowerShell parity on synthetic fixtures."""
import importlib
import importlib.util
import os
from types import SimpleNamespace

import pytest


OWNER = "S-1-5-21-111-222-333-1001"
SYSTEM = "S-1-5-18"
EVERYONE = "S-1-1-0"
ROOT = "D:\\synthetic-acl\\root"
FILE = ROOT + "\\file.txt"
CHILD = ROOT + "\\child"
FULL = 0x1F01FF


def api():
    assert importlib.util.find_spec("connectors.windows_acl") is not None, "Direct Windows ACL verifier is missing"
    return importlib.import_module("connectors.windows_acl")


class Descriptor:
    def __init__(self, *, owner=OWNER, flags=3, protected=True, inherited=False, rules=None):
        self.owner = owner
        self.control = 4 | (4096 if protected else 0)
        self.rules = rules if rules is not None else [((0, flags | (16 if inherited else 0)), FULL, sid) for sid in (OWNER, SYSTEM)]

    def GetSecurityDescriptorOwner(self): return self.owner
    def GetSecurityDescriptorControl(self): return (self.control, 1)
    def GetSecurityDescriptorDacl(self): return self
    def GetAceCount(self): return len(self.rules)
    def GetAce(self, index): return self.rules[index]


class Backend:
    def __init__(self):
        self.user = OWNER
        self.records = {ROOT: (16, Descriptor()), FILE: (32, Descriptor(flags=0, protected=False, inherited=True)),
                        CHILD: (16, Descriptor(protected=False, inherited=True))}
        self.events = []

    def current_sid(self):
        self.events.append("token")
        return self.user

    def attributes(self, path):
        self.events.append(("attributes", path))
        return self.records[path][0]

    def descriptor(self, path):
        self.events.append(("descriptor", path))
        return self.records[path][1]

    def sid_string(self, sid): return sid


@pytest.fixture
def fake(monkeypatch):
    module = api()
    backend = Backend()
    monkeypatch.setattr(module, "_WindowsSecurity", lambda: backend)
    return module, backend


def test_exact_private_root_and_inherited_file_directory_are_accepted(fake):
    module, _ = fake
    assert module.verify_private_paths([ROOT, FILE, CHILD]) is None


@pytest.mark.parametrize("change", ["wrong_owner", "unprotected", "inherited_root", "missing_system", "extra_principal", "duplicate_owner",
                                        "deny", "object_ace", "read_only", "generic_all", "no_propagate", "inherit_only", "unknown_flags", "missing_dir_inheritance", "null_dacl", "absent_dacl"])
def test_relaxed_or_unexpected_root_security_is_rejected(fake, change):
    module, backend = fake
    descriptor = backend.records[ROOT][1]
    if change == "wrong_owner": descriptor.owner = SYSTEM
    elif change == "unprotected": descriptor.control = 4
    elif change == "inherited_root": descriptor.rules[0] = ((0, 19), FULL, OWNER)
    elif change == "missing_system": descriptor.rules.pop()
    elif change == "extra_principal": descriptor.rules.append(((0, 3), FULL, EVERYONE))
    elif change == "duplicate_owner": descriptor.rules[1] = ((0, 3), FULL, OWNER)
    elif change == "deny": descriptor.rules[0] = ((1, 3), FULL, OWNER)
    elif change == "object_ace": descriptor.rules[0] = ((5, 3), FULL, None, None, OWNER)
    elif change == "read_only": descriptor.rules[0] = ((0, 3), 0x120089, OWNER)
    elif change == "generic_all": descriptor.rules[0] = ((0, 3), 0x10000000, OWNER)
    elif change == "no_propagate": descriptor.rules[0] = ((0, 7), FULL, OWNER)
    elif change == "inherit_only": descriptor.rules[0] = ((0, 11), FULL, OWNER)
    elif change == "unknown_flags": descriptor.rules[0] = ((0, 35), FULL, OWNER)
    elif change == "missing_dir_inheritance": descriptor.rules[0] = ((0, 1), FULL, OWNER)
    elif change == "null_dacl": descriptor.GetSecurityDescriptorDacl = lambda: None
    else: descriptor.control = 4096
    with pytest.raises(module.WindowsAclError): module.verify_private_paths([ROOT])


@pytest.mark.parametrize("path,flags", [(FILE, 3), (FILE, 20), (CHILD, 16), (CHILD, 23)])
def test_child_rules_preserve_exact_inheritance_and_no_propagation(fake, path, flags):
    module, backend = fake
    backend.records[path][1].rules[0] = ((0, flags), FULL, OWNER)
    with pytest.raises(module.WindowsAclError): module.verify_private_paths([ROOT, path])


def test_system_identity_requires_one_unique_system_rule(fake):
    module, backend = fake
    backend.user = SYSTEM
    backend.records[ROOT] = (16, Descriptor(owner=SYSTEM, rules=[((0, 3), FULL, SYSTEM)]))
    module.verify_private_paths([ROOT])


@pytest.mark.parametrize("paths", [[], ["relative"], ["D:relative"], ["\\relative"], [ROOT + "\nother"], [ROOT + "\x00"], "not-an-inventory", [b"D:\\bytes"]])
def test_invalid_inventory_is_rejected_before_native_security_reads(fake, paths):
    module, backend = fake
    with pytest.raises(module.WindowsAclError): module.verify_private_paths(paths)
    assert backend.events == []


def test_inventory_count_and_utf8_byte_bounds_precede_native_reads(fake):
    module, backend = fake
    with pytest.raises(module.WindowsAclError): module.verify_private_paths([ROOT] * 50001)
    with pytest.raises(module.WindowsAclError): module.verify_private_paths(["D:\\" + "x" * (8 * 1024 * 1024)])
    assert backend.events == []


@pytest.mark.parametrize("attributes", [16 | 1024, 32 | 1024])
def test_reparse_points_are_refused_before_following_their_security_descriptor(fake, attributes):
    module, backend = fake
    backend.records[ROOT] = (attributes, Descriptor())
    with pytest.raises(module.WindowsAclError): module.verify_private_paths([ROOT])
    assert not any(isinstance(e, tuple) and e[0] == "descriptor" for e in backend.events)


def test_first_path_must_be_a_directory(fake):
    module, _ = fake
    with pytest.raises(module.WindowsAclError): module.verify_private_paths([FILE])


def test_fresh_checks_reject_acl_change_without_cache(fake):
    module, backend = fake
    module.verify_private_paths([ROOT, FILE])
    backend.records[FILE][1].rules.append(((0, 0), FULL, EVERYONE))
    with pytest.raises(module.WindowsAclError): module.verify_private_paths([ROOT, FILE])


def test_native_failure_does_not_leak_path_or_error_and_never_falls_back(fake):
    module, backend = fake
    backend.descriptor = lambda _: (_ for _ in ()).throw(OSError("PRIVATE_PATH_AND_TOKEN"))
    with pytest.raises(module.WindowsAclError) as caught: module.verify_private_paths([ROOT])
    assert "PRIVATE" not in str(caught.value) and ROOT not in str(caught.value)


def test_current_impersonation_identity_is_checked_again_on_each_call(fake):
    module, backend = fake
    module.verify_private_paths([ROOT])
    backend.user = SYSTEM
    with pytest.raises(module.WindowsAclError): module.verify_private_paths([ROOT])


def test_reparse_substitution_during_security_lookup_is_rejected(fake):
    module, backend = fake
    original = backend.descriptor
    def substituted(path):
        result = original(path)
        backend.records[path] = (16 | 1024, result)
        return result
    backend.descriptor = substituted
    with pytest.raises(module.WindowsAclError): module.verify_private_paths([ROOT])


@pytest.mark.parametrize("thread_state", ["impersonated", "no_token", "access_denied"])
def test_native_identity_uses_thread_token_and_only_no_token_allows_process_fallback(thread_state):
    module = api()
    backend = object.__new__(module._WindowsSecurity)
    calls = []
    class NativeError(Exception):
        def __init__(self, code): self.winerror = code
    class Token:
        def __init__(self, sid): self.sid = sid
        def Close(self): calls.append("closed")
    def thread(*_):
        if thread_state == "impersonated": return Token("thread-sid")
        raise NativeError(1008 if thread_state == "no_token" else 5)
    def process(*_):
        calls.append("process")
        return Token("process-sid")
    backend.api = SimpleNamespace(GetCurrentThread=lambda: 1, GetCurrentProcess=lambda: 2)
    backend.con = SimpleNamespace(TOKEN_QUERY=8)
    backend.errors = SimpleNamespace(ERROR_NO_TOKEN=1008)
    backend.security = SimpleNamespace(OpenThreadToken=thread, OpenProcessToken=process,
        GetTokenInformation=lambda token, _: (token.sid, 0), TokenUser=1, ConvertSidToStringSid=lambda sid: sid)
    if thread_state == "access_denied":
        with pytest.raises(NativeError): backend.current_sid()
        assert calls == []
    else:
        assert backend.current_sid() == ("thread-sid" if thread_state == "impersonated" else "process-sid")
        assert calls == (["closed"] if thread_state == "impersonated" else ["process", "closed"])


def _powershell_accepts(paths):
    import base64
    import subprocess
    from connectors.workspace import _WINDOWS_VERIFY_ACL
    command = base64.b64encode(_WINDOWS_VERIFY_ACL.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
        input="".join(str(path) + "\n" for path in paths), capture_output=True, text=True, encoding="utf-8", timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    return result.returncode == 0


@pytest.mark.skipif(os.name != "nt", reason="Native Windows ACL and PowerShell parity")
@pytest.mark.parametrize("change", ["everyone", "root_inheritance", "propagation", "junction"])
def test_real_ntfs_private_inventory_and_loosened_acl_match_existing_verifier(tmp_path, change):
    import win32api
    import win32con
    import win32security
    import time
    module = api()
    root = tmp_path / "private"
    root.mkdir()
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try: owner = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally: token.Close()
    system = win32security.ConvertStringSidToSid(SYSTEM)
    acl = win32security.ACL()
    for sid in (owner, system): acl.AddAccessAllowedAceEx(2, 3, FULL, sid)
    win32security.SetNamedSecurityInfo(str(root), win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        owner, None, acl, None)
    child = root / "child"
    child.mkdir(mode=0o777)
    file = child / "synthetic.txt"
    file.write_bytes(b"Synthetic ACL fixture only")
    paths = [root, child, file]
    module.verify_private_paths(paths)
    start = time.perf_counter()
    for _ in range(100): module.verify_private_paths(paths)
    native_ms = (time.perf_counter() - start) * 10
    start = time.perf_counter()
    assert _powershell_accepts(paths)
    shell_ms = (time.perf_counter() - start) * 1000
    print(f"ACL parity fixture={change} native_mean_ms={native_ms:.3f} powershell_ms={shell_ms:.3f}")
    junction = None
    if change == "everyone":
        acl = win32security.GetNamedSecurityInfo(str(file), win32security.SE_FILE_OBJECT,
                                               win32security.DACL_SECURITY_INFORMATION).GetSecurityDescriptorDacl()
        acl.AddAccessAllowedAceEx(2, 0, 0x120089, win32security.ConvertStringSidToSid(EVERYONE))
        win32security.SetNamedSecurityInfo(str(file), win32security.SE_FILE_OBJECT,
                                          win32security.DACL_SECURITY_INFORMATION, None, None, acl, None)
    elif change == "root_inheritance":
        win32security.SetNamedSecurityInfo(str(root), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.UNPROTECTED_DACL_SECURITY_INFORMATION, None, None, acl, None)
    elif change == "propagation":
        changed = win32security.ACL()
        for sid in (owner, system): changed.AddAccessAllowedAceEx(2, 7, FULL, sid)
        win32security.SetNamedSecurityInfo(str(child), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION, None, None, changed, None)
    else:
        import _winapi
        junction = root / "junction"
        _winapi.CreateJunction(str(child), str(junction))
        paths.append(junction)
    try:
        with pytest.raises(module.WindowsAclError): module.verify_private_paths(paths)
        assert not _powershell_accepts(paths)
    finally:
        if junction is not None: junction.rmdir()
    assert file.read_bytes() == b"Synthetic ACL fixture only"
