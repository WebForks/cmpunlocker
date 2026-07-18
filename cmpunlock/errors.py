# SPDX-License-Identifier: GPL-2.0-only


class CmpUnlockError(Exception):
    """A user-facing validation or execution failure."""


class ProfileError(CmpUnlockError):
    pass


class FirmwareError(CmpUnlockError):
    pass


class SystemCheckError(CmpUnlockError):
    pass


class ApplyError(CmpUnlockError):
    pass

