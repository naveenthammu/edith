"""Fresh, read-only checks for EDITH's exact private Windows filesystem ACLs.

This is the native equivalent of workspace._WINDOWS_VERIFY_ACL, without spawning
PowerShell for each check. It never repairs permissions or caches authorization.
The caller still owns path-inventory, ancestor, hard-link and read-race checks;
this function checks each named object and its ACL, not file contents.

https://timgolden.me.uk/pywin32-docs/win32security__GetNamedSecurityInfo_meth.html
https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-ace_header
"""
import ntpath
import os


MAX_PATHS = 50_000
MAX_INPUT_BYTES = 8 * 1024 * 1024
_SYSTEM = "S-1-5-18"
_DIRECTORY = 0x10
_REPARSE = 0x400
_DACL_PRESENT = 0x0004
_DACL_PROTECTED = 0x1000
_FULL_CONTROL = 0x001F01FF
_OBJECT_AND_CONTAINER_INHERIT = 0x03
_INHERITED = 0x10


class WindowsAclError(RuntimeError):
    """Private permissions could not be proven; contains no path or native error."""


class _WindowsSecurity:
    """Narrow native read boundary; importing this module has no Windows side effects."""
    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows is required")
        import win32api
        import win32con
        import win32file
        import win32security
        import winerror
        self.api, self.con, self.file, self.security, self.errors = win32api, win32con, win32file, win32security, winerror

    def current_sid(self):
        # Match the effective Windows identity, including thread impersonation.
        try:
            token = self.security.OpenThreadToken(self.api.GetCurrentThread(), self.con.TOKEN_QUERY, True)
        except Exception as exc:
            if getattr(exc, "winerror", None) != self.errors.ERROR_NO_TOKEN:
                raise
            token = self.security.OpenProcessToken(self.api.GetCurrentProcess(), self.con.TOKEN_QUERY)
        try:
            return self.sid_string(self.security.GetTokenInformation(token, self.security.TokenUser)[0])
        finally:
            token.Close()

    def attributes(self, path):
        return self.file.GetFileAttributes(path)

    def descriptor(self, path):
        return self.security.GetNamedSecurityInfo(path, self.security.SE_FILE_OBJECT,
            self.security.OWNER_SECURITY_INFORMATION | self.security.DACL_SECURITY_INFORMATION)

    def sid_string(self, sid):
        return self.security.ConvertSidToStringSid(sid)


def _inventory(paths):
    if isinstance(paths, (str, bytes, os.PathLike)):
        raise ValueError("inventory")
    result, size = [], 0
    for path in paths:
        if len(result) >= MAX_PATHS:
            raise ValueError("count")
        value = os.fspath(path)
        # Python < 3.13 treats "\\x" as absolute; demand a drive or UNC share too.
        if (not isinstance(value, str) or not ntpath.isabs(value)
                or not ntpath.splitdrive(value)[0] or any(ord(c) < 32 for c in value)):
            raise ValueError("path")
        size += len(value.encode("utf-8")) + 1
        if size > MAX_INPUT_BYTES:
            raise ValueError("bytes")
        result.append(value)
    if not result:
        raise ValueError("empty")
    return result


def _check_descriptor(descriptor, backend, owner, *, directory, first):
    if backend.sid_string(descriptor.GetSecurityDescriptorOwner()) != owner:
        raise ValueError("owner")
    control, _revision = descriptor.GetSecurityDescriptorControl()
    if not control & _DACL_PRESENT or (first and (not directory or not control & _DACL_PROTECTED)):
        raise ValueError("protection")
    expected = {owner, _SYSTEM}
    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None or acl.GetAceCount() != len(expected):
        raise ValueError("rule count")
    seen = set()
    effective_flags = _OBJECT_AND_CONTAINER_INHERIT if directory else 0
    allowed_flags = {effective_flags} if first else {effective_flags, effective_flags | _INHERITED}
    for index in range(acl.GetAceCount()):
        ace = acl.GetAce(index)
        if not isinstance(ace, tuple) or len(ace) != 3:
            raise ValueError("ACE shape")
        header, mask, sid = ace
        if (not isinstance(header, tuple) or len(header) != 2 or header[0] != 0
                or header[1] not in allowed_flags or mask != _FULL_CONTROL):
            raise ValueError("rights")
        identity = backend.sid_string(sid)
        if identity not in expected or identity in seen:
            raise ValueError("principal")
        seen.add(identity)
    if seen != expected:
        raise ValueError("missing principal")


def verify_private_paths(paths):
    """Require an exact current-user/SYSTEM FullControl ACL on every supplied path.

    First path must be a protected directory with explicit OI|CI rules. Descendant
    directories retain OI|CI, files retain neither, and descendants may be inherited.
    Deny, object/callback, extra, duplicate, missing and unknown-flag ACEs fail closed.
    Missing pywin32 or any native read error also fails; there is no fallback.
    """
    try:
        inventory = _inventory(paths)
        backend = _WindowsSecurity()
        owner = backend.current_sid()
        for index, path in enumerate(inventory):
            attributes = backend.attributes(path)
            if attributes & _REPARSE:
                raise ValueError("linked")
            directory = bool(attributes & _DIRECTORY)
            if index == 0 and not directory:
                raise ValueError("root")
            descriptor = backend.descriptor(path)
            _check_descriptor(descriptor, backend, owner, directory=directory, first=index == 0)
            # A path substituted with a junction during descriptor lookup is never accepted.
            if backend.attributes(path) & (_DIRECTORY | _REPARSE) != attributes & (_DIRECTORY | _REPARSE):
                raise ValueError("attributes changed")
    except Exception:
        raise WindowsAclError("Private Windows permissions could not be verified; access is disabled.") from None
