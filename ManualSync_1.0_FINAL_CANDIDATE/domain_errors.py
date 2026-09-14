"""Structured domain errors — a stable code, never a sentence.

Why this exists
---------------
Services used to refuse an operation by raising a ready-made Italian
sentence::

    raise ValueError("actor è obbligatorio per registrare questa decisione.")

That is incompatible with the localization architecture frozen in
6d49fbe: the UI can be English while the service that refused the
operation only knows how to say why in Italian. It also invites the worse
failure — branching on message text — which breaks the moment a word
changes or a language is switched.

So a refusal now carries a **stable machine-readable code** and optional
**structured parameters**. The domain says *what rule was broken*; the UI
decides *what words to use*. This module therefore imports nothing: no
i18n, no Gradio, no catalog. A domain module that could reach a
translation catalog would eventually use one.

Compatibility, deliberately
---------------------------
``DomainValidationError`` also inherits ``ValueError`` and
``DomainNotFoundError`` also inherits ``KeyError``, because callers and
suites already written around ``except ValueError`` / ``except KeyError``
must keep working exactly as before. This is a real dependency, chosen
rather than inherited by accident, and it costs nothing: the class
hierarchy is not what the architecture forbids -- parsing the *message*
is, and that is now impossible because the message IS the code.

``str(exc)`` returns the bare code (``ERR_ACTOR_REQUIRED``), which is
stable, greppable and safe in a log. It is deliberately NOT a sentence:
anything that prints it to a user is visibly wrong rather than subtly
untranslated. ``repr()`` adds the parameters for debugging.
"""
from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """A domain rule refused an operation.

    :param code: stable identifier, e.g. ``ERR_ACTOR_REQUIRED``. Never
        localized, never parsed, safe to persist in a log.
    :param params: structured detail the UI may interpolate into whatever
        sentence it chooses. Values must be plain data.
    """

    #: Subclasses may declare a default so call sites stay short.
    code: str = "ERR_UNKNOWN"

    def __init__(self, code: str | None = None, /, **params: Any) -> None:
        self.code = str(code or type(self).code)
        self.params: dict[str, Any] = dict(params)
        super().__init__(self.code)

    def __str__(self) -> str:
        # The code, alone. A sentence here is what this class exists to
        # prevent -- if it ever reaches a user unresolved, it should look
        # wrong, not merely look like the wrong language.
        return self.code

    def __repr__(self) -> str:
        if not self.params:
            return f"{type(self).__name__}({self.code!r})"
        detail = ", ".join(f"{k}={v!r}" for k, v in sorted(self.params.items()))
        return f"{type(self).__name__}({self.code!r}, {detail})"

    def as_dict(self) -> dict[str, Any]:
        """Deterministic serialization: sorted params, no object identity."""
        return {"code": self.code,
                "params": {k: self.params[k] for k in sorted(self.params)}}

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DomainError):
            return NotImplemented
        return type(self) is type(other) and self.as_dict() == other.as_dict()

    def __hash__(self) -> int:
        return hash((type(self).__name__, self.code,
                     tuple(sorted((k, repr(v)) for k, v in self.params.items()))))


class DomainValidationError(DomainError, ValueError):
    """Input or state a human can correct: a missing actor, a decision that
    contradicts another, a required justification. Also a ValueError so
    existing ``except ValueError`` handlers keep catching it."""

    code = "ERR_VALIDATION"


class DomainNotFoundError(DomainError, KeyError):
    """Something referenced does not exist (any more). Also a KeyError so
    existing ``except KeyError`` handlers keep catching it."""

    code = "ERR_NOT_FOUND"

    def __str__(self) -> str:  # KeyError would otherwise quote the message
        return self.code


class DomainAlreadyExistsError(DomainError, FileExistsError):
    """Creating something that is already there.

    Also a FileExistsError because create_project() has always raised one
    and three call sites -- including two suites -- catch exactly that.
    """

    code = "ERR_ALREADY_EXISTS"

    def __str__(self) -> str:  # OSError formats its args otherwise
        return self.code


class DomainFileMissingError(DomainError, FileNotFoundError):
    """A file or repository that must exist does not.

    Sits between category A and B: the cause is technical, but the user
    can act on it (open or create a project). Also a FileNotFoundError
    because app.py already branches on that in three places.
    """

    code = "ERR_FILE_MISSING"

    def __str__(self) -> str:
        return self.code


# ---------------------------------------------------------------------
# Codes migrated in this gate.
#
# Scope is deliberately the surface that is ALREADY localized -- shared
# workspace/context, Conformità, and the Starter Pack actions reachable
# from it. Converting all 228 Italian raises in one pass would touch every
# product area at once, which is exactly the kind of change that cannot be
# reviewed or reverted cleanly.
#
# Not migrated, on purpose: invariant violations the UI cannot produce
# ("applies deve essere uno tra ...", "campi non aggiornabili", unsupported
# evidence_kind). Those are programmer errors. Turning them into polished
# user sentences would dress up a bug as a normal outcome.
# ---------------------------------------------------------------------
ERR_ACTOR_REQUIRED = "ERR_ACTOR_REQUIRED"
ERR_JUSTIFICATION_REQUIRED = "ERR_JUSTIFICATION_REQUIRED"
ERR_COVERAGE_REQUIRES_APPLIES = "ERR_COVERAGE_REQUIRES_APPLIES"
ERR_COVERED_REQUIRES_EVIDENCE = "ERR_COVERED_REQUIRES_EVIDENCE"
ERR_EVIDENCE_OBJECT_NOT_FOUND = "ERR_EVIDENCE_OBJECT_NOT_FOUND"
ERR_EVIDENCE_NOT_FOUND = "ERR_EVIDENCE_NOT_FOUND"
ERR_REQUIREMENT_NOT_FOUND = "ERR_REQUIREMENT_NOT_FOUND"
ERR_EVIDENCE_EXTERNAL_INCOMPLETE = "ERR_EVIDENCE_EXTERNAL_INCOMPLETE"
ERR_EVIDENCE_INTERNAL_INCOMPLETE = "ERR_EVIDENCE_INTERNAL_INCOMPLETE"
ERR_PACK_NOT_INSTALLED = "ERR_PACK_NOT_INSTALLED"
ERR_PACK_NO_REQUIREMENTS_SELECTED = "ERR_PACK_NO_REQUIREMENTS_SELECTED"
ERR_PROJECT_EXISTS = "ERR_PROJECT_EXISTS"
ERR_PROJECT_MANIFEST_INVALID = "ERR_PROJECT_MANIFEST_INVALID"
ERR_PROJECT_REPOSITORY_MISSING = "ERR_PROJECT_REPOSITORY_MISSING"

#: Every code this gate migrated. The localization suite asserts both
#: catalogs carry "error.<code>" for each one.
MIGRATED_CODES = (
    ERR_ACTOR_REQUIRED,
    ERR_JUSTIFICATION_REQUIRED,
    ERR_COVERAGE_REQUIRES_APPLIES,
    ERR_COVERED_REQUIRES_EVIDENCE,
    ERR_EVIDENCE_OBJECT_NOT_FOUND,
    ERR_EVIDENCE_NOT_FOUND,
    ERR_REQUIREMENT_NOT_FOUND,
    ERR_EVIDENCE_EXTERNAL_INCOMPLETE,
    ERR_EVIDENCE_INTERNAL_INCOMPLETE,
    ERR_PACK_NOT_INSTALLED,
    ERR_PACK_NO_REQUIREMENTS_SELECTED,
    ERR_PROJECT_EXISTS,
    ERR_PROJECT_MANIFEST_INVALID,
    ERR_PROJECT_REPOSITORY_MISSING,
)

# ---------------------------------------------------------------------
# Authoring / Contenuti — first migration batch.
#
# Repository and object validation only: the errors that say "this input
# is not usable" or "that file/repository is not there". Deliberately NOT
# the 32 "actor è obbligatorio ..." raises, which share one condition but
# 22 different sentences -- collapsing those needs a wording decision,
# and this gate is forbidden from changing wording.
#
# Each catalog entry reproduces the CURRENT Italian sentence exactly,
# including its interpolation, so the screen is unchanged. Where the old
# f-string used !r, the catalog uses {name!r} for the same reason.
# ---------------------------------------------------------------------
ERR_AUTHORING_REPOSITORY_MISSING = "ERR_AUTHORING_REPOSITORY_MISSING"
ERR_AUTHORING_DRAFT_KIND_UNSUPPORTED = "ERR_AUTHORING_DRAFT_KIND_UNSUPPORTED"
ERR_AUTHORING_TOPIC_TITLE_REQUIRED = "ERR_AUTHORING_TOPIC_TITLE_REQUIRED"
ERR_AUTHORING_BLOCK_TYPE_UNSUPPORTED = "ERR_AUTHORING_BLOCK_TYPE_UNSUPPORTED"
ERR_AUTHORING_ASSET_FILE_MISSING = "ERR_AUTHORING_ASSET_FILE_MISSING"
ERR_AUTHORING_TOPIC_ID_REQUIRED = "ERR_AUTHORING_TOPIC_ID_REQUIRED"
ERR_AUTHORING_OBJECT_KIND_UNSUPPORTED = "ERR_AUTHORING_OBJECT_KIND_UNSUPPORTED"

#: The Authoring/Contenuti batch. Kept separate from MIGRATED_CODES so a
#: suite can still assert which layer each code belongs to.
AUTHORING_CODES = (
    ERR_AUTHORING_REPOSITORY_MISSING,
    ERR_AUTHORING_DRAFT_KIND_UNSUPPORTED,
    ERR_AUTHORING_TOPIC_TITLE_REQUIRED,
    ERR_AUTHORING_BLOCK_TYPE_UNSUPPORTED,
    ERR_AUTHORING_ASSET_FILE_MISSING,
    ERR_AUTHORING_TOPIC_ID_REQUIRED,
    ERR_AUTHORING_OBJECT_KIND_UNSUPPORTED,
)

#: Every code migrated so far, whatever the layer.
ALL_CODES = MIGRATED_CODES + AUTHORING_CODES

#: What the UI shows when it meets a code it has no sentence for, or a
#: failure that is not a DomainError at all. Kept here so the fallback is
#: part of the contract rather than an accident of the resolver.
ERR_UNKNOWN = "ERR_UNKNOWN"
